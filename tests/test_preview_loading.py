"""Readiness/cancellation regressions, plus an optional real Chromium smoke.

Run the local HTTP/browser smoke with HPS_RUN_WEBENGINE_TESTS=1. It uses a
separate process so Chromium's application/profile lifetime cannot interfere
with ordinary Qt widget tests. No user projects or external sites are opened.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QUrl

import studio.preview as preview_module
from studio.preview import PreviewView


class TimerProbe:
    def __init__(self):
        self.active = False
        self.delays = []

    def isActive(self):
        return self.active

    def start(self, delay=None):
        self.active = True
        self.delays.append(delay)

    def stop(self):
        self.active = False


class PreviewProbe:
    """Production methods with only the browser and Qt timers replaced."""

    def __init__(self):
        self._load_generation = 8
        self._document_token = "document-eight"
        self._remote_load_active = True
        self._current_preview_url = "http://127.0.0.1:8501/"
        self._probe_pending = True
        self._load_retry_count = 0
        self._max_load_retries = 3
        self._load_timeout_seconds = 45.0
        self._load_deadline = time.monotonic() + 45
        self._last_load_error = ""
        self._retry_generation = 0
        self._readiness_timer = TimerProbe()
        self._readiness_timer.start()
        self._retry_timer = TimerProbe()
        self._edit_mode = True
        self._draft_settings = {"elements": {"title": {"color": "#123456"}}}
        self.preview_loaded = SimpleNamespace(emit=Mock())
        self.browser_message = SimpleNamespace(emit=Mock())
        self.set_edit_mode = Mock()
        self.set_draft_settings = Mock()
        self.stop = Mock()
        self.setUrl = Mock()
        self.setHtml = Mock()
        self.load = Mock()
        self.page_value = Mock()
        self.page = Mock(return_value=self.page_value)

    def __getattr__(self, name):
        method = PreviewView.__dict__.get(name)
        if isinstance(method, staticmethod):
            return method.__func__
        if isinstance(method, property):
            return method.fget(self)
        if callable(method):
            return MethodType(method, self)
        raise AttributeError(name)

    def document(self, **changes):
        data = {
            "token": self._document_token,
            "href": self._current_preview_url,
            "ready": True,
            "documentState": "complete",
        }
        data.update(changes)
        return json.dumps(data)


@pytest.fixture
def preview_probe(monkeypatch):
    value = PreviewProbe()
    original = preview_module.isValid
    monkeypatch.setattr(
        preview_module, "isValid", lambda obj: obj is value or original(obj)
    )
    return value


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8501", "http://127.0.0.1:8501/"),
        ("http://127.0.0.1:8501/", "http://127.0.0.1:8501/"),
        ("http://localhost:8501?name=a%2Fb#tab", "http://localhost:8501/?name=a%2Fb#tab"),
        ("http://localhost:8501/page?x=1", "http://localhost:8501/page?x=1"),
    ],
)
def test_preview_url_normalization_preserves_app_query_and_path(url, expected):
    assert PreviewView._canonical_preview_url(url) == expected


def test_missing_root_slash_does_not_reload_the_same_preview(preview_probe):
    assert preview_probe.preview_matches("http://127.0.0.1:8501")
    assert not preview_probe.preview_matches("http://127.0.0.1:8502")


def test_json_document_marks_ready_once_and_restores_editor_settings(preview_probe):
    value = preview_probe
    generation, token = value._load_generation, value._document_token
    result = value.document()
    value._remote_document_checked(generation, token, result)
    value._remote_document_checked(generation, token, result)

    value.preview_loaded.emit.assert_called_once_with("http://127.0.0.1:8501/")
    value.set_edit_mode.assert_called_once_with(True)
    value.set_draft_settings.assert_called_once_with(value._draft_settings)
    assert not value._readiness_timer.isActive()
    assert not value._remote_load_active
    value.load.assert_not_called()


@pytest.mark.parametrize("result", [None, "", "not JSON", "[]", "null", "true"])
def test_unavailable_javascript_result_waits_without_reloading(preview_probe, result):
    value = preview_probe
    value._remote_document_checked(value._load_generation, value._document_token, result)
    assert value._remote_load_active
    assert not value._probe_pending
    assert value._load_retry_count == 0
    assert value._readiness_timer.isActive()
    value.load.assert_not_called()
    value.browser_message.emit.assert_not_called()
    value.preview_loaded.emit.assert_not_called()


def test_empty_streamlit_shell_waits_without_restarting_hydration(preview_probe):
    value = preview_probe
    value._remote_document_checked(
        value._load_generation, value._document_token, value.document(ready=False)
    )
    assert value._remote_load_active
    assert value._load_retry_count == 0
    value.load.assert_not_called()
    value.preview_loaded.emit.assert_not_called()


@pytest.mark.parametrize("stale", ["generation", "callback_token", "document_token"])
def test_previous_document_cannot_complete_current_navigation(preview_probe, stale):
    value = preview_probe
    generation = value._load_generation - (stale == "generation")
    token = "old-document" if stale == "callback_token" else value._document_token
    result = value.document(token="old-document") if stale == "document_token" else value.document()
    value._remote_document_checked(generation, token, result)
    assert value._remote_load_active
    value.preview_loaded.emit.assert_not_called()
    if stale in {"generation", "callback_token"}:
        assert value._probe_pending, "A stale callback must not clear a new pending probe"


@pytest.mark.parametrize("href", ["about:blank", "data:text/html,placeholder", "http://127.0.0.1:8502/"])
def test_placeholder_or_other_server_cannot_be_reported_as_loaded(preview_probe, href):
    value = preview_probe
    value._remote_document_checked(
        value._load_generation, value._document_token, value.document(href=href)
    )
    value.preview_loaded.emit.assert_not_called()
    assert value._remote_load_active


def test_poll_uses_json_string_and_waits_for_only_one_callback(preview_probe):
    value = preview_probe
    value._probe_pending = False
    value._poll_preview()
    value._poll_preview()
    value.page_value.runJavaScript.assert_called_once()
    script, callback = value.page_value.runJavaScript.call_args.args
    assert "JSON.stringify" in script
    callback(value.document())
    value.preview_loaded.emit.assert_called_once()


def test_stopping_invalidates_callback_before_touching_browser(preview_probe):
    value = preview_probe
    generation, token, result = value._load_generation, value._document_token, value.document()
    value.stop.side_effect = lambda: value._remote_document_checked(generation, token, result)
    value._retry_timer.start(850)
    value.stop_preview()
    value._remote_document_checked(generation, token, result)
    value._retry_navigation()

    assert value.current_preview_url is None
    assert not value._remote_load_active
    assert not value._readiness_timer.isActive()
    assert not value._retry_timer.isActive()
    value.setUrl.assert_called_once_with(QUrl("about:blank"))
    value.preview_loaded.emit.assert_not_called()
    value.load.assert_not_called()


def test_callback_during_deleted_qt_view_does_not_access_page(preview_probe, monkeypatch):
    value = preview_probe
    monkeypatch.setattr(preview_module, "isValid", lambda obj: False)
    value._remote_document_checked(value._load_generation, value._document_token, value.document())
    value.page.assert_not_called()
    value.preview_loaded.emit.assert_not_called()


def test_refresh_navigates_to_remote_url_even_when_error_html_is_current(preview_probe):
    value = preview_probe
    value._remote_load_active = False
    value.load_preview = Mock()
    value.refresh_preview()
    value.load_preview.assert_called_once_with("http://127.0.0.1:8501/")
    value.page_value.triggerAction.assert_not_called()


def test_blank_frontend_timeout_is_bounded_and_reports_actionable_error(preview_probe):
    value = preview_probe
    value._load_deadline = time.monotonic() - 1
    value._poll_preview()
    value._poll_preview()
    assert not value._remote_load_active
    value.setHtml.assert_called_once()
    message = value.browser_message.emit.call_args.args[0]
    assert "45 seconds" in message
    assert "Check Output" in message
    value.page_value.runJavaScript.assert_not_called()


def test_network_failures_reconnect_with_a_bound_and_do_not_duplicate_timers(preview_probe):
    value = preview_probe
    for attempt in range(1, 4):
        value._retry_timer.stop()
        value._retry_preview("Connection refused")
        value._retry_preview("Duplicate load-failed event")
        assert value._load_retry_count == attempt
    assert value._retry_timer.delays == [850, 1200, 1550]
    value._retry_timer.stop()
    value._retry_preview("Connection refused")
    assert not value._remote_load_active
    value.setHtml.assert_called_once()
    value.preview_loaded.emit.assert_not_called()


@pytest.mark.skipif(
    os.environ.get("HPS_RUN_WEBENGINE_TESTS") != "1",
    reason="Set HPS_RUN_WEBENGINE_TESTS=1 to start the isolated local Chromium smoke",
)
def test_real_webengine_delayed_render_stop_and_refresh():
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    environment.setdefault("QT_QUICK_BACKEND", "software")
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        # This is a test process only. The desktop app keeps Chromium's normal
        # sandbox; root-owned CI containers cannot launch that sandbox.
        environment.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    environment.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-dev-shm-usage")
    environment["PYTHONPATH"] = str(project_root) + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--webengine-smoke"],
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=35,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    line = next(line for line in completed.stdout.splitlines() if line.startswith("WEBENGINE_RESULT:"))
    result = json.loads(line.split(":", 1)[1])
    assert result["slow_document_requests"] == 1
    assert result["stop_detached"] is True
    assert result["refresh_recovered"] is True
    assert result["query_preserved"] is True
    assert result["visual_selection_bridge"] is True
    assert result["network_reconnected"] is True


def _run_webengine_smoke():
    """Exercise production PreviewView against controllable HTTP documents."""

    import threading
    from collections import Counter
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit

    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    requests = []
    recovered = False
    network_available = False

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            path = urlsplit(self.path).path
            if path == "/disconnect" and not network_available:
                self.close_connection = True
                return
            if path in {"/slow", "/cancel"}:
                body = """<div id="root"></div><script>
                setTimeout(() => {
                  document.querySelector('#root').innerHTML =
                    '<main class="stApp" data-testid="stAppViewContainer">Rendered after startup</main>';
                }, 2500);
                </script>"""
            elif path == "/recover" and not recovered:
                body = '<div id="root"></div>'
            else:
                body = '<div id="root"><main class="stApp"><button id="fixture-control">Pick this element</button></main></div>'
            content = ("<!doctype html><html><body>" + body + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    application = QApplication.instance() or QApplication([])
    view = PreviewView(Path(__file__).resolve().parents[1] / "resources")
    view.resize(900, 650)
    view.show()
    loaded = []
    messages = []
    selections = []
    view.preview_loaded.connect(loaded.append)
    view.browser_message.connect(messages.append)
    view.element_selected.connect(selections.append)
    origin = f"http://127.0.0.1:{server.server_port}"

    def pump(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.01)

    def wait(predicate, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            application.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        assert predicate(), f"Timed out; logs={messages}, loaded={loaded}, url={view.url().toString()}"

    try:
        view.load_preview(origin + "/instant")
        wait(lambda: len(loaded) == 1)
        view.set_edit_mode(True)
        view.page().runJavaScript("""
            document.querySelector('#fixture-control').dispatchEvent(
              new PointerEvent('pointerdown', {bubbles: true, cancelable: true})
            );
        """)
        wait(lambda: bool(selections))
        assert selections[-1]["text"] == "Pick this element"
        assert selections[-1]["tag"] == "button"
        view.set_edit_mode(False)
        view.load_preview(origin + "/slow?app_value=kept")
        pump(1)
        assert len(loaded) == 1, "Old rendered DOM must not complete a new document"
        wait(lambda: len(loaded) == 2)
        assert not any("reconnect" in message or "could not" in message for message in messages), messages
        counts = Counter(urlsplit(path).path for path in requests)
        assert counts["/slow"] == 1, requests
        assert "/slow?app_value=kept" in requests
        assert not any("_hps_preview" in path for path in requests)

        view.load_preview(origin + "/cancel")
        wait(lambda: any(urlsplit(path).path == "/cancel" for path in requests))
        view.stop_preview()
        pump(3)
        assert len(loaded) == 2
        assert view.current_preview_url is None
        assert view.url().toString() == "about:blank"
        assert not view._readiness_timer.isActive()
        assert not view._retry_timer.isActive()

        view.load_preview(origin + "/disconnect")
        wait(lambda: any("reconnecting" in message for message in messages))
        assert len(loaded) == 2
        network_available = True
        wait(lambda: len(loaded) == 3)
        assert loaded[-1] == origin + "/disconnect"
        assert view._load_retry_count == 1

        view._load_timeout_seconds = 0.8
        view.load_preview(origin + "/recover")
        wait(lambda: not view._remote_load_active)
        assert any("app interface did not appear" in message for message in messages)
        assert len(loaded) == 3
        recovered = True
        view._load_timeout_seconds = 5
        view.refresh_preview()
        wait(lambda: len(loaded) == 4)
        assert loaded[-1] == origin + "/recover"
        assert view.url().toString() == origin + "/recover"
        assert Counter(urlsplit(path).path for path in requests)["/recover"] == 2

        print("WEBENGINE_RESULT:" + json.dumps({
            "slow_document_requests": counts["/slow"],
            "stop_detached": True,
            "refresh_recovered": True,
            "query_preserved": True,
            "visual_selection_bridge": True,
            "network_reconnected": True,
        }), flush=True)
    finally:
        view.stop_preview()
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
        server.shutdown()
        server.server_close()


if __name__ == "__main__" and "--webengine-smoke" in sys.argv:
    _run_webengine_smoke()
