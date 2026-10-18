from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEventLoop, QFile, QIODevice, QTimer, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEngineLoadingInfo,
    QWebEngineProfile,
    QWebEngineScript,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from shiboken6 import isValid

from .bridge import EditorBridge


class PreviewView(QWebEngineView):
    """Embedded browser preview with resilient Streamlit loading."""

    element_selected = Signal(dict)
    browser_message = Signal(str)
    preview_loaded = Signal(str)
    inline_patch_changed = Signal(str, dict)
    inline_edit_started = Signal(str)
    inline_edit_finished = Signal(str, bool)
    inline_pending_changed = Signal(bool)

    def __init__(self, resources_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.resources_dir = resources_dir
        self.bridge = EditorBridge(self)
        self.bridge.element_selected.connect(self._browser_element_selected)
        self.bridge.browser_message.connect(self.browser_message)
        self.bridge.inline_edit.connect(self._browser_inline_edit)
        self.bridge.inline_flushed.connect(self._browser_inline_flushed)

        self.channel = QWebChannel(self.page())
        self.channel.registerObject("studioBridge", self.bridge)
        self.page().setWebChannel(self.channel)
        self.page().setBackgroundColor(QColor("#0b1020"))
        self.page().profile().setHttpCacheType(
            QWebEngineProfile.HttpCacheType.NoCache
        )

        self._edit_mode = False
        self._draft_settings: dict[str, Any] = {}
        self._current_preview_url: str | None = None
        self._remote_load_active = False
        self._load_generation = 0
        self._load_retry_count = 0
        self._max_load_retries = 3
        self._load_timeout_seconds = 45.0
        self._load_deadline = 0.0
        self._document_token = ""
        self._inline_sessions: dict[str, dict[str, Any]] = {}
        self._inline_pending = False
        self._inline_flush_loop: QEventLoop | None = None
        self._inline_flush_request = ""
        self._inline_flush_result = False
        self._inline_flush_serial = 0
        self._probe_pending = False
        self._last_load_error = ""
        self._retry_generation = 0
        self._readiness_timer = QTimer(self)
        self._readiness_timer.setInterval(250)
        self._readiness_timer.timeout.connect(self._poll_preview)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_navigation)

        self._install_editor_script()
        self.page().loadingChanged.connect(self._loading_changed)
        self.setHtml(self._empty_preview_html())

    @staticmethod
    def _empty_preview_html() -> str:
        return """
        <!doctype html>
        <html><head><meta charset='utf-8'>
        <style>
          body{margin:0;background:#0c1220;color:#e7eefc;font-family:Inter,Segoe UI,Arial,sans-serif;display:grid;place-items:center;min-height:100vh}
          .card{padding:28px 34px;border:1px solid #26395f;border-radius:20px;background:#111b30;box-shadow:0 24px 60px rgba(0,0,0,.35);text-align:center;max-width:520px}
          h2{margin:0 0 10px}.muted{color:#9fb0d0;line-height:1.55}
        </style></head><body>
          <div class='card'><h2>Project preview</h2><div class='muted'>Select a project, then press <b>Run</b>. Running previews open in a closable tab without stopping the server.</div></div>
        </body></html>
        """

    @staticmethod
    def _loading_html(url: str) -> str:
        safe_url = html.escape(url)
        return f"""
        <!doctype html>
        <html><head><meta charset='utf-8'>
        <style>
          *{{box-sizing:border-box}}
          body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#08101e;color:#edf5ff;font-family:Inter,Segoe UI,Arial,sans-serif}}
          .panel{{width:min(570px,calc(100% - 44px));padding:32px;border:1px solid #31527f;border-radius:22px;background:linear-gradient(145deg,#142748,#0c172d);box-shadow:0 30px 80px rgba(0,0,0,.44);text-align:center}}
          .spinner{{width:42px;height:42px;margin:0 auto 18px;border:3px solid rgba(116,181,255,.2);border-top-color:#74b5ff;border-radius:50%;animation:spin .8s linear infinite}}
          h2{{margin:0 0 9px}} p{{margin:0;color:#a8b9d5;line-height:1.55}} code{{color:#8ec7ff}}
          @keyframes spin{{to{{transform:rotate(360deg)}}}}
        </style></head><body><section class='panel'><div class='spinner'></div><h2>Loading project preview</h2><p>Connecting to <code>{safe_url}</code></p></section></body></html>
        """

    @staticmethod
    def _error_html(url: str, message: str) -> str:
        safe_url = html.escape(url)
        safe_message = html.escape(message)
        return f"""
        <!doctype html>
        <html><head><meta charset='utf-8'>
        <style>
          body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#08101e;color:#f2f6ff;font-family:Inter,Segoe UI,Arial,sans-serif}}
          .panel{{width:min(620px,calc(100% - 44px));padding:32px;border:1px solid #83465a;border-radius:22px;background:linear-gradient(145deg,#301927,#10182b);box-shadow:0 30px 80px rgba(0,0,0,.45);text-align:center}}
          h2{{margin:0 0 10px}} p{{color:#c3aec0;line-height:1.6}} code{{color:#ff9cb5}}
        </style></head><body><section class='panel'><h2>Preview could not be displayed</h2><p>{safe_message}</p><p><code>{safe_url}</code></p><p>Use <b>Run → Refresh Preview</b> to try again. The Streamlit process can remain running.</p></section></body></html>
        """

    def _install_editor_script(self) -> None:
        script_path = self.resources_dir / "editor.js"
        channel_library = QFile(":/qtwebchannel/qwebchannel.js")
        if not channel_library.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError("Qt's bundled WebChannel JavaScript could not be opened.")
        try:
            channel_source = bytes(channel_library.readAll()).decode("utf-8")
        finally:
            channel_library.close()
        # HTTP pages cannot load a qrc: script through a DOM <script> request.
        # Inject Qt's trusted bundled client with our editor instead of relaxing
        # Chromium's local-resource access settings.
        source = channel_source + "\n;\n" + script_path.read_text(encoding="utf-8")
        script = QWebEngineScript()
        script.setName("HoYoProjectStudioEditor")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(source)
        self.page().scripts().insert(script)

    @property
    def current_preview_url(self) -> str | None:
        return self._current_preview_url

    @staticmethod
    def _canonical_preview_url(url: str) -> str:
        target = QUrl(str(url))
        if not target.path():
            target.setPath("/")
        return target.toString(QUrl.ComponentFormattingOption.FullyEncoded)

    def preview_matches(self, url: str) -> bool:
        """Compare logical URLs without treating a missing root slash as new."""

        return bool(
            self._current_preview_url
            and self._canonical_preview_url(url) == self._current_preview_url
        )

    def _cancel_pending_load(self) -> None:
        # A JavaScript callback can arrive even after navigation is stopped.
        # Invalidate its generation before asking Chromium to stop anything.
        self._load_generation += 1
        self._remote_load_active = False
        self._current_preview_url = None
        self._document_token = ""
        self._finish_detached_inline_edits()
        self._probe_pending = False
        self._readiness_timer.stop()
        self._retry_timer.stop()
        self.stop()

    def stop_preview(self) -> None:
        """Cancel loading and detach the remote page, including its WebSocket."""

        self._cancel_pending_load()
        self.setUrl(QUrl("about:blank"))

    def load_preview(self, url: str) -> None:
        """Navigate once, then wait for Streamlit's frontend to render."""

        self._cancel_pending_load()
        self._current_preview_url = self._canonical_preview_url(url)
        self._load_retry_count = 0
        self._last_load_error = ""
        self._load_deadline = time.monotonic() + self._load_timeout_seconds
        self._remote_load_active = True
        # Keep the existing starting page visible until Chromium navigates.
        # An intervening setHtml/loadFinished pair used to race this request.
        self._navigate_preview(self._load_generation)

    def _is_current_load(self, generation: int, token: str | None = None) -> bool:
        # Qt invokes outstanding runJavaScript callbacks while deleting a page.
        # Do not access the deleted C++ view from one of those callbacks.
        return bool(
            isValid(self)
            and generation == self._load_generation
            and self._remote_load_active
            and self._current_preview_url
            and (token is None or token == self._document_token)
        )

    def _install_document_token(self) -> None:
        """Identify the new document without modifying Streamlit query params."""

        scripts = self.page().scripts()
        for previous in scripts.find("HoYoPreviewDocumentToken"):
            scripts.remove(previous)
        script = QWebEngineScript()
        script.setName("HoYoPreviewDocumentToken")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(
            "window.__hpsPreviewDocumentToken = "
            + json.dumps(self._document_token)
            + ";"
        )
        scripts.insert(script)

    def _navigate_preview(self, generation: int) -> None:
        if not self._is_current_load(generation):
            return
        self._document_token = f"{generation}:{time.monotonic_ns()}"
        self._probe_pending = False
        self._install_document_token()
        self.load(QUrl(self._current_preview_url))
        self._readiness_timer.start()

    def _retry_navigation(self) -> None:
        self._navigate_preview(self._retry_generation)

    def _document_is_local_preview(self, href: str) -> bool:
        if not self._current_preview_url:
            return False
        expected = QUrl(self._current_preview_url)
        actual = QUrl(str(href))
        # Streamlit can update the query string and path as the app starts.
        # The injected token identifies the document; the origin rejects our
        # own data/about placeholders and Chromium's internal error documents.
        return bool(
            actual.scheme() in {"http", "https"}
            and actual.scheme() == expected.scheme()
            and actual.host() == expected.host()
            and actual.port() == expected.port()
        )

    def _loading_changed(self, info: QWebEngineLoadingInfo) -> None:
        if not self._is_current_load(self._load_generation):
            return
        if info.status() != QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus:
            return
        if not self._document_is_local_preview(info.url().toString()):
            return
        if self.page().isLoading():
            return
        # Only an actual Chromium navigation failure triggers a new request.
        # An empty root while React starts must never restart that startup.
        reason = info.errorString().strip() or "Chromium could not load the page"
        self._last_load_error = reason
        self._retry_preview(reason)

    def _retry_preview(self, reason: str) -> None:
        if not self._is_current_load(self._load_generation):
            return
        if self._retry_timer.isActive():
            return
        if (
            self._load_retry_count >= self._max_load_retries
            or time.monotonic() >= self._load_deadline
        ):
            self._fail_preview(reason)
            return
        self._load_retry_count += 1
        self._readiness_timer.stop()
        self._probe_pending = False
        self._retry_generation = self._load_generation
        delay = min(1800, 500 + self._load_retry_count * 350)
        self.browser_message.emit(
            f"Preview connection failed ({reason}); reconnecting "
            f"{self._load_retry_count}/{self._max_load_retries}.\n"
        )
        self._retry_timer.start(delay)

    def _fail_preview(self, reason: str) -> None:
        if not self._is_current_load(self._load_generation):
            return
        url = self._current_preview_url
        self._remote_load_active = False
        self._probe_pending = False
        self._readiness_timer.stop()
        self._retry_timer.stop()
        self.stop()
        self.setHtml(self._error_html(url, reason))
        self.browser_message.emit(f"Preview could not be displayed: {reason}\n")

    def show_project_placeholder(
        self,
        project_name: str,
        *,
        running_project_name: str | None = None,
        starting: bool = False,
    ) -> None:
        """Show a lightweight transition page without touching a server."""

        self._cancel_pending_load()
        safe_name = html.escape(str(project_name))

        if starting:
            heading = f"Starting {safe_name}"
            detail = (
                "Project Studio is launching Streamlit. The preview will "
                "appear here automatically when the server is ready."
            )
            badge = "STARTING"
        elif running_project_name:
            safe_running = html.escape(str(running_project_name))
            heading = safe_name
            detail = (
                f"{safe_running} is still running in the background. "
                f"Use Run → Open Running Preview to return to it, or press "
                f"Switch and Run to start {safe_name}."
            )
            badge = "PROJECT SELECTED"
        else:
            heading = safe_name
            detail = (
                "This project is not running yet. Press Run to start its "
                "Streamlit preview."
            )
            badge = "READY"

        self.setHtml(
            f"""
            <!doctype html>
            <html><head><meta charset='utf-8'>
            <style>
              *{{box-sizing:border-box}}
              body{{margin:0;min-height:100vh;display:grid;place-items:center;
                    background:radial-gradient(circle at 35% 20%,#17284a 0,transparent 34%),#09101f;
                    color:#eef4ff;font-family:Inter,Segoe UI,Arial,sans-serif}}
              .card{{width:min(620px,calc(100% - 48px));padding:34px 38px;border:1px solid #304b78;
                     border-radius:24px;background:linear-gradient(145deg,rgba(24,39,72,.94),rgba(10,18,35,.96));
                     box-shadow:0 30px 80px rgba(0,0,0,.42);text-align:center}}
              .badge{{display:inline-block;margin-bottom:14px;padding:6px 10px;border:1px solid #4e78b8;
                      border-radius:999px;color:#8fc2ff;font-size:11px;font-weight:800;letter-spacing:.14em}}
              h2{{margin:0 0 10px;font-size:30px}}
              p{{margin:0;color:#aebfda;line-height:1.65;font-size:14px}}
              .dot{{display:inline-block;width:8px;height:8px;margin-right:7px;border-radius:50%;background:#65adff;
                    box-shadow:0 0 15px #65adff;animation:pulse 1.4s ease-in-out infinite}}
              @keyframes pulse{{50%{{opacity:.35;transform:scale(.72)}}}}
            </style></head><body>
              <section class='card'>
                <div class='badge'><span class='dot'></span>{badge}</div>
                <h2>{heading}</h2>
                <p>{detail}</p>
              </section>
            </body></html>
            """
        )

    def refresh_preview(self) -> None:
        if not self._current_preview_url:
            return
        # An error page is setHtml content. Reloading that page just reloads the
        # error; always return to the remembered remote URL instead.
        self.load_preview(self._current_preview_url)

    def set_edit_mode(self, enabled: bool) -> None:
        self._edit_mode = bool(enabled)
        value = "true" if enabled else "false"
        self.page().runJavaScript(
            f"window.hpsSetEditMode && window.hpsSetEditMode({value});"
        )

    def _browser_event_is_current(self, data: dict[str, Any]) -> bool:
        return bool(
            isValid(self)
            and self._current_preview_url
            and self._document_token
            and data.get("document_token") == self._document_token
        )

    def _browser_element_selected(self, data: dict[str, Any]) -> None:
        if not self._edit_mode or not self._browser_event_is_current(data):
            return
        description = dict(data)
        description.pop("document_token", None)
        self.element_selected.emit(description)

    @property
    def has_pending_inline_edits(self) -> bool:
        return self._inline_pending

    @property
    def inline_original_text(self) -> str | None:
        session = next(reversed(self._inline_sessions), None)
        return self._inline_sessions[session]["original"] if session is not None else None

    def _update_inline_pending(self) -> None:
        pending = any(
            state["text"] != state["original"]
            for state in self._inline_sessions.values()
        )
        if pending != self._inline_pending:
            self._inline_pending = pending
            self.inline_pending_changed.emit(pending)

    def _browser_inline_edit(self, data: dict[str, Any]) -> None:
        if not self._browser_event_is_current(data):
            return
        session_id = data["session"]
        element_id = data["id"]
        phase = data["phase"]
        text = data["text"]
        state = self._inline_sessions.get(session_id)
        if phase == "start":
            if not self._edit_mode or state is not None:
                return
            self._inline_sessions[session_id] = {
                "id": element_id, "original": text, "text": text,
            }
            self.inline_edit_started.emit(element_id)
            return
        if state is None or state["id"] != element_id:
            return
        if text != state["text"]:
            state["text"] = text
            current = self._draft_settings.get(element_id, {})
            patch = dict(current) if isinstance(current, dict) else {}
            patch["text"] = text
            self._draft_settings = {**self._draft_settings, element_id: patch}
            # Do not echo this update back through runJavaScript. A queued
            # earlier keystroke must not overwrite newer browser input.
            self.inline_patch_changed.emit(element_id, {"text": text})
        if phase in {"finish", "cancel"}:
            self._inline_sessions.pop(session_id, None)
            self.inline_edit_finished.emit(element_id, phase == "cancel")
        self._update_inline_pending()

    def _finish_detached_inline_edits(self) -> None:
        """Keep acknowledged text when a process or document disappears."""

        for state in list(getattr(self, "_inline_sessions", {}).values()):
            self.inline_edit_finished.emit(state["id"], False)
        if hasattr(self, "_inline_sessions"):
            self._inline_sessions.clear()
            self._update_inline_pending()
        loop = getattr(self, "_inline_flush_loop", None)
        if loop is not None:
            self._inline_flush_result = False
            loop.quit()

    def _browser_inline_flushed(self, data: dict[str, Any]) -> None:
        if (
            not self._browser_event_is_current(data)
            or data.get("request") != self._inline_flush_request
        ):
            return
        self._inline_flush_result = data.get("success") is True
        if self._inline_flush_loop is not None:
            self._inline_flush_loop.quit()

    def commit_inline_edit(self, *, timeout_ms: int = 3500) -> bool:
        """Finish typing before Save, including messages from embedded frames.

        WebChannel delivery is asynchronous. A short nested Qt event loop lets
        Chromium send its final text and finish events before Save reads the
        working draft. User input is excluded during this flush to keep it from
        starting another save or closing the window recursively.
        """

        if self._inline_flush_loop is not None:
            return True
        if not self._current_preview_url or not self._document_token:
            return True
        if not self._edit_mode and not self._inline_sessions:
            return True
        generation = self._load_generation
        token = self._document_token
        self._inline_flush_serial += 1
        request = f"qt:{token}:{self._inline_flush_serial}"
        self._inline_flush_request = request
        self._inline_flush_result = False
        loop = QEventLoop(self)
        self._inline_flush_loop = loop
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)

        def invoked(result: Any) -> None:
            if (
                not isValid(self)
                or self._inline_flush_request != request
                or self._inline_flush_loop is not loop
            ):
                return
            if generation != self._load_generation or token != self._document_token:
                loop.quit()
            elif result is False and not self._inline_sessions:
                # Loading/placeholder documents have no editor or text buffer.
                self._inline_flush_result = True
                loop.quit()

        script = (
            "(() => { if (!window.hpsCommitInlineEdit) return false; "
            "window.hpsCommitInlineEdit(" + json.dumps(request) + "); return true; })();"
        )
        try:
            self.page().runJavaScript(script, invoked)
            timer.start(max(100, timeout_ms))
            loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            succeeded = self._inline_flush_result
        finally:
            timer.stop()
            timer.deleteLater()
            self._inline_flush_loop = None
            self._inline_flush_request = ""
            loop.deleteLater()
        if not succeeded:
            self.browser_message.emit(
                "The preview could not finish its current text edit. "
                "Your working changes are still open; try Save again.\n"
            )
        return succeeded

    def set_draft_settings(self, settings: dict[str, Any]) -> None:
        self._draft_settings = settings
        payload = json.dumps(settings, ensure_ascii=False).replace("<", "\\u003c")
        self.page().runJavaScript(
            f"window.hpsApplyAll && window.hpsApplyAll({payload});"
        )

    def apply_patch(self, element_id: str, patch: dict[str, Any]) -> None:
        safe_id = json.dumps(element_id, ensure_ascii=False)
        safe_patch = json.dumps(patch, ensure_ascii=False).replace("<", "\\u003c")
        self.page().runJavaScript(
            f"window.hpsApplyPatch && window.hpsApplyPatch({safe_id}, {safe_patch});"
        )

    def clear_selection(self) -> None:
        self.page().runJavaScript(
            "window.hpsClearSelection && window.hpsClearSelection();"
        )

    def _poll_preview(self) -> None:
        generation = self._load_generation
        if not self._is_current_load(generation):
            return
        if time.monotonic() >= self._load_deadline:
            reason = self._last_load_error or (
                "The Streamlit server responded, but its app interface did not "
                f"appear within {self._load_timeout_seconds:g} seconds. "
                "Check Output for a Python error, then refresh the preview."
            )
            self._fail_preview(reason)
            return
        if self._probe_pending or self._retry_timer.isActive():
            return
        self._probe_pending = True
        token = self._document_token
        script = """
        (() => {
          const app = document.querySelector(
            '.stApp, [data-testid="stApp"], [data-testid="stAppViewContainer"]'
          );
          return JSON.stringify({
            token: window.__hpsPreviewDocumentToken || '',
            href: location.href,
            ready: Boolean(document.body && app),
            documentState: document.readyState
          });
        })();
        """
        # PySide versions including 6.12 convert a returned JavaScript object
        # to an empty Python string. JSON is a stable scalar across bindings.
        self.page().runJavaScript(
            script,
            lambda result: self._remote_document_checked(generation, token, result),
        )

    def _remote_document_checked(
        self, generation: int, token: str, result: Any
    ) -> None:
        if not self._is_current_load(generation, token):
            return
        self._probe_pending = False
        try:
            data = json.loads(result) if isinstance(result, str) else result
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        if data.get("token") != token:
            return
        if not self._document_is_local_preview(data.get("href", "")):
            return
        if data.get("ready") is not True:
            # Poll this document again. Its JavaScript may still be loading or
            # waiting for Streamlit's WebSocket and initial app data.
            return

        self._remote_load_active = False
        self._readiness_timer.stop()
        self._retry_timer.stop()
        self.set_edit_mode(self._edit_mode)
        if self._draft_settings:
            self.set_draft_settings(self._draft_settings)
        if self._current_preview_url:
            self.preview_loaded.emit(self._current_preview_url)

