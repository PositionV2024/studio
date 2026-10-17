"""Exercise editor lifecycle through MainWindow without starting WebEngine."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QTabWidget, QWidget

from studio import file_removal
from studio.code_editor import CodeEditorDocument, shutdown_python_assist
from studio.main_window import MainWindow
from studio.project_manifest import ProjectManifest, save_manifest
from studio.project_runner import ProjectRunner


class ExplorerProbe:
    def __init__(self, root: Path) -> None:
        self.project_path = root
        self.refresh = Mock()
        self.select_path = Mock()


class EditorWindow(MainWindow):
    """Use the production document methods with ordinary Qt widgets only."""

    def __init__(self, root: Path) -> None:
        QMainWindow.__init__(self)
        self.current_project = SimpleNamespace(path=root)
        self.current_settings = None
        self.project_explorer = ExplorerProbe(root)
        self.runner = SimpleNamespace(_discover_python=ProjectRunner._discover_python)
        self.open_editors: dict[Path, CodeEditorDocument] = {}
        self.document_tabs = QTabWidget(self)
        self.preview = QWidget(self.document_tabs)
        self.document_tabs.addTab(self.preview, "Preview")
        self.setCentralWidget(self.document_tabs)
        self.action_updates = 0

    def _update_project_actions(self) -> None:
        self.action_updates += 1

    def show_workspace(self) -> None:
        pass


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app
    shutdown_python_assist()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


@pytest.fixture
def window(tmp_path: Path, qt_app):
    root = tmp_path / "project"
    root.mkdir()
    value = EditorWindow(root)
    yield value
    for document in list(value.open_editors.values()):
        document.shutdown()
    value.hide()
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()


@pytest.fixture
def dialogs(monkeypatch):
    confirm = Mock(return_value=QMessageBox.StandardButton.Yes)
    unsaved = Mock(return_value=QMessageBox.StandardButton.Discard)
    warning = Mock(return_value=QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "exec", confirm)
    monkeypatch.setattr(QMessageBox, "question", unsaved)
    monkeypatch.setattr(QMessageBox, "warning", warning)

    def unexpected_error(*args, **kwargs):
        raise AssertionError(f"Unexpected error dialog: {args[1:]!r}")

    monkeypatch.setattr(QMessageBox, "critical", unexpected_error)
    return SimpleNamespace(confirm=confirm, unsaved=unsaved, warning=warning)


def open_document(window: EditorWindow, relative_path: str, text: str = "original\n"):
    path = window.project_explorer.project_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    window._open_file_in_editor(path)
    return window.open_editors[path.resolve()]


def edit_document(document: CodeEditorDocument, added: str = "unsaved edit\n") -> str:
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    document.editor.insertPlainText(added)
    assert document.is_modified
    return document.editor.toPlainText()


@pytest.mark.parametrize("cancel_stage", ["confirmation", "unsaved_changes"])
def test_cancelled_deletion_keeps_dirty_document(window, dialogs, monkeypatch, cancel_stage):
    document = open_document(window, "notes.txt")
    edited = edit_document(document)
    stop = Mock(wraps=document.shutdown)
    monkeypatch.setattr(document, "shutdown", stop)
    native_file = Mock()
    monkeypatch.setattr(file_removal, "QFile", native_file)
    if cancel_stage == "confirmation":
        dialogs.confirm.return_value = QMessageBox.StandardButton.No
    else:
        dialogs.unsaved.return_value = QMessageBox.StandardButton.Cancel

    window._delete_project_path(document.path)

    assert document.path.read_text(encoding="utf-8") == "original\n"
    assert document.editor.toPlainText() == edited
    assert document.is_modified
    assert window.open_editors[document.path] is document
    assert window.document_tabs.indexOf(document) >= 0
    native_file.assert_not_called()
    stop.assert_not_called()
    window.project_explorer.refresh.assert_not_called()
    if cancel_stage == "confirmation":
        dialogs.unsaved.assert_not_called()
    else:
        dialogs.unsaved.assert_called_once()


def test_failed_trash_keeps_discarded_buffer_open(window, dialogs, monkeypatch):
    document = open_document(window, "notes.txt")
    edited = edit_document(document)
    stop = Mock(wraps=document.shutdown)
    monkeypatch.setattr(document, "shutdown", stop)
    native_file = Mock()
    native_file.moveToTrash.return_value = False
    native_file.errorString.return_value = "Recycle Bin is unavailable"
    constructor = Mock(return_value=native_file)
    monkeypatch.setattr(file_removal, "QFile", constructor)

    window._delete_project_path(document.path)

    constructor.assert_called_once_with(str(document.path))
    native_file.moveToTrash.assert_called_once_with()
    assert document.path.read_text(encoding="utf-8") == "original\n"
    assert document.editor.toPlainText() == edited
    assert document.is_modified
    assert window.open_editors[document.path] is document
    assert window.document_tabs.indexOf(document) >= 0
    stop.assert_not_called()
    window.project_explorer.refresh.assert_not_called()
    dialogs.warning.assert_called_once()
    assert "Recycle Bin is unavailable" in dialogs.warning.call_args.args[-1]


def test_trashing_folder_saves_and_closes_only_affected_documents(
    window, dialogs, monkeypatch, tmp_path
):
    first = open_document(window, "source/first.txt")
    second = open_document(window, "source/nested/second.txt")
    unrelated = open_document(window, "keep.txt")
    first_text = edit_document(first, "first edit\n")
    second_text = edit_document(second, "second edit\n")
    unrelated_text = edit_document(unrelated, "keep editing\n")
    first_stop = Mock(wraps=first.shutdown)
    second_stop = Mock(wraps=second.shutdown)
    unrelated_stop = Mock(wraps=unrelated.shutdown)
    monkeypatch.setattr(first, "shutdown", first_stop)
    monkeypatch.setattr(second, "shutdown", second_stop)
    monkeypatch.setattr(unrelated, "shutdown", unrelated_stop)
    dialogs.unsaved.return_value = QMessageBox.StandardButton.Save
    target = window.project_explorer.project_path / "source"
    recycled = tmp_path / "recycled-source"

    def recycle():
        target.rename(recycled)
        return True

    native_file = Mock()
    native_file.moveToTrash.side_effect = recycle
    constructor = Mock(return_value=native_file)
    monkeypatch.setattr(file_removal, "QFile", constructor)

    window._delete_project_path(target)

    constructor.assert_called_once_with(str(target))
    assert not target.exists()
    assert (recycled / "first.txt").read_text(encoding="utf-8") == first_text
    assert (recycled / "nested/second.txt").read_text(encoding="utf-8") == second_text
    assert set(window.open_editors) == {unrelated.path}
    assert window.document_tabs.indexOf(first) == -1
    assert window.document_tabs.indexOf(second) == -1
    assert window.document_tabs.indexOf(unrelated) >= 0
    assert unrelated.editor.toPlainText() == unrelated_text
    assert unrelated.is_modified
    first_stop.assert_called_once_with()
    second_stop.assert_called_once_with()
    unrelated_stop.assert_not_called()
    window.project_explorer.select_path.assert_called_once_with(target.parent)
    dialogs.warning.assert_not_called()


def test_trashing_symlink_keeps_target_and_its_dirty_editor(
    window, dialogs, monkeypatch, tmp_path
):
    document = open_document(window, "target.txt")
    edited = edit_document(document)
    link = window.project_explorer.project_path / "alias.txt"
    try:
        link.symlink_to(document.path)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"Symbolic links unavailable: {error}")
    recycled = tmp_path / "recycled-link"

    def recycle():
        link.rename(recycled)
        return True

    native_file = Mock()
    native_file.moveToTrash.side_effect = recycle
    monkeypatch.setattr(file_removal, "QFile", Mock(return_value=native_file))

    window._delete_project_path(link)

    assert not link.exists()
    assert recycled.is_symlink()
    assert document.path.read_text(encoding="utf-8") == "original\n"
    assert document.editor.toPlainText() == edited
    assert document.is_modified
    assert window.open_editors[document.path] is document
    assert window.document_tabs.indexOf(document) >= 0
    dialogs.unsaved.assert_not_called()
    dialogs.warning.assert_not_called()


def test_undo_and_redo_on_selected_editor_leave_visual_history_untouched(window, qt_app):
    document = open_document(window, "notes.txt")
    edited = edit_document(document)
    visual_history = SimpleNamespace(undo=Mock(), redo=Mock())
    window.current_settings = visual_history
    window.show()
    window.activateWindow()
    # A toolbar command may arrive while focus is on the tab bar, not the text.
    tab_bar = window.document_tabs.tabBar()
    tab_bar.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    tab_bar.setFocus()
    qt_app.processEvents()

    window.undo()
    assert document.editor.toPlainText() == "original\n"
    window.redo()
    assert document.editor.toPlainText() == edited
    visual_history.undo.assert_not_called()
    visual_history.redo.assert_not_called()


def test_existing_tabs_keep_their_original_project_context(window, tmp_path):
    first_root = window.project_explorer.project_path
    first_python = str(Path(sys.executable))
    save_manifest(first_root, ProjectManifest("First", python_executable=first_python))
    first = open_document(window, "package/module.py", "value = 1\n")
    assert first.project_path == first_root
    assert first.python_executable == first_python

    second_root = tmp_path / "second-project"
    second_root.mkdir()
    second_python = str(Path(getattr(sys, "_base_executable", sys.executable)))
    save_manifest(second_root, ProjectManifest("Second", python_executable=second_python))
    window.current_project = SimpleNamespace(path=second_root)
    window.project_explorer.project_path = second_root
    second = open_document(window, "other.py", "value = 2\n")
    assert second.project_path == second_root
    assert second.python_executable == second_python

    window._open_file_in_editor(first.path)

    assert window._current_editor() is first
    assert len(window.open_editors) == 2
    assert first.project_path == first_root
    assert first.python_executable == first_python
    assert second.project_path == second_root
