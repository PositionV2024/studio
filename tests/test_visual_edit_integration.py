"""Exercise visual Save and exit handling through the real inspector and window.

Only the browser and process endpoints are probes. Inspector input events,
selection changes, per-project SettingsStore instances, Save wiring, undo/redo,
and MainWindow's closeEvent use their production implementations. A separate
development smoke harness covers the real WebEngine/Streamlit path.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QTabWidget, QWidget,
)

from studio.inspector import PropertyInspector
from studio.main_window import MainWindow
from studio.project_library import ProjectLibrary
from studio.settings_store import SettingsStore


ALL_FIELDS = {
    "text", "source", "font_size", "width", "height", "padding", "margin",
    "color", "background_color", "border_color", "border_width",
    "border_radius", "opacity", "visible",
}


def text_selection(element_id="hero.title", text="Original title") -> dict:
    return {
        "id": element_id,
        "label": "Heading",
        "type": "text",
        "tag": "h1",
        "text": text,
        "source": "",
        "computed": {"font_size": 32, "width": 600, "height": 50, "opacity": 1},
        "supported_fields": sorted(ALL_FIELDS - {"source"}),
        "field_reasons": {"source": "Only an image has an image source."},
    }


class PreviewProbe(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.current_preview_url = "http://127.0.0.1:8501/"
        self.patches: list[tuple[str, dict]] = []
        self.drafts: list[dict] = []
        self.stop_preview = Mock(side_effect=self._stop)
        self.set_edit_mode = Mock()
        self.clear_selection = Mock()
        self.show_project_placeholder = Mock()

    def _stop(self):
        self.current_preview_url = None

    def apply_patch(self, element_id, patch):
        self.patches.append((element_id, copy.deepcopy(patch)))

    def set_draft_settings(self, draft):
        self.drafts.append(copy.deepcopy(draft))

    def load_preview(self, url):
        self.current_preview_url = str(url).rstrip("/") + "/"

    def preview_matches(self, url):
        return bool(self.current_preview_url and
                    self.current_preview_url.rstrip("/") == str(url).rstrip("/"))


class RunnerProbe:
    def __init__(self, project):
        self.is_running = True
        self.is_preview_running = True
        self.is_stopping = False
        self.has_pending_start = False
        self.running_project_path = project.path
        self.running_project_name = project.name
        self.preview_url = "http://127.0.0.1:8501"
        self.process_id = 4321
        self.shutdown = Mock(side_effect=self._shutdown)

    def _shutdown(self):
        self.is_running = False
        self.is_preview_running = False
        self.running_project_path = None

    def is_project_running(self, path):
        return self.is_preview_running and self.running_project_path == Path(path)


class VisualEditWindow(MainWindow):
    """Production visual-edit workflow with no network or child processes."""

    def __init__(self, tmp_path):
        QMainWindow.__init__(self)
        self.library = ProjectLibrary(tmp_path / "projects.sqlite")
        self.records = {}
        for name in ("Alpha", "Bravo"):
            root = tmp_path / name
            root.mkdir()
            self.records[name] = self.library.add_or_update(
                name=name, path=root, entry_point="app.py", preview_port=8501,
            )
        self.current_project = self.records["Alpha"]
        self.current_settings = None
        self._visual_settings = {}
        self._visual_project_names = {}
        self._visual_selection_project = None
        self._closing_approved = False
        self.open_editors = {}
        self._preview_user_closed = False
        self.runner = RunnerProbe(self.current_project)
        self.script_runner = SimpleNamespace(shutdown=Mock())
        self.console = SimpleNamespace(
            append_system_message=Mock(), set_working_directory=Mock(), shutdown=Mock(),
        )
        self.project_explorer = SimpleNamespace(set_project=Mock())
        self.logs = QPlainTextEdit(self)
        self.document_tabs = QTabWidget(self)
        self.preview = PreviewProbe(self.document_tabs)
        self.document_tabs.addTab(self.preview, "Preview — Alpha")
        self.inspector = PropertyInspector(self)
        self.inspector.patch_changed.connect(self._property_changed)
        self.inspector.save_requested.connect(self.save_visual_changes)
        self.inspector.pending_edits_changed.connect(
            lambda _pending: self._update_visual_save_state()
        )
        self.edit_action = QAction(self)
        self.edit_action.setCheckable(True)
        self.edit_action.toggled.connect(self._toggle_edit_mode)
        self.edit_action.setChecked(True)
        central = QWidget(self)
        layout = QHBoxLayout(central)
        layout.addWidget(self.document_tabs, 2)
        layout.addWidget(self.inspector, 1)
        self.setCentralWidget(central)
        self.resize(1100, 940)
        self._can_close_all_editors = Mock(return_value=True)
        self._save_workspace_layout = Mock()
        self.current_settings = self._settings_for_project(self.current_project.path)
        self._update_visual_save_state()

    def show_workspace(self):
        self.centralWidget().show()

    def _update_project_actions(self):
        self._update_visual_save_state()


@pytest.fixture(scope="session")
def visual_edit_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def visual_window(tmp_path, visual_edit_app, monkeypatch):
    window = VisualEditWindow(tmp_path)
    window.dialogs = SimpleNamespace(
        question=Mock(return_value=QMessageBox.StandardButton.Cancel),
        exit_choice=QMessageBox.StandardButton.Cancel,
        exit_prompts=[],
        critical=Mock(return_value=QMessageBox.StandardButton.Ok),
        information=Mock(return_value=QMessageBox.StandardButton.Ok),
        warning=Mock(return_value=QMessageBox.StandardButton.Ok),
    )
    for name in ("question", "critical", "information", "warning"):
        monkeypatch.setattr(QMessageBox, name, getattr(window.dialogs, name))
    def record_exit_prompt(dialog):
        window.dialogs.exit_prompts.append({
            "title": dialog.windowTitle(),
            "text": dialog.text(),
            "details": dialog.informativeText(),
            "buttons": dialog.standardButtons(),
            "save_label": dialog.button(QMessageBox.StandardButton.Save).text(),
        })
        return window.dialogs.exit_choice
    monkeypatch.setattr(QMessageBox, "exec", record_exit_prompt)
    window.assist_shutdown = Mock()
    monkeypatch.setattr("studio.main_window.shutdown_python_assist", window.assist_shutdown)
    window.show()
    window.activateWindow()
    visual_edit_app.processEvents()
    yield window
    window.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    visual_edit_app.processEvents()


def disk_draft(window, project="Alpha"):
    path = window.records[project].path / ".studio" / "draft.json"
    return json.loads(path.read_text(encoding="utf-8"))


def select_project(window, name):
    record = window.records[name]
    item = QListWidgetItem(name)
    item.setData(Qt.ItemDataRole.UserRole, record.id)
    window._project_changed(item, None)


def select_text(window, text="Original title", element_id="hero.title"):
    window._element_selected(text_selection(element_id, text))


def type_pending(window, text):
    field = window.inspector.text
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, text)
    assert field.hasFocus(), "Test must leave the edited field focused"
    assert window.inspector.has_pending_edits


def click_save(window):
    assert window.inspector.save_button.isEnabled()
    QTest.mouseClick(window.inspector.save_button, Qt.MouseButton.LeftButton)


def assert_no_shutdown(window):
    window._save_workspace_layout.assert_not_called()
    window.runner.shutdown.assert_not_called()
    window.script_runner.shutdown.assert_not_called()
    window.console.shutdown.assert_not_called()
    window.assist_shutdown.assert_not_called()
    assert window.runner.is_running


def assert_save_discard_cancel_prompt(window):
    assert len(window.dialogs.exit_prompts) == 1
    prompt = window.dialogs.exit_prompts[0]
    text = prompt["text"] + "\n" + prompt["details"]
    buttons = prompt["buttons"]
    for button in (QMessageBox.StandardButton.Save,
                   QMessageBox.StandardButton.Discard,
                   QMessageBox.StandardButton.Cancel):
        assert buttons & button, f"Exit prompt must offer {button.name}"
    return text


def test_save_button_commits_latest_focused_text_and_reopens_from_disk(visual_window):
    window = visual_window
    select_text(window)
    original = disk_draft(window)
    type_pending(window, "Latest heading before Save")
    assert disk_draft(window) == original
    assert "unsaved" in window.inspector.save_status.text().lower()
    click_save(window)
    assert disk_draft(window)["hero.title"]["text"] == "Latest heading before Save"
    assert not window.current_settings.is_dirty
    assert not window.inspector.has_pending_edits
    assert "unsaved" not in window.inspector.save_status.text().lower()
    reopened = SettingsStore(window.current_project.path)
    assert reopened.load_draft() == window.current_settings.load_draft()
    window.dialogs.critical.assert_not_called()


@pytest.mark.parametrize("field_name, typed_value, patch_key, expected", [
    ("font_size", "44", "font_size", 44),
    ("text_color", "#2468ac", "color", "#2468ac"),
])
def test_save_commits_focused_number_and_colour_controls(
    visual_window, field_name, typed_value, patch_key, expected,
):
    window = visual_window
    select_text(window)
    control = getattr(window.inspector, field_name)
    field = control.lineEdit() if field_name == "font_size" else control.edit
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, typed_value)
    assert field.hasFocus()
    assert disk_draft(window) == {}
    click_save(window)
    assert disk_draft(window)["hero.title"][patch_key] == expected
    assert not window.current_settings.is_dirty
    assert not window.inspector.has_pending_edits


def test_live_changes_are_buffered_and_visual_undo_redo_updates_save_state(visual_window):
    window = visual_window
    select_text(window)
    original = disk_draft(window)
    window._property_changed("hero.title", {"font_size": 48})
    assert window.preview.patches[-1] == ("hero.title", {"font_size": 48})
    assert disk_draft(window) == original
    assert window.current_settings.is_dirty
    window.preview.setFocus()
    window.undo()
    assert not window.current_settings.is_dirty
    assert window.current_settings.load_draft() == original
    window.redo()
    assert window.current_settings.is_dirty
    assert window.current_settings.load_draft()["hero.title"]["font_size"] == 48
    assert disk_draft(window) == original
    click_save(window)
    assert disk_draft(window)["hero.title"]["font_size"] == 48
    window.preview.setFocus()
    window.undo()
    assert window.current_settings.is_dirty
    window.redo()
    assert not window.current_settings.is_dirty


def test_changing_selected_element_commits_text_to_the_original_element(visual_window):
    window = visual_window
    select_text(window)
    type_pending(window, "First element has this text")
    select_text(window, "Other heading", "hero.subtitle")
    assert window.current_settings.load_draft()["hero.title"]["text"] == "First element has this text"
    assert "hero.subtitle" not in window.current_settings.load_draft()
    type_pending(window, "Second element has other text")
    click_save(window)
    assert disk_draft(window) == {
        "hero.title": {"text": "First element has this text"},
        "hero.subtitle": {"text": "Second element has other text"},
    }


def test_switching_projects_retains_unsaved_changes_in_the_original_project(visual_window):
    window = visual_window
    select_text(window)
    type_pending(window, "Unsaved Alpha heading")
    alpha_store = window.current_settings
    select_project(window, "Bravo")
    assert window.current_project.name == "Bravo"
    assert alpha_store.is_dirty
    assert alpha_store.load_draft()["hero.title"]["text"] == "Unsaved Alpha heading"
    assert disk_draft(window, "Alpha") == {}
    assert not window.current_settings.is_dirty
    select_project(window, "Alpha")
    assert window.current_settings is alpha_store
    assert window.inspector.save_button.isEnabled()
    click_save(window)
    assert disk_draft(window, "Alpha")["hero.title"]["text"] == "Unsaved Alpha heading"
    assert disk_draft(window, "Bravo") == {}


@pytest.mark.parametrize("stopped", [False, True], ids=["manual-tab-close", "server-stop"])
def test_save_stays_available_when_preview_closes(visual_window, stopped):
    window = visual_window
    select_text(window)
    type_pending(window, "Keep after preview closes")
    if stopped:
        window.runner._shutdown()
        window._runner_process_stopped("Alpha", 0, True)
    else:
        window._close_workspace_tab(window._preview_tab_index())
    assert window._preview_tab_index() < 0
    assert not window.inspector.element_id
    assert window.inspector.save_button.isEnabled()
    assert window.current_settings.is_dirty
    click_save(window)
    assert disk_draft(window)["hero.title"]["text"] == "Keep after preview closes"


def test_reopening_live_preview_reapplies_unsaved_working_changes(visual_window):
    window = visual_window
    select_text(window)
    window._property_changed("hero.title", {"text": "Working preview title"})
    window._close_workspace_tab(window._preview_tab_index())
    window.open_running_preview()
    assert window._preview_tab_index() >= 0
    assert window.preview.drafts[-1]["hero.title"]["text"] == "Working preview title"
    assert window.current_settings.is_dirty
    assert disk_draft(window) == {}


def test_unavailable_image_source_is_hidden_and_cannot_emit_patch(visual_window):
    window = visual_window
    select_text(window)
    inspector = window.inspector
    marker = inspector.field_markers["source"]
    assert marker.text() == ""
    assert marker.isHidden()
    assert inspector.field_rows["source"].isHidden()
    assert inspector.field_rows["source"].cursor().shape() == Qt.CursorShape.ForbiddenCursor
    assert inspector.source.isReadOnly()
    assert inspector.field_rows["source"].toolTip()
    assert inspector.field_markers["text"].text() == ""
    before = window.current_settings.load_draft()
    emitted = len(window.preview.patches)
    QTest.keyClicks(inspector.source, "unavailable.png")
    inspector._emit("source", "unavailable.png")
    inspector.commit_pending_edits()
    assert inspector.source.text() == ""
    assert window.current_settings.load_draft() == before
    assert len(window.preview.patches) == emitted


def test_exit_cancel_commits_focused_text_then_keeps_window_and_processes(visual_window):
    window = visual_window
    select_text(window)
    type_pending(window, "Do not lose this focused edit")
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    assert "Alpha" in assert_save_discard_cancel_prompt(window)
    assert window.current_settings.load_draft()["hero.title"]["text"] == "Do not lose this focused edit"
    assert window.current_settings.is_dirty
    assert disk_draft(window) == {}
    assert_no_shutdown(window)


@pytest.mark.parametrize("choice", [QMessageBox.StandardButton.Save, QMessageBox.StandardButton.Discard])
def test_exit_save_or_discard_covers_dirty_projects_after_selection_changes(visual_window, choice):
    window = visual_window
    select_text(window)
    window._property_changed("hero.title", {"text": "Alpha pending"})
    alpha = window.current_settings
    select_project(window, "Bravo")
    # The fixture's active project is moved to Bravo so a real selection is
    # editable there; Alpha remains a dirty cached project for the exit prompt.
    window.runner.running_project_path = window.current_project.path
    window.runner.running_project_name = "Bravo"
    window.edit_action.setChecked(True)
    select_text(window)
    type_pending(window, "Bravo pending")
    bravo = window.current_settings
    window.dialogs.exit_choice = choice
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    prompt = assert_save_discard_cancel_prompt(window)
    assert "Alpha" in prompt and "Bravo" in prompt
    if choice == QMessageBox.StandardButton.Save:
        assert disk_draft(window, "Alpha")["hero.title"]["text"] == "Alpha pending"
        assert disk_draft(window, "Bravo")["hero.title"]["text"] == "Bravo pending"
        assert not alpha.is_dirty
        assert not bravo.is_dirty
    else:
        assert disk_draft(window, "Alpha") == {}
        assert disk_draft(window, "Bravo") == {}
        assert SettingsStore(window.records["Alpha"].path).load_draft() == {}
        assert SettingsStore(window.records["Bravo"].path).load_draft() == {}
    window.runner.shutdown.assert_called_once()
    window.script_runner.shutdown.assert_called_once()
    window.console.shutdown.assert_called_once()


@pytest.mark.parametrize("error", [PermissionError("The folder is read-only"),
                                  ValueError("Invalid settings payload")])
def test_save_failure_keeps_edits_and_allows_a_successful_retry(visual_window, monkeypatch, error):
    window = visual_window
    select_text(window)
    type_pending(window, "Preserve me if Save fails")
    store = window.current_settings
    real_save = store.save_draft
    failing_save = Mock(side_effect=error)
    monkeypatch.setattr(store, "save_draft", failing_save)
    click_save(window)
    failing_save.assert_called_once()
    assert store.is_dirty
    assert disk_draft(window) == {}
    assert window.inspector.save_button.isEnabled()
    window.dialogs.critical.assert_called_once()
    assert window.runner.is_running
    monkeypatch.setattr(store, "save_draft", real_save)
    click_save(window)
    assert disk_draft(window)["hero.title"]["text"] == "Preserve me if Save fails"
    assert not store.is_dirty


def test_exit_save_failure_aborts_shutdown_without_discarding_changes(visual_window, monkeypatch):
    window = visual_window
    select_text(window)
    window._property_changed("hero.title", {"text": "Must survive failed exit Save"})
    window.dialogs.exit_choice = QMessageBox.StandardButton.Save
    monkeypatch.setattr(window.current_settings, "save_draft",
                        Mock(side_effect=PermissionError("Draft is locked")))
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    assert window.current_settings.is_dirty
    assert disk_draft(window) == {}
    window.dialogs.critical.assert_called_once()
    assert_no_shutdown(window)


def test_code_buffer_cancel_does_not_offer_visual_exit_or_stop_processes(visual_window):
    window = visual_window
    select_text(window)
    window._property_changed("hero.title", {"text": "Visual draft"})
    window._can_close_all_editors.return_value = False
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    window.dialogs.question.assert_not_called()
    assert window.dialogs.exit_prompts == []
    assert_no_shutdown(window)


def test_clean_exit_does_not_show_unsaved_visual_warning(visual_window):
    window = visual_window
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    window.dialogs.question.assert_not_called()
    assert window.dialogs.exit_prompts == []
    window.runner.shutdown.assert_called_once()


def test_publish_commits_latest_focused_visual_edit(visual_window):
    window = visual_window
    select_text(window)
    type_pending(window, "Latest published heading")
    window.publish()
    assert disk_draft(window)["hero.title"]["text"] == "Latest published heading"
    assert window.current_settings.load_published()["hero.title"]["text"] == "Latest published heading"
    assert not window.current_settings.is_dirty
    assert not window.inspector.has_pending_edits
    window.dialogs.information.assert_called_once()
