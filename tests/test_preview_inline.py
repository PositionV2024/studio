"""Direct preview typing, channel ownership, and the Save/exit flush barrier."""

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

import studio.preview as preview_module
from studio.bridge import EditorBridge
from studio.preview import PreviewView


def inline_payload(**changes):
    return {
        "document_token": "current-document", "session": "typing-session",
        "id": "hero.title", "phase": "start", "text": "Original title",
        **changes,
    }


class InlineProbe:
    def __init__(self):
        self._edit_mode = True
        self._current_preview_url = "http://127.0.0.1:8501/"
        self._document_token = "current-document"
        self._inline_sessions = {}
        self._inline_pending = False
        self._inline_flush_loop = None
        self._inline_flush_request = "flush-request"
        self._inline_flush_result = False
        self._draft_settings = {"hero.title": {"font_size": 40}}
        self.events = []
        for signal in (
            "element_selected", "inline_patch_changed", "inline_edit_started",
            "inline_edit_finished", "inline_pending_changed", "browser_message",
        ):
            setattr(self, signal, SimpleNamespace(emit=Mock(
                side_effect=lambda *args, name=signal: self.events.append((name, *args))
            )))

    def __getattr__(self, name):
        method = PreviewView.__dict__.get(name)
        if isinstance(method, property):
            return method.fget(self)
        if callable(method):
            return MethodType(method, self)
        raise AttributeError(name)


@pytest.fixture
def inline_probe(monkeypatch):
    probe = InlineProbe()
    valid = preview_module.isValid
    monkeypatch.setattr(preview_module, "isValid", lambda obj: obj is probe or valid(obj))
    return probe


@pytest.mark.parametrize("payload", [
    "not json", "[]", "null", "{}",
    json.dumps(inline_payload(phase="execute")),
    json.dumps(inline_payload(id="")),
    json.dumps(inline_payload(session=123)),
    json.dumps(inline_payload(text={"html": "unsafe shape"})),
    json.dumps(inline_payload(document_token="")),
])
def test_bridge_rejects_malformed_inline_messages(payload):
    bridge = EditorBridge()
    events, warnings = [], []
    bridge.inline_edit.connect(events.append)
    bridge.browser_message.connect(warnings.append)
    bridge.inlineEdit(payload)
    assert events == []
    assert warnings == ["Invalid inline editing message.\n"]


def test_bridge_preserves_plain_text_and_validates_flush_acknowledgements():
    bridge = EditorBridge()
    events, flushed = [], []
    bridge.inline_edit.connect(events.append)
    bridge.inline_flushed.connect(flushed.append)
    data = inline_payload(text="<b>Plain text</b>\n中文 🔥")
    bridge.inlineEdit(json.dumps(data))
    assert events == [data]
    bridge.inlineFlushed(json.dumps({"document_token": "current-document", "request": "r", "success": "yes"}))
    assert flushed == []
    acknowledgement = {"document_token": "current-document", "request": "r", "success": True}
    bridge.inlineFlushed(json.dumps(acknowledgement))
    assert flushed == [acknowledgement]


def test_selection_requires_current_document_and_edit_mode(inline_probe):
    view = inline_probe
    view._browser_element_selected({"document_token": "old-document", "id": "stale"})
    view._browser_element_selected({"id": "missing token"})
    assert view.events == []
    view._browser_element_selected({"document_token": "current-document", "id": "hero.title"})
    assert view.events == [("element_selected", {"id": "hero.title"})]
    view._edit_mode = False
    view._browser_element_selected({"document_token": "current-document", "id": "disabled"})
    assert len(view.events) == 1


def test_inline_inputs_update_draft_without_replaying_browser_patches(inline_probe):
    view = inline_probe
    original = "  " + "Long text " * 300 + "\n"
    view._browser_inline_edit(inline_payload(text=original))
    assert view.inline_original_text == original
    assert not view.has_pending_inline_edits
    view._browser_inline_edit(inline_payload(phase="update", text="New title"))
    view._browser_inline_edit(inline_payload(phase="update", text="New title"))
    assert view._draft_settings == {"hero.title": {"font_size": 40, "text": "New title"}}
    assert view.has_pending_inline_edits
    view.inline_patch_changed.emit.assert_called_once_with("hero.title", {"text": "New title"})
    assert view.events == [
        ("inline_edit_started", "hero.title"),
        ("inline_patch_changed", "hero.title", {"text": "New title"}),
        ("inline_pending_changed", True),
    ]


@pytest.mark.parametrize("cancel", [False, True])
def test_final_text_is_delivered_before_session_finishes(inline_probe, cancel):
    view = inline_probe
    view._browser_inline_edit(inline_payload())
    view._browser_inline_edit(inline_payload(phase="update", text="Partial input"))
    text = "Original title" if cancel else "Last character included!"
    view._browser_inline_edit(inline_payload(phase="cancel" if cancel else "finish", text=text))
    assert view.events[-3:] == [
        ("inline_patch_changed", "hero.title", {"text": text}),
        ("inline_edit_finished", "hero.title", cancel),
        ("inline_pending_changed", False),
    ]
    assert view.inline_original_text is None
    assert not view.has_pending_inline_edits


def test_unknown_duplicate_or_stale_inline_sessions_cannot_change_draft(inline_probe):
    view = inline_probe
    view._browser_inline_edit(inline_payload(phase="update", text="Before start"))
    view._browser_inline_edit(inline_payload(document_token="previous document"))
    assert view.events == []
    view._browser_inline_edit(inline_payload())
    view._browser_inline_edit(inline_payload(text="Duplicate start"))
    view._browser_inline_edit(inline_payload(phase="update", id="different element", text="Wrong element"))
    view._browser_inline_edit(inline_payload(phase="update", session="different session", text="Wrong session"))
    view._browser_inline_edit(inline_payload(phase="update", document_token="previous document", text="Wrong document"))
    assert view.events == [("inline_edit_started", "hero.title")]
    assert view._draft_settings == {"hero.title": {"font_size": 40}}


def test_detaching_preview_finishes_acknowledged_edits_without_new_patches(inline_probe):
    view = inline_probe
    view._browser_inline_edit(inline_payload())
    view._browser_inline_edit(inline_payload(phase="update", text="Keep after Stop"))
    loop = SimpleNamespace(quit=Mock())
    view._inline_flush_loop = loop
    view._finish_detached_inline_edits()
    assert view.events[-2:] == [
        ("inline_edit_finished", "hero.title", False),
        ("inline_pending_changed", False),
    ]
    assert view._draft_settings["hero.title"]["text"] == "Keep after Stop"
    assert view._inline_sessions == {}
    loop.quit.assert_called_once()


def test_flush_acknowledgement_requires_the_current_request_and_document(inline_probe):
    view = inline_probe
    view._inline_flush_loop = SimpleNamespace(quit=Mock())
    view._browser_inline_flushed({"document_token": "old", "request": "flush-request", "success": True})
    view._browser_inline_flushed({"document_token": "current-document", "request": "old request", "success": True})
    assert not view._inline_flush_result
    view._inline_flush_loop.quit.assert_not_called()
    view._browser_inline_flushed({"document_token": "current-document", "request": "flush-request", "success": True})
    assert view._inline_flush_result
    view._inline_flush_loop.quit.assert_called_once()


@pytest.mark.skipif(
    os.environ.get("HPS_RUN_WEBENGINE_TESTS") != "1",
    reason="Set HPS_RUN_WEBENGINE_TESTS=1 to exercise native preview typing in Chromium",
)
def test_real_inline_typing_plain_paste_composition_and_nested_frame_flush():
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    environment.setdefault("QT_QUICK_BACKEND", "software")
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        environment.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    environment.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-dev-shm-usage")
    environment["PYTHONPATH"] = str(root) + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--inline-browser"],
        cwd=root, env=environment, text=True, capture_output=True, timeout=40,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    line = next(line for line in completed.stdout.splitlines() if line.startswith("INLINE_BROWSER_RESULT:"))
    result = json.loads(line.split(":", 1)[1])
    assert all(result.values()), result


def _run_inline_browser():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    documents = {
        "/": """<main class="stApp"><h1 id="title">Original title<a id="anchor" href="#title" aria-hidden="true"><svg width="14" height="14"><path d="M0 0L14 14"/></svg></a></h1>
        <button id="button">Launch<svg id="icon" width="14" height="14"></svg></button>
        <p id="rich"><strong>Rich child</strong> parent label</p><input id="live" value="Live value">
        <iframe id="child" title="fixture-child" src="/frame"></iframe></main>
        <script>window.actions=0;document.querySelector('#button').onclick=()=>window.actions++;</script>""",
        "/frame": """<h2 id="frame-title">Frame title</h2><iframe id="deep" title="fixture-deep" src="/deep"></iframe>""",
        "/deep": "<p id='deep-title'>Nested frame text</p>",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = documents.get(self.path, documents["/"])
            content = ("<!doctype html><html><head><meta charset='utf-8'><style>body{font:18px Arial;padding:14px}h1{font-size:32px}iframe{width:650px;height:250px;border:1px solid #aaa}svg{pointer-events:none}</style></head><body>" + body + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app = QApplication.instance() or QApplication([])
    view = PreviewView(Path(__file__).resolve().parents[1] / "resources")
    view.resize(1000, 850)
    view.show()
    events, selected, messages, loaded = [], [], [], []
    view.inline_edit_started.connect(lambda element_id: events.append(("start", element_id)))
    view.inline_patch_changed.connect(lambda element_id, patch: events.append(("patch", element_id, patch)))
    view.inline_edit_finished.connect(lambda element_id, cancel: events.append(("finish", element_id, cancel)))
    view.element_selected.connect(selected.append)
    view.browser_message.connect(messages.append)
    view.preview_loaded.connect(loaded.append)

    def wait(predicate, seconds=8):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        assert predicate(), f"Timed out: events={events}, messages={messages}, selections={selected}"

    def evaluate(expression):
        result = []
        view.page().runJavaScript("JSON.stringify(" + expression + ")", result.append)
        wait(lambda: bool(result))
        return json.loads(result[0])

    top = "document"
    frame = "document.querySelector('#child').contentDocument"
    deep = frame + ".querySelector('#deep').contentDocument"

    def pick(selector, document=top, editable=True):
        count = len(selected)
        evaluate("(() => { " + document + ".querySelector(" + json.dumps(selector) + ").dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,cancelable:true})); return true; })()")
        wait(lambda: len(selected) > count)
        if editable:
            wait(lambda: bool(view._inline_sessions))
        return selected[-1]["id"]

    def overlay(document=top):
        return document + ".querySelector('#hps-inline-editor').shadowRoot.querySelector('textarea')"

    def type_text(text):
        QTest.keyClicks(view.focusProxy() or view, text)

    try:
        view.load_preview(f"http://127.0.0.1:{server.server_port}/")
        wait(lambda: bool(loaded))
        wait(lambda: evaluate("Boolean(" + deep + ".querySelector('#deep-title'))"))
        view.set_edit_mode(True)
        title_id = pick("#title")
        assert evaluate(overlay() + ".value") == "Original title"
        type_text("Typed directly!")
        assert view.commit_inline_edit(), messages
        assert events[-1] == ("finish", title_id, False)
        assert view._draft_settings[title_id]["text"] == "Typed directly!"
        assert evaluate("document.querySelector('#title').firstChild.nodeValue") == "Typed directly!"
        assert evaluate("Boolean(document.querySelector('#anchor svg'))")
        assert not evaluate("Boolean(document.querySelector('#hps-inline-editor'))")

        pick("#title")
        type_text("Cancel this change")
        QTest.keyClick(view.focusProxy() or view, Qt.Key.Key_Escape)
        wait(lambda: not view._inline_sessions)
        assert events[-1] == ("finish", title_id, True)
        assert evaluate("document.querySelector('#title').firstChild.nodeValue") == "Typed directly!"

        button_id = pick("#button")
        type_text("Button label")
        QTest.keyClick(view.focusProxy() or view, Qt.Key.Key_Return)
        wait(lambda: not view._inline_sessions)
        assert events[-1] == ("finish", button_id, False)
        assert evaluate("Boolean(document.querySelector('#icon')) && window.actions === 0")

        pick("#title")
        evaluate("(() => { const input=" + overlay() + "; const data=new DataTransfer(); data.setData('text/plain','<b>Plain paste</b>'); data.setData('text/html','<b>Rich paste</b>'); input.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:data})); return true; })()")
        assert view.commit_inline_edit(), messages
        assert evaluate("document.querySelector('#title').firstChild.nodeValue") == "<b>Plain paste</b>"
        assert not evaluate("Boolean(document.querySelector('#title b'))")

        pick("#title")
        evaluate("(() => { const input=" + overlay() + "; input.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true})); input.value='中'; input.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true})); input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true,isComposing:true})); return true; })()")
        assert evaluate("Boolean(document.querySelector('#hps-inline-editor'))")
        evaluate("(() => { const input=" + overlay() + "; input.value='中文'; input.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true})); input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true})); return true; })()")
        wait(lambda: not view._inline_sessions)
        assert view._draft_settings[title_id]["text"] == "中文"

        pick("#rich", editable=False)
        assert "text" not in selected[-1]["supported_fields"]
        assert not evaluate("Boolean(document.querySelector('#hps-inline-editor'))")
        pick("#live", editable=False)
        assert "text" not in selected[-1]["supported_fields"]
        assert evaluate("document.querySelector('#live').value") == "Live value"

        frame_id = pick("#frame-title", frame)
        type_text("Changed inside frame")
        next_id = pick("#title")
        previous_finish = max(index for index, item in enumerate(events) if item == ("finish", frame_id, False))
        new_start = max(index for index, item in enumerate(events) if item == ("start", next_id))
        assert previous_finish < new_start
        assert view._draft_settings[frame_id]["text"] == "Changed inside frame"
        assert view.commit_inline_edit(), messages

        deep_id = pick("#deep-title", deep)
        type_text("Last nested character!")
        assert view.commit_inline_edit(), messages
        assert events[-1] == ("finish", deep_id, False)
        assert view._draft_settings[deep_id]["text"] == "Last nested character!"
        assert evaluate(deep + ".querySelector('#deep-title').textContent") == "Last nested character!"

        previous_events = len(events)
        view.bridge.inlineEdit(json.dumps(inline_payload(document_token="obsolete document")))
        assert len(events) == previous_events
        print("INLINE_BROWSER_RESULT:" + json.dumps({
            "native_typing_and_immediate_flush": True, "cancel_restores_text": True,
            "enter_commits": True, "plain_text_paste": True, "composition_enter_is_safe": True,
            "nested_anchor_and_svg_preserved": True, "live_controls_unchanged": True,
            "old_frame_finishes_before_next_start": True, "nested_frame_flush": True,
            "stale_document_rejected": True,
        }), flush=True)
    finally:
        view.stop_preview()
        view.close()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        server.shutdown()
        server.server_close()


if __name__ == "__main__" and "--inline-browser" in sys.argv:
    _run_inline_browser()
