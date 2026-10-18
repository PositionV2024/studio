"""Verify preview ownership and tab closure using real asynchronous children.

The native browser is replaced by a recording QWidget. ProjectRunner launches
harmless Python children through its production QProcess lifecycle; no user
project, Streamlit port, interactive shell, or WebEngine subprocess is needed.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QTabWidget, QWidget,
)

from studio.main_window import MainWindow
from studio.project_manifest import ProjectManifest, save_manifest
from studio.project_runner import ProjectRunner


class PreviewProbe(QWidget):
    preview_loaded = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.current_preview_url = None
        self.loads = []
        self.stops = 0
        self.placeholders = []
        self.drafts = []

    def load_preview(self, url):
        self.current_preview_url = str(url).rstrip("/") + "/"
        self.loads.append(url)

    def preview_matches(self, url):
        return bool(self.current_preview_url and
                    self.current_preview_url.rstrip("/") == str(url).rstrip("/"))

    def stop_preview(self):
        self.stops += 1
        self.current_preview_url = None

    def show_project_placeholder(self, name, *, starting=False):
        self.current_preview_url = None
        self.placeholders.append((name, starting))

    def set_draft_settings(self, draft):
        self.drafts.append(draft)

    def refresh_preview(self):
        self.loads.append("refresh")


class PreviewWindow(MainWindow):
    def __init__(self, records):
        QMainWindow.__init__(self)
        self.records = {record.id: record for record in records}
        self.current_project = records[0]
        self.current_settings = None
        self.library = SimpleNamespace(get=self.records.get, touch=Mock())
        self.project_explorer = SimpleNamespace(set_project=Mock())
        self.inspector = SimpleNamespace(clear_selection=Mock())
        self.console = SimpleNamespace(append_system_message=Mock(),
                                       set_working_directory=Mock(), show_process=Mock())
        self._preview_user_closed = False
        self.open_editors = {}
        self.document_tabs = QTabWidget(self)
        self.document_tabs.currentChanged.connect(self._document_tab_changed)
        self.preview = PreviewProbe(self.document_tabs)
        self.preview.preview_loaded.connect(self._preview_loaded)
        self.editor = QPlainTextEdit(self)
        self.editor.setPlainText("unsaved editor buffer")
        self.editor.document().setModified(True)
        self.document_tabs.addTab(self.editor, "work.py *")
        self.setCentralWidget(self.document_tabs)
        self.logs = QPlainTextEdit(self)
        self.script_panel = QWidget(self)
        self.bottom_tabs = QTabWidget(self)
        self.bottom_tabs.addTab(self.logs, "Output")
        self.bottom_tabs.addTab(self.script_panel, "Script Test")
        self.edit_action = QAction(self)
        self.edit_action.setCheckable(True)
        self.runner = ProjectRunner(self)
        self.runner._health_timer.setInterval(10_000)
        self.runner.log_received.connect(self._append_log)
        self.runner.state_changed.connect(self._runner_state_changed)
        self.runner.preview_ready.connect(self._preview_ready)
        self.runner.failed.connect(self._show_runner_error)
        self.runner.process_started.connect(self._runner_process_started)
        self.runner.process_stopped.connect(self._runner_process_stopped)
        self.starts = []
        self.finishes = []
        self.runner.process_started.connect(lambda *args: self.starts.append(args))
        self.runner.process_stopped.connect(lambda *args: self.finishes.append(args))

    def show_workspace(self):
        pass

    def _update_project_actions(self):
        pass


@pytest.fixture(scope="session")
def preview_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def preview_window(tmp_path, preview_app, monkeypatch):
    records = []
    for name in ("Alpha", "Bravo", "Charlie"):
        root = tmp_path / name
        root.mkdir()
        (root / "app.py").write_text("# Harmless lifecycle fixture\n", encoding="utf-8")
        save_manifest(root, ProjectManifest(name, python_executable=sys.executable))
        records.append(SimpleNamespace(id=name, name=name, path=root))
    value = PreviewWindow(records)
    value.child_command = "import time; print('child ready', flush=True); time.sleep(30)"
    real_start = value.runner.process.start
    monkeypatch.setattr(value.runner.process, "start", lambda *_args: real_start(
        sys.executable, ["-u", "-c", value.child_command]))
    value.real_start = real_start
    value.error_dialog = Mock(return_value=QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "critical", value.error_dialog)
    monkeypatch.setattr(QMessageBox, "warning", Mock(return_value=QMessageBox.StandardButton.Ok))
    yield value
    value.runner.shutdown()
    value.hide()
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    preview_app.processEvents()


def wait_for(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "Timed out waiting for preview process lifecycle"


def start(window, app):
    starts = len(window.starts)
    window.run_current_project()
    wait_for(app, lambda: len(window.starts) > starts)
    assert window.runner.is_preview_running


def ready(window):
    window.runner._health_timer.stop()
    window.runner._preview_url = "http://127.0.0.1:8501"
    window.runner.preview_ready.emit(str(window.runner.running_project_path),
                                     window.runner.preview_url)


def select(window, name):
    item = QListWidgetItem(name)
    item.setData(Qt.ItemDataRole.UserRole, name)
    window._project_changed(item, None)


def assert_documents_preserved(window):
    index = window.document_tabs.indexOf(window.editor)
    assert index >= 0
    assert window.document_tabs.tabText(index) == "work.py *"
    assert window.editor.toPlainText() == "unsaved editor buffer"
    assert window.editor.document().isModified()
    assert window.bottom_tabs.indexOf(window.script_panel) >= 0


def test_stop_closes_preview_and_cancels_browser_without_touching_documents(
    preview_window, preview_app
):
    window = preview_window
    start(window, preview_app)
    ready(window)
    window.edit_action.setChecked(True)
    window.bottom_tabs.setCurrentWidget(window.script_panel)
    window.runner.stop_project()
    wait_for(preview_app, lambda: bool(window.finishes))
    assert window._preview_tab_index() == -1
    assert window.preview.current_preview_url is None
    assert window.preview.stops >= 1
    assert not window.edit_action.isChecked()
    assert not window._preview_user_closed
    assert window.bottom_tabs.currentWidget() is window.script_panel
    assert_documents_preserved(window)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_normal_or_failed_child_exit_closes_preview_before_error_dialog(
    preview_window, preview_app, exit_code
):
    window = preview_window
    window.child_command = f"raise SystemExit({exit_code})"
    seen_at_error = []
    window.error_dialog.side_effect = lambda *_: seen_at_error.append(window._preview_tab_index())
    window.run_current_project()
    wait_for(preview_app, lambda: bool(window.finishes))
    assert window._preview_tab_index() == -1
    assert window.finishes[-1][1:] == (exit_code, False)
    assert seen_at_error == ([] if exit_code == 0 else [-1])
    assert_documents_preserved(window)


def test_unexpected_process_kill_closes_preview(preview_window, preview_app):
    window = preview_window
    start(window, preview_app)
    ready(window)
    window.runner.process.kill()
    wait_for(preview_app, lambda: bool(window.finishes))
    assert window._preview_tab_index() == -1
    assert window.preview.current_preview_url is None
    assert not window.finishes[-1][2]
    assert_documents_preserved(window)


def test_invalid_manifest_closes_starting_tab_before_error(preview_window):
    window = preview_window
    (window.current_project.path / "app.py").unlink()
    observed = []
    window.error_dialog.side_effect = lambda *_: observed.append(window._preview_tab_index())
    window.run_current_project()
    assert observed == [-1]
    assert window.starts == []
    assert not window.runner.is_running
    assert window._preview_tab_index() == -1


def test_missing_interpreter_discovery_closes_starting_tab(preview_window, monkeypatch):
    window = preview_window
    def unavailable(*_):
        raise RuntimeError("No usable Python interpreter")
    monkeypatch.setattr(window.runner, "_discover_python", unavailable)
    window.run_current_project()
    assert window._preview_tab_index() == -1
    assert not window.runner.is_running
    assert "No usable Python" in window.logs.toPlainText()


def test_failed_to_start_closes_tab_without_finished_signal(
    preview_window, preview_app, monkeypatch, tmp_path
):
    window = preview_window
    monkeypatch.setattr(window.runner.process, "start", window.real_start)
    monkeypatch.setattr(window.runner, "_discover_python", lambda *_: tmp_path / "missing-python")
    window.run_current_project()
    wait_for(preview_app, lambda: window.error_dialog.called)
    assert window._preview_tab_index() == -1
    assert window.finishes == []
    assert not window.runner.is_running


def test_stop_while_launching_missing_interpreter_still_closes_tab(
    preview_window, preview_app, monkeypatch, tmp_path
):
    window = preview_window
    monkeypatch.setattr(window.runner.process, "start", window.real_start)
    monkeypatch.setattr(window.runner, "_discover_python", lambda *_: tmp_path / "missing-python")
    window.run_current_project()
    window.runner.stop_project()
    wait_for(preview_app, lambda: window._preview_tab_index() < 0)
    assert not window.runner.is_running
    assert not window.runner.is_stopping
    assert window.finishes == []


def test_switch_queued_while_old_launch_fails_starts_new_project(
    preview_window, preview_app, monkeypatch, tmp_path
):
    window = preview_window
    missing = tmp_path / "missing-python"
    monkeypatch.setattr(window.runner, "_discover_python", lambda root, _manifest:
                        missing if root.name == "Alpha" else Path(sys.executable))
    monkeypatch.setattr(window.runner.process, "start", lambda program, args:
                        window.real_start(program, args) if program == str(missing)
                        else window.real_start(sys.executable, ["-u", "-c", window.child_command]))
    window.run_current_project()
    select(window, "Bravo")
    window.run_current_project()
    wait_for(preview_app, lambda: bool(window.starts))
    assert window.runner.running_project_name == "Bravo"
    assert window._preview_tab_index() >= 0
    assert window.document_tabs.tabText(window._preview_tab_index()) == "Preview — Bravo"
    ready(window)
    assert window.preview.loads == [window.runner.preview_url]


def test_manual_close_during_startup_ignores_late_ready_and_can_reopen(
    preview_window, preview_app
):
    window = preview_window
    window.run_current_project()
    window._close_workspace_tab(window._preview_tab_index())
    wait_for(preview_app, lambda: bool(window.starts))
    ready(window)
    assert window.runner.is_preview_running
    assert window._preview_tab_index() == -1
    assert window.preview.loads == []
    window.open_running_preview()
    assert window._preview_tab_index() >= 0
    assert window.preview.loads == [window.runner.preview_url]
    assert not window._preview_user_closed
    assert_documents_preserved(window)


def test_selection_during_startup_loads_actual_running_project_without_focus_jump(
    preview_window, preview_app
):
    window = preview_window
    window.run_current_project()
    select(window, "Bravo")
    window.document_tabs.setCurrentWidget(window.editor)
    wait_for(preview_app, lambda: bool(window.starts))
    ready(window)
    assert window.current_project.name == "Bravo"
    assert window.preview.loads == [window.runner.preview_url]
    assert window.document_tabs.tabText(window._preview_tab_index()) == "Preview — Alpha"
    assert window.document_tabs.currentWidget() is window.editor
    assert_documents_preserved(window)


def test_returning_to_running_project_does_not_reload_for_trailing_slash(
    preview_window, preview_app
):
    window = preview_window
    start(window, preview_app)
    ready(window)
    assert window.preview.current_preview_url.endswith("/")
    assert not window.runner.preview_url.endswith("/")
    select(window, "Bravo")
    select(window, "Alpha")
    window.open_running_preview()
    ready(window)
    assert window.preview.loads == [window.runner.preview_url]


def test_restart_closes_old_tab_then_opens_new_preview(preview_window, preview_app):
    window = preview_window
    start(window, preview_app)
    ready(window)
    closed_at_finish = []
    window.runner.process_stopped.connect(lambda *_: closed_at_finish.append(window._preview_tab_index()))
    window.restart_running_project()
    wait_for(preview_app, lambda: len(window.starts) == 2)
    assert closed_at_finish == [-1]
    assert window._preview_tab_index() >= 0
    assert window.document_tabs.tabText(window._preview_tab_index()) == "Preview — Alpha"
    ready(window)
    assert len(window.preview.loads) == 2
    assert_documents_preserved(window)


def test_switch_reopens_for_new_run_and_rejects_stale_owner_or_url(
    preview_window, preview_app
):
    window = preview_window
    start(window, preview_app)
    ready(window)
    select(window, "Bravo")
    window.run_current_project()
    select(window, "Charlie")
    wait_for(preview_app, lambda: len(window.starts) == 2)
    assert window.runner.running_project_name == "Bravo"
    ready(window)
    loads = list(window.preview.loads)
    window.runner.preview_ready.emit(str(window.records["Alpha"].path), window.runner.preview_url)
    window.runner.preview_ready.emit(str(window.records["Bravo"].path), "http://127.0.0.1:9999")
    assert window.preview.loads == loads
    assert window.current_project.name == "Charlie"
    assert window.document_tabs.tabText(window._preview_tab_index()) == "Preview — Bravo"
    assert_documents_preserved(window)


@pytest.mark.parametrize("close_during_restart", [False, True])
def test_explicit_restart_reopens_manual_close_unless_closed_again_while_waiting(
    preview_window, preview_app, close_during_restart
):
    window = preview_window
    start(window, preview_app)
    ready(window)
    if not close_during_restart:
        window._close_workspace_tab(window._preview_tab_index())
    window.restart_running_project()
    if close_during_restart:
        window._close_workspace_tab(window._preview_tab_index())
    wait_for(preview_app, lambda: len(window.starts) == 2)
    ready(window)
    assert (window._preview_tab_index() < 0) is close_during_restart
    assert window._preview_user_closed is close_during_restart


def test_stop_while_starting_does_not_reopen_on_delayed_started(preview_window, preview_app):
    window = preview_window
    window.run_current_project()
    window.runner.stop_project()
    wait_for(preview_app, lambda: bool(window.finishes) or window.error_dialog.called)
    assert window._preview_tab_index() == -1
    assert not window.runner.is_running


def test_loaded_log_requires_current_live_page_and_ignores_after_stop(
    preview_window, preview_app
):
    window = preview_window
    start(window, preview_app)
    ready(window)
    window.preview.preview_loaded.emit("http://127.0.0.1:9999")
    assert "Preview loaded:" not in window.logs.toPlainText()
    url = window.runner.preview_url
    window.preview.preview_loaded.emit(url + "/")
    assert window.logs.toPlainText().count("Preview loaded:") == 1
    window.runner.stop_project()
    window.preview.preview_loaded.emit(url + "/")
    wait_for(preview_app, lambda: bool(window.finishes))
    window.runner.preview_ready.emit(str(window.current_project.path), url)
    assert window.logs.toPlainText().count("Preview loaded:") == 1
    assert window._preview_tab_index() == -1
