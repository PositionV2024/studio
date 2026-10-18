"""Exercise Test Script through the real editor, window wiring and child Python.

The preview widget is replaced with an ordinary QWidget so these tests do not
start WebEngine, a Streamlit server or an interactive shell. The production
toolbar, document methods, Script Test panel and script process are unchanged.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QProcess, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QWidget,
)

from studio.code_editor import CodeEditorDocument, shutdown_python_assist
from studio.main_window import MainWindow
from studio.project_explorer import ProjectExplorer
from studio.project_manifest import ProjectManifest, save_manifest
from studio.project_runner import ProjectRunner


class ScriptEditorWindow(MainWindow):
    """Real document/actions/panel wiring with no preview or shell processes."""

    def __init__(self, root: Path, settings_path: Path) -> None:
        QMainWindow.__init__(self)
        self.ui_settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
        self.current_project = SimpleNamespace(path=root)
        self.current_settings = None
        self.runner = ProjectRunner(self)
        self.open_editors: dict[Path, CodeEditorDocument] = {}
        self._preview_user_closed = False

        self.project_explorer = ProjectExplorer(self)
        self.project_explorer.set_project(root)
        self.project_explorer.file_opened.connect(self._open_file_in_editor)
        self.document_tabs = QTabWidget(self)
        self.preview = QWidget(self.document_tabs)
        self.preview.stop_preview = Mock()
        self.document_tabs.addTab(self.preview, "Preview")
        self.document_tabs.currentChanged.connect(self._document_tab_changed)

        self.logs = QPlainTextEdit(self)
        self.logs.setPlainText("Existing preview output stays here.\n")
        self.console = QWidget(self)
        self.console.shutdown = Mock()
        self.bottom_tabs = QTabWidget(self)
        self.bottom_tabs.addTab(self.logs, "Output")
        self.bottom_tabs.addTab(self.console, "Console")
        self._setup_script_testing()

        self.center_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.center_splitter.addWidget(self.document_tabs)
        self.center_splitter.addWidget(self.bottom_tabs)
        self.center_splitter.setSizes([550, 250])
        center = QWidget(self)
        layout = QHBoxLayout(center)
        layout.addWidget(self.project_explorer, 1)
        layout.addWidget(self.center_splitter, 3)
        self.setCentralWidget(center)
        self._create_toolbar()
        self.resize(1300, 850)

        self.script_starts: list[tuple[str, int]] = []
        self.script_finishes: list[tuple[str, int, bool]] = []
        self.script_runner.started.connect(
            lambda path, pid: self.script_starts.append((path, pid))
        )
        self.script_runner.finished.connect(
            lambda path, code, stopped: self.script_finishes.append((path, code, stopped))
        )

    def show_workspace(self) -> None:
        self.centralWidget().show()


@pytest.fixture(scope="session")
def script_editor_app():
    app = QApplication.instance() or QApplication([])
    yield app
    shutdown_python_assist()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def script_window(tmp_path: Path, script_editor_app, monkeypatch):
    root = tmp_path / "project with spaces"
    root.mkdir()
    save_manifest(root, ProjectManifest("Script tests", python_executable=sys.executable))
    value = ScriptEditorWindow(root, tmp_path / "studio-ui.ini")
    value.dialogs = SimpleNamespace(
        critical=Mock(return_value=QMessageBox.StandardButton.Ok),
        warning=Mock(return_value=QMessageBox.StandardButton.Ok),
        question=Mock(return_value=QMessageBox.StandardButton.Cancel),
    )
    for name in ("critical", "warning", "question"):
        monkeypatch.setattr(QMessageBox, name, getattr(value.dialogs, name))
    value.show()
    value.activateWindow()
    script_editor_app.processEvents()
    yield value
    value.script_runner.shutdown()
    value.runner.shutdown()
    for document in list(value.open_editors.values()):
        document.shutdown()
    value.hide()
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    script_editor_app.processEvents()


def _wait_for(app, predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "Timed out waiting for the script/editor workflow"


def _open(window: ScriptEditorWindow, relative_path: str, code: str) -> CodeEditorDocument:
    path = window.current_project.path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    window._open_file_in_editor(path)
    return window.open_editors[path.resolve()]


def _edit(document: CodeEditorDocument, code: str) -> None:
    document.editor.selectAll()
    document.editor.insertPlainText(code)
    document.editor.hide_completions()
    assert document.is_modified


def _click_test(window: ScriptEditorWindow, document: CodeEditorDocument) -> None:
    assert window.document_tabs.currentWidget() is document
    assert document.test_script_button.isVisible()
    assert document.test_script_button.isEnabled()
    QTest.mouseClick(document.test_script_button, Qt.MouseButton.LeftButton)


def _finished(window, app, count=1) -> None:
    _wait_for(app, lambda: len(window.script_finishes) >= count and not window.script_runner.is_running)


def test_button_saves_only_selected_modified_script_and_runs_latest_buffer(
    script_window, script_editor_app
):
    window = script_window
    first = _open(window, "selected.py", "print('old disk version')\n")
    second = _open(window, "unrelated.py", "print('unrelated disk version')\n")
    _edit(second, "print('keep this unsaved')\n")
    # Select the first document using the real tab bar, then edit and click.
    tab = window.document_tabs.tabBar()
    QTest.mouseClick(tab, Qt.MouseButton.LeftButton,
                     pos=tab.tabRect(window.document_tabs.indexOf(first)).center())
    latest = "print('latest selected buffer')\n"
    _edit(first, latest)
    _click_test(window, first)
    _finished(window, script_editor_app)

    assert first.path.read_text(encoding="utf-8") == latest
    assert not first.is_modified
    assert second.path.read_text(encoding="utf-8") == "print('unrelated disk version')\n"
    assert second.is_modified
    panel = window.script_test_panel
    assert window.bottom_tabs.currentWidget() is panel
    assert window.bottom_tabs.tabText(window.bottom_tabs.indexOf(panel)) == "Script Test"
    assert panel.isVisible()
    assert "latest selected buffer" in panel.output.toPlainText()
    assert "old disk version" not in panel.output.toPlainText()
    assert window.script_finishes == [(str(first.path), 0, False)]
    assert first.path.name in panel.file_label.text()
    window.dialogs.critical.assert_not_called()


def test_clean_script_runs_without_rewriting_the_file(
    script_window, script_editor_app, monkeypatch
):
    window = script_window
    document = _open(window, "clean.py", "print('clean run')\n")
    save = Mock(wraps=document.save)
    monkeypatch.setattr(document, "save", save)
    before = document.path.stat().st_mtime_ns
    _click_test(window, document)
    _finished(window, script_editor_app)
    save.assert_not_called()
    assert document.path.stat().st_mtime_ns == before
    assert "clean run" in window.script_test_panel.output.toPlainText()


@pytest.mark.parametrize("failure", ["write_error", "cancelled_save"])
def test_unsaved_script_is_never_started_if_save_fails(
    script_window, script_editor_app, monkeypatch, failure
):
    window = script_window
    document = _open(window, "must_not_run.py", "print('stale disk version')\n")
    latest = "print('unsaved version')\n"
    _edit(document, latest)
    if failure == "cancelled_save":
        save = Mock(return_value=False)
        monkeypatch.setattr(document, "save", save)
    else:
        def denied(*_args, **_kwargs):
            raise PermissionError("The script is temporarily read-only")
        monkeypatch.setattr("studio.code_editor.os.replace", denied)

    _click_test(window, document)
    script_editor_app.processEvents()

    assert not window.script_runner.is_running
    assert window.script_starts == []
    assert window.script_finishes == []
    assert document.path.read_text(encoding="utf-8") == "print('stale disk version')\n"
    assert document.editor.toPlainText() == latest
    assert document.is_modified
    if failure == "cancelled_save":
        save.assert_called_once_with()
    else:
        window.dialogs.critical.assert_called_once()


def test_open_tab_keeps_its_file_project_and_python_after_sidebar_changes(
    script_window, script_editor_app, tmp_path
):
    window = script_window
    first_root = window.current_project.path
    (first_root / "marker.txt").write_text("original project", encoding="utf-8")
    code = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "import sibling\n"
        "print('CONTEXT=' + json.dumps({\n"
        "    'file': __file__, 'cwd': os.getcwd(), 'python': sys.executable,\n"
        "    'prefix': sys.prefix, 'marker': Path('marker.txt').read_text(),\n"
        "    'sibling': sibling.VALUE,\n"
        "}))\n"
    )
    document = _open(window, "nested/check context.py", code)
    document.path.with_name("sibling.py").write_text("VALUE = 'near selected script'\n", encoding="utf-8")
    expected_python = document.script_python_executable
    assert expected_python == sys.executable

    second_root = tmp_path / "another project"
    second_root.mkdir()
    save_manifest(second_root, ProjectManifest("Other", python_executable="/missing/other/python"))
    (second_root / "marker.txt").write_text("wrong project", encoding="utf-8")
    window.current_project = SimpleNamespace(path=second_root)
    window.project_explorer.set_project(second_root)
    _open(window, "other.py", "raise AssertionError('wrong script')\n")
    window.document_tabs.setCurrentWidget(document)
    # A changed manifest also must not silently change this already-open tab.
    save_manifest(first_root, ProjectManifest("Changed", python_executable="/missing/new/python"))

    _click_test(window, document)
    _finished(window, script_editor_app)
    lines = window.script_test_panel.output.toPlainText().splitlines()
    context = json.loads(next(line.removeprefix("CONTEXT=") for line in lines if line.startswith("CONTEXT=")))
    assert Path(context["file"]) == document.path
    assert Path(context["cwd"]) == first_root
    assert Path(context["python"]) == Path(expected_python)
    assert Path(context["prefix"]) == Path(sys.prefix)
    assert context["marker"] == "original project"
    assert context["sibling"] == "near selected script"
    assert window.script_finishes[-1] == (str(document.path), 0, False)


@pytest.mark.parametrize("filename", ["dwd", "notes.txt"])
def test_language_choice_gates_execution_and_supports_extensionless_python(
    script_window, script_editor_app, filename
):
    window = script_window
    assert not window.test_script_action.isEnabled()  # Preview is selected.
    document = _open(window, filename, "print('language override executed')\n")
    original = document.path.read_bytes()
    assert document.test_script_button.isVisible()
    assert not document.test_script_button.isEnabled()
    assert not window.test_script_action.isEnabled()
    assert window.test_current_script() is False
    assert not window.script_starts

    document.language_selector.setCurrentIndex(document.language_selector.findData("python"))
    assert document.test_script_button.isEnabled()
    assert window.test_script_action.isEnabled()
    _click_test(window, document)
    _finished(window, script_editor_app)
    assert "language override executed" in window.script_test_panel.output.toPlainText()
    assert document.path.read_bytes() == original
    assert not document.path.with_suffix(".py").exists()

    document.language_selector.setCurrentIndex(document.language_selector.findData("plain"))
    assert not document.test_script_button.isEnabled()
    assert not window.test_script_action.isEnabled()
    assert not document.is_modified


def test_output_traceback_and_exit_status_use_the_dedicated_panel(
    script_window, script_editor_app
):
    window = script_window
    previous_output = window.logs.toPlainText()
    document = _open(window, "failing.py", (
        "import sys\n"
        "print('normal output', flush=True)\n"
        "print('stderr output', file=sys.stderr, flush=True)\n"
        "raise ValueError('expected test traceback')\n"
    ))
    window.bottom_tabs.hide()
    _click_test(window, document)
    _finished(window, script_editor_app)
    panel = window.script_test_panel
    output = panel.output.toPlainText()
    assert panel.isVisible() and window.bottom_tabs.currentWidget() is panel
    assert "normal output" in output
    assert "stderr output" in output
    assert "Traceback (most recent call last)" in output
    assert "ValueError: expected test traceback" in output
    assert "1" in panel.status_label.text()
    assert window.script_finishes[-1] == (str(document.path), 1, False)
    assert window.logs.toPlainText() == previous_output
    assert window.runner.process.state() == QProcess.ProcessState.NotRunning
    assert not window.runner.is_running
    window.dialogs.critical.assert_not_called()
    window.dialogs.warning.assert_not_called()


def test_stop_remains_available_after_tab_change_and_running_tab_close(
    script_window, script_editor_app
):
    window = script_window
    document = _open(window, "waiting.py", "import time\nprint('ready for stop', flush=True)\ntime.sleep(30)\n")
    path = document.path
    _click_test(window, document)
    _wait_for(script_editor_app, lambda: "ready for stop" in window.script_test_panel.output.toPlainText())
    assert window.script_runner.is_running
    assert not document.test_script_button.isEnabled()

    other = _open(window, "second.py", "print('second disk version')\n")
    _edit(other, "print('second unsaved version')\n")
    assert not other.test_script_button.isEnabled()
    assert not window.test_script_action.isEnabled()
    assert window.test_current_script() is False
    assert other.is_modified
    assert other.path.read_text(encoding="utf-8") == "print('second disk version')\n"
    assert window.script_runner.script_path == path

    window.document_tabs.setCurrentWidget(window.preview)
    window._close_workspace_tab(window.document_tabs.indexOf(document))
    script_editor_app.processEvents()
    assert path not in window.open_editors
    assert window.script_runner.is_running
    assert window.stop_script_test_action.isEnabled()
    assert window.script_test_panel.stop_button.isVisible()
    assert window.script_test_panel.stop_button.isEnabled()
    QTest.mouseClick(window.script_test_panel.stop_button, Qt.MouseButton.LeftButton)
    _finished(window, script_editor_app)
    assert window.script_finishes[-1][0] == str(path)
    assert window.script_finishes[-1][2] is True
    assert "stop" in window.script_test_panel.status_label.text().lower()
    assert not window.stop_script_test_action.isEnabled()


def test_f7_runs_the_active_file_and_shift_f7_stops_without_opening_the_menu(
    script_window, script_editor_app
):
    window = script_window
    document = _open(window, "shortcut.py", "import time\nprint('shortcut ready', flush=True)\ntime.sleep(30)\n")
    assert window.refresh_action.shortcut().toString() == "F6"
    document.editor.setFocus()
    window.activateWindow()
    script_editor_app.processEvents()
    QTest.keyClick(document.editor, Qt.Key.Key_F7)
    _wait_for(script_editor_app, lambda: "shortcut ready" in window.script_test_panel.output.toPlainText())
    window.document_tabs.setCurrentWidget(window.preview)
    window.script_test_panel.output.setFocus()
    QTest.keyClick(window.script_test_panel.output, Qt.Key.Key_F7, Qt.KeyboardModifier.ShiftModifier)
    _finished(window, script_editor_app)
    assert window.script_finishes[-1][2] is True
    assert window.runner.process.state() == QProcess.ProcessState.NotRunning


@pytest.mark.parametrize("submit", ["button", "enter"])
def test_input_prompt_accepts_text_and_end_input_releases_stdin(
    script_window, script_editor_app, submit
):
    window = script_window
    document = _open(window, "interactive.py", (
        "import sys\n"
        "name = input('Your name? ')\n"
        "print('HELLO:' + name, flush=True)\n"
        "remainder = sys.stdin.read()\n"
        "print('EOF:' + str(len(remainder)))\n"
    ))
    _click_test(window, document)
    panel = window.script_test_panel
    _wait_for(script_editor_app, lambda: "Your name?" in panel.output.toPlainText())
    assert panel.input_edit.isEnabled()
    assert panel.send_button.isEnabled()
    QTest.keyClicks(panel.input_edit, "Clarence")
    if submit == "button":
        QTest.mouseClick(panel.send_button, Qt.MouseButton.LeftButton)
    else:
        QTest.keyClick(panel.input_edit, Qt.Key.Key_Return)
    _wait_for(script_editor_app, lambda: "HELLO:Clarence" in panel.output.toPlainText())
    assert panel.input_edit.text() == ""
    QTest.mouseClick(panel.eof_button, Qt.MouseButton.LeftButton)
    _finished(window, script_editor_app)
    assert "EOF:0" in panel.output.toPlainText()
    assert window.script_finishes[-1][1:] == (0, False)
    assert not panel.input_edit.isEnabled()
    assert not panel.send_button.isEnabled()
    assert not panel.eof_button.isEnabled()


def test_invalid_configured_python_reports_failure_without_running_in_studio_python(
    script_window, script_editor_app
):
    window = script_window
    missing = str(window.current_project.path / "missing environment" / "python")
    save_manifest(window.current_project.path, ProjectManifest("Missing Python", python_executable=missing))
    marker = window.current_project.path / "must-not-exist.txt"
    document = _open(window, "wrong_environment.py", (
        "from pathlib import Path\n"
        "Path('must-not-exist.txt').write_text('incorrect fallback')\n"
    ))
    assert document.script_python_executable == missing
    _click_test(window, document)
    script_editor_app.processEvents()
    assert not window.script_runner.is_running
    assert window.script_starts == []
    assert not marker.exists()
    assert "fail" in window.script_test_panel.status_label.text().lower()
    assert "python" in window.script_test_panel.output.toPlainText().lower()
    assert document.test_script_button.isEnabled()


def test_application_close_stops_active_script_and_preserves_other_cleanup(
    script_window, script_editor_app
):
    window = script_window
    document = _open(window, "close_wait.py", "import time\nprint('close ready', flush=True)\ntime.sleep(30)\n")
    _click_test(window, document)
    _wait_for(script_editor_app, lambda: "close ready" in window.script_test_panel.output.toPlainText())
    assert window.script_runner.is_running
    assert window.close()
    script_editor_app.processEvents()
    assert not window.script_runner.is_running
    assert window.script_finishes[-1][2] is True
    window.console.shutdown.assert_called_once_with()
    assert window.runner.process.state() == QProcess.ProcessState.NotRunning
    window.dialogs.question.assert_not_called()
