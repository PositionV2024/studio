"""Verify the selected project's live edits are saved before publish review.

These cases use production MainWindow methods, real source editors, real Qt
Properties fields, real SettingsStores and the publishing dialog's local scan.
The shared fixture controls only the browser/process endpoints. Dialog.exec is
replaced so no account sign-in or remote action can be selected during the test.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog

from studio.code_editor import CodeEditorDocument
from studio.publish_dialog import PublishDialog
from studio.settings_store import SettingsStore
from test_direct_edit_integration import begin_inline, direct_window, inline_text
from test_visual_edit_integration import (
    select_text, type_pending, visual_edit_app, visual_window,
)


def editor(window, project_name, filename, *, change=True):
    root = window.records[project_name].path
    path = root / filename
    path.write_text("print('saved original')\n", encoding="utf-8")
    document = CodeEditorDocument(path, window, project_path=root)
    document.set_language_mode("plain")
    window.open_editors[path.resolve()] = document
    window.document_tabs.addTab(document, filename)
    if change:
        document.editor.moveCursor(QTextCursor.MoveOperation.End)
        document.editor.insertPlainText(f"# Unsaved {project_name} {filename}\n")
        assert document.is_modified
    return document


@pytest.fixture
def publishing_window(direct_window):
    window = direct_window
    window.alpha_app = editor(window, "Alpha", "app.py")
    window.alpha_helper = editor(window, "Alpha", "helper.py")
    window.alpha_clean = editor(window, "Alpha", "unchanged.py", change=False)
    window.bravo_app = editor(window, "Bravo", "app.py")
    window.document_tabs.setCurrentWidget(window.preview)
    yield window
    for document in window.open_editors.values():
        document.shutdown()


@pytest.fixture
def publish_capture(monkeypatch, visual_edit_app):
    capture = SimpleNamespace(dialogs=[], before_scan=None)

    def review_locally(dialog):
        # This callback runs on entry to exec, before its queued initial scan.
        assert dialog._scan is None
        assert dialog._worker is None
        capture.dialogs.append(dialog)
        if capture.before_scan is not None:
            capture.before_scan(dialog)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            visual_edit_app.processEvents()
            if dialog._scan is not None and dialog._worker is None:
                break
            time.sleep(0.01)
        assert dialog._scan is not None, dialog.status_label.text()
        assert dialog._worker is None
        assert not dialog.publish_button.isEnabled()
        dialog.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(PublishDialog, "exec", review_locally)
    yield capture
    for dialog in capture.dialogs:
        dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    visual_edit_app.processEvents()


def assert_source_unsaved(document):
    assert document.path.read_text(encoding="utf-8") == "print('saved original')\n"
    assert document.is_modified
    assert "Unsaved" in document.editor.toPlainText()


@pytest.mark.parametrize("target, tab_index", [("github", 0), ("vercel", 1), ("streamlit", 2)])
def test_selected_project_code_and_last_preview_character_are_saved_before_review(
    publishing_window, publish_capture, target, tab_index,
):
    window = publishing_window
    beta_store = window._settings_for_project(window.records["Bravo"].path)
    beta_store.set_element_patch("bravo.title", {"text": "Keep Bravo unsaved"})
    clean_before = window.alpha_clean.path.stat()
    begin_inline(window)
    inline_text(window, "Already received text")
    window.preview.queued_inline_text = "Final preview character before publishing!"
    expected = {"hero.title": {"text": "Final preview character before publishing!"}}
    saved_after_flush = []
    for document in (window.alpha_app, window.alpha_helper):
        document.saved.connect(lambda _path: saved_after_flush.append(window._inline_visual_owner is None))

    def check_before_scan(dialog):
        assert dialog.project_path == window.records["Alpha"].path
        assert dialog.target_tabs.currentIndex() == tab_index
        assert window._inline_visual_owner is None
        assert window.preview.queued_inline_text is None
        assert window.current_settings.load_published() == expected
        assert SettingsStore(dialog.project_path).load_published() == expected
        assert not window.current_settings.is_dirty
        for document in (window.alpha_app, window.alpha_helper):
            assert not document.is_modified
            assert document.path.read_text(encoding="utf-8") == document.editor.toPlainText()
        assert_source_unsaved(window.bravo_app)
        assert beta_store.is_dirty
        assert beta_store.load_published() == {}
        assert SettingsStore(window.records["Bravo"].path).load_draft() == {}

    publish_capture.before_scan = check_before_scan
    window.open_publish_dialog(target)
    assert len(publish_capture.dialogs) == 1
    assert saved_after_flush == [True, True]
    scan = publish_capture.dialogs[0]._scan
    scanned = {item.path: item for item in scan.files}
    assert "app.py" in scanned and ".studio/published.json" in scanned
    assert scanned["app.py"].sha256 == hashlib.sha256(window.alpha_app.path.read_bytes()).hexdigest()
    assert scanned[".studio/published.json"].sha256 == hashlib.sha256(window.current_settings.published_path.read_bytes()).hexdigest()
    assert window.alpha_clean.path.stat().st_mtime_ns == clean_before.st_mtime_ns
    assert window.alpha_clean.path.stat().st_ino == clean_before.st_ino
    window.dialogs.critical.assert_not_called()


def test_code_save_failure_keeps_buffers_and_prevents_dialog_or_local_scan(
    publishing_window, publish_capture, monkeypatch,
):
    window = publishing_window
    begin_inline(window)
    inline_text(window, "UI edit remains available")
    replace = os.replace

    def fail_selected_source(source, destination):
        if Path(destination) == window.alpha_app.path:
            raise PermissionError("Selected source file is read-only")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_selected_source)
    window.open_publish_dialog("github")
    assert publish_capture.dialogs == []
    assert window.findChildren(PublishDialog) == []
    assert_source_unsaved(window.alpha_app)
    assert_source_unsaved(window.alpha_helper)
    assert_source_unsaved(window.bravo_app)
    assert window.current_settings.is_dirty
    assert window.current_settings.load_draft()["hero.title"]["text"] == "UI edit remains available"
    assert window.current_settings.load_published() == {}
    assert SettingsStore(window.current_project.path).load_draft() == {}
    window.dialogs.critical.assert_called_once()


@pytest.mark.parametrize("failed_file", ["draft.json", "published.json"])
def test_ui_save_or_publish_failure_prevents_dialog_and_preserves_available_changes(
    publishing_window, publish_capture, monkeypatch, failed_file,
):
    window = publishing_window
    begin_inline(window)
    inline_text(window, "Publish this UI later")
    expected = {"hero.title": {"text": "Publish this UI later"}}
    replace = os.replace
    failed_path = window.current_project.path / ".studio" / failed_file

    def fail_settings(source, destination):
        if Path(destination) == failed_path:
            raise PermissionError("UI settings could not be replaced")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_settings)
    window.open_publish_dialog("vercel")
    assert publish_capture.dialogs == []
    assert window.findChildren(PublishDialog) == []
    assert window.current_settings.load_draft() == expected
    assert window.current_settings.load_published() == {}
    assert window.current_settings.is_dirty == (failed_file == "draft.json")
    assert SettingsStore(window.current_project.path).load_draft() == ({} if failed_file == "draft.json" else expected)
    assert not window.alpha_app.is_modified
    assert not window.alpha_helper.is_modified
    assert_source_unsaved(window.bravo_app)
    window.dialogs.critical.assert_called_once()
    assert "Could not prepare project for publishing" in window.dialogs.critical.call_args.args


def test_unacknowledged_inline_flush_prevents_any_source_save_or_dialog(
    publishing_window, publish_capture,
):
    window = publishing_window
    begin_inline(window)
    inline_text(window, "Acknowledged edit is still here")
    window.preview.queued_inline_text = "Unacknowledged last character!"
    owner_before = window._inline_visual_owner
    window.preview.commit_inline_edit.side_effect = None
    window.preview.commit_inline_edit.return_value = False
    window.open_publish_dialog("streamlit")
    assert publish_capture.dialogs == []
    assert window.findChildren(PublishDialog) == []
    for document in (window.alpha_app, window.alpha_helper, window.bravo_app):
        assert_source_unsaved(document)
    assert window._inline_visual_owner == owner_before
    assert window.preview.queued_inline_text == "Unacknowledged last character!"
    assert window.current_settings.is_dirty
    assert window.current_settings.load_published() == {}
    assert SettingsStore(window.current_project.path).load_draft() == {}
    window.dialogs.warning.assert_called_once()
    window.dialogs.critical.assert_not_called()


def test_focused_properties_value_is_committed_before_publication_review(
    publishing_window, publish_capture,
):
    window = publishing_window
    select_text(window)
    type_pending(window, "Still typing when Publish is pressed")
    assert window.inspector.has_pending_edits

    def check_before_scan(dialog):
        published = json.loads((dialog.project_path / ".studio" / "published.json").read_text())
        assert published["hero.title"]["text"] == "Still typing when Publish is pressed"
        assert not window.inspector.has_pending_edits
        assert not window.current_settings.is_dirty

    publish_capture.before_scan = check_before_scan
    window.open_publish_dialog("github")
    assert len(publish_capture.dialogs) == 1
    assert_source_unsaved(window.bravo_app)
