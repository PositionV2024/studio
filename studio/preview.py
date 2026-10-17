from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, QUrl, QUrlQuery, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView

from .bridge import EditorBridge


class PreviewView(QWebEngineView):
    """Embedded browser preview with resilient Streamlit loading."""

    element_selected = Signal(dict)
    browser_message = Signal(str)
    preview_loaded = Signal(str)

    def __init__(self, resources_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.resources_dir = resources_dir
        self.bridge = EditorBridge(self)
        self.bridge.element_selected.connect(self.element_selected)
        self.bridge.browser_message.connect(self.browser_message)

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
        self._max_load_retries = 6

        self._install_editor_script()
        self.loadFinished.connect(self._after_load)
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
        source = script_path.read_text(encoding="utf-8")
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

    def load_preview(self, url: str) -> None:
        """Load a Streamlit URL and retry if Chromium sees an early blank page."""

        self._current_preview_url = str(url).rstrip("/") + "/"
        self._load_generation += 1
        generation = self._load_generation
        self._load_retry_count = 0
        self._remote_load_active = False
        self.setHtml(self._loading_html(self._current_preview_url))
        QTimer.singleShot(100, lambda: self._navigate_preview(generation))

    def _navigate_preview(self, generation: int) -> None:
        if generation != self._load_generation or not self._current_preview_url:
            return
        target = QUrl(self._current_preview_url)
        query = QUrlQuery(target)
        query.addQueryItem("_hps_preview", str(time.time_ns()))
        target.setQuery(query)
        self._remote_load_active = True
        self.load(target)

    def _retry_preview(self, reason: str) -> None:
        if not self._current_preview_url:
            return
        if self._load_retry_count >= self._max_load_retries:
            self._remote_load_active = False
            self.setHtml(self._error_html(self._current_preview_url, reason))
            self.browser_message.emit(
                f"Preview failed after {self._load_retry_count} retries: {reason}\n"
            )
            return
        self._load_retry_count += 1
        delay = min(1800, 300 + self._load_retry_count * 250)
        generation = self._load_generation
        self.browser_message.emit(
            f"Preview was not ready ({reason}); retry {self._load_retry_count}/{self._max_load_retries}.\n"
        )
        QTimer.singleShot(delay, lambda: self._navigate_preview(generation))

    def show_project_placeholder(
        self,
        project_name: str,
        *,
        running_project_name: str | None = None,
        starting: bool = False,
    ) -> None:
        """Show a lightweight transition page without touching a server."""

        self._load_generation += 1
        self._remote_load_active = False
        self._current_preview_url = None
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
        self._load_retry_count = 0
        self._remote_load_active = True
        self.page().triggerAction(QWebEnginePage.WebAction.ReloadAndBypassCache)

    def set_edit_mode(self, enabled: bool) -> None:
        self._edit_mode = bool(enabled)
        value = "true" if enabled else "false"
        self.page().runJavaScript(
            f"window.hpsSetEditMode && window.hpsSetEditMode({value});"
        )

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

    def _after_load(self, ok: bool) -> None:
        if not self._remote_load_active:
            return
        if not ok:
            self._retry_preview("Chromium could not load the page")
            return

        generation = self._load_generation
        QTimer.singleShot(
            850,
            lambda: self._verify_remote_document(generation),
        )

    def _verify_remote_document(self, generation: int) -> None:
        if generation != self._load_generation or not self._remote_load_active:
            return
        script = """
        (() => {
          const body = document.body;
          const root = document.querySelector('#root, .stApp, [data-testid="stAppViewContainer"]');
          return {
            ready: Boolean(body && root),
            bodyChildren: body ? body.children.length : 0,
            textLength: body ? (body.innerText || '').trim().length : 0,
            href: location.href
          };
        })();
        """
        self.page().runJavaScript(
            script,
            lambda result: self._remote_document_checked(generation, result),
        )

    def _remote_document_checked(self, generation: int, result: Any) -> None:
        if generation != self._load_generation or not self._remote_load_active:
            return
        data = result if isinstance(result, dict) else {}
        if not data.get("ready") and int(data.get("bodyChildren", 0) or 0) == 0:
            self._retry_preview("the page was blank while Streamlit was starting")
            return

        self._remote_load_active = False
        self.set_edit_mode(self._edit_mode)
        if self._draft_settings:
            self.set_draft_settings(self._draft_settings)
        if self._current_preview_url:
            self.preview_loaded.emit(self._current_preview_url)

