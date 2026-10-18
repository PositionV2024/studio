"""Cover the user's extensionless-file workflow through the real UI components."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMainWindow, QPushButton, QTabWidget, QWidget

from studio.code_editor import CodeEditorDocument, shutdown_python_assist
from studio.main_window import MainWindow
from studio.project_explorer import NewFileDialog, ProjectExplorer
from studio.project_runner import ProjectRunner


class FileEditorWindow(MainWindow):
    """Keep production file/document methods without starting the WebEngine pane."""

    def __init__(self, root: Path, settings_path: Path) -> None:
        QMainWindow.__init__(self)
        self.ui_settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
        self.current_project = SimpleNamespace(path=root)
        self.current_settings = None
        self.runner = SimpleNamespace(_discover_python=ProjectRunner._discover_python)
        self.open_editors: dict[Path, CodeEditorDocument] = {}
        self.project_explorer = ProjectExplorer(self)
        self.project_explorer.set_project(root)
        self.project_explorer.file_opened.connect(self._open_file_in_editor)
        self.document_tabs = QTabWidget(self)
        self.preview = QWidget(self.document_tabs)
        self.document_tabs.addTab(self.preview, "Preview")
        center = QWidget(self)
        layout = QHBoxLayout(center)
        layout.addWidget(self.project_explorer, 1)
        layout.addWidget(self.document_tabs, 3)
        self.setCentralWidget(center)
        self.resize(1300, 800)

    def _update_project_actions(self) -> None:
        pass

    def show_workspace(self) -> None:
        pass


@pytest.fixture(scope="session")
def file_editor_app():
    app = QApplication.instance() or QApplication([])
    yield app
    shutdown_python_assist()


@pytest.fixture
def file_windows(tmp_path: Path, file_editor_app):
    root = tmp_path / "project"
    root.mkdir()
    settings_path = tmp_path / "studio-ui.ini"
    windows = []

    def create():
        value = FileEditorWindow(root, settings_path)
        windows.append(value)
        value.show()
        value.activateWindow()
        file_editor_app.processEvents()
        return value

    yield create
    for window in windows:
        for document in list(window.open_editors.values()):
            document.shutdown()
        window.hide()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    file_editor_app.processEvents()


def _wait_for(app, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "Timed out waiting for the file/editor workflow"


def _completion_names(document):
    model = document.editor.completion_model
    return [model.item(row).data(Qt.ItemDataRole.UserRole)["name"]
            for row in range(model.rowCount())]


def test_new_file_without_suffix_opens_python_editor_with_completion_and_help(
    file_windows, file_editor_app
):
    window = file_windows()
    observation = {}
    errors = []

    def complete_dialog():
        dialog = QApplication.activeModalWidget()
        try:
            if not isinstance(dialog, NewFileDialog):
                raise AssertionError(f"Expected New File dialog, received {dialog!r}")
            observation["file_type"] = dialog.file_type_combo.currentData()
            QTest.keyClicks(dialog.name_edit, "dwd")
            observation["preview"] = dialog.filename_preview.text()
            QTest.mouseClick(dialog.create_button, Qt.MouseButton.LeftButton)
        except Exception as error:
            errors.append(error)
            if dialog is not None:
                dialog.reject()

    QTimer.singleShot(0, complete_dialog)
    QTest.mouseClick(window.project_explorer.new_file_button, Qt.MouseButton.LeftButton)
    assert not errors, errors
    assert observation["file_type"] == ".py"
    assert observation["preview"] == "File to create: dwd.py"
    path = window.current_project.path / "dwd.py"
    assert path.read_bytes() == b""
    assert not path.with_suffix("").exists()
    document = window.open_editors[path.resolve()]
    assert window.document_tabs.currentWidget() is document
    assert document.editor.is_python
    buttons = {button.text() for button in document.findChildren(QPushButton)}
    assert "Suggest" not in buttons and "Python Help" not in buttons
    assert document.syntax_label.isVisible()

    window.activateWindow()
    document.editor.setFocus()
    QTest.keyClicks(document.editor, "pri")
    _wait_for(file_editor_app, lambda: document.editor.completer.popup().isVisible()
              and "print" in _completion_names(document))
    row = _completion_names(document).index("print")
    document.editor.completer.popup().setCurrentIndex(
        document.editor.completer.completionModel().index(row, 0)
    )
    QTest.keyClick(document.editor, Qt.Key.Key_Tab)
    assert document.editor.toPlainText() == "print"
    assert document.is_modified
    assert path.read_bytes() == b""

    QTest.keyClick(document.editor, Qt.Key.Key_F1)
    popup = document.editor.completer.popup()
    _wait_for(file_editor_app, lambda: "print" in popup.details_title.text().lower())
    assert popup.isVisible() and popup.details_only
    assert "stdout" in popup.details_text.toPlainText()


def test_existing_extensionless_file_language_choice_survives_reopening(
    file_windows, file_editor_app
):
    first = file_windows()
    path = first.current_project.path / "dwd"
    original = b"print('existing work')\r\n"
    path.write_bytes(original)
    first._open_file_in_editor(path)
    document = first.open_editors[path.resolve()]
    assert not document.editor.is_python
    assert document.language_mode == "auto"
    document.language_selector.setCurrentIndex(document.language_selector.findData("python"))
    assert document.editor.is_python
    assert not document.is_modified
    first._close_workspace_tab(first.document_tabs.indexOf(document))
    first.ui_settings.sync()
    first.hide()

    second = file_windows()
    second._open_file_in_editor(path)
    restored = second.open_editors[path.resolve()]
    assert restored.language_mode == "python"
    assert restored.editor.is_python
    buttons = {button.text() for button in restored.findChildren(QPushButton)}
    assert "Suggest" not in buttons and "Python Help" not in buttons
    assert path.read_bytes() == original
    assert not path.with_suffix(".py").exists()
    assert not restored.is_modified

    restored.language_selector.setCurrentIndex(restored.language_selector.findData("auto"))
    assert not restored.editor.is_python
    assert str(path.resolve()) not in second._editor_language_overrides()
    second._close_workspace_tab(second.document_tabs.indexOf(restored))
    second._open_file_in_editor(path)
    automatic = second.open_editors[path.resolve()]
    assert automatic.language_mode == "auto"
    assert not automatic.editor.is_python
    assert path.read_bytes() == original
