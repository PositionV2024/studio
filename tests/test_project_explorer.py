from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QPlainTextEdit, QWidget

from studio.project_explorer import NewFileDialog, ProjectExplorer


@pytest.fixture(scope="session")
def explorer_app():
    return QApplication.instance() or QApplication([])


def test_create_opens_file_in_selected_folder_and_refresh_keeps_selection(
    tmp_path: Path, monkeypatch, explorer_app
) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    existing = folder / "main.py"
    existing.touch()
    explorer = ProjectExplorer()
    assert not explorer.new_file_button.isEnabled()
    assert not explorer.delete_button.isEnabled()
    explorer.set_project(tmp_path)
    explorer.select_path(existing)
    opened = []
    explorer.file_opened.connect(opened.append)
    def create_file(dialog):
        dialog.name_edit.setText("helpers.py")
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(NewFileDialog, "exec", create_file)

    explorer.new_file()
    explorer_app.processEvents()
    created = folder / "helpers.py"
    assert created.exists()
    assert opened == [created]
    assert explorer.selected_path == created
    assert explorer.destination_label.text() == "Add to: src"
    explorer.refresh()
    explorer_app.processEvents()
    assert explorer.selected_path == created
    assert explorer.tree.isExpanded(explorer.model.index(str(folder)))
    explorer.close()


def test_extensionless_new_file_defaults_to_python_and_opens_the_created_path(
    tmp_path: Path, monkeypatch, explorer_app
) -> None:
    explorer = ProjectExplorer()
    explorer.set_project(tmp_path)
    opened = []
    explorer.file_opened.connect(opened.append)

    def create_file(dialog):
        assert dialog.file_type_combo.currentData() == ".py"
        assert ".py" in dialog.extension_hint.text()
        dialog.name_edit.setText("dwd")
        assert dialog.filename_preview.text() == "File to create: dwd.py"
        QTest.mouseClick(dialog.create_button, Qt.MouseButton.LeftButton)
        return dialog.result()

    monkeypatch.setattr(NewFileDialog, "exec", create_file)
    explorer.new_file()

    assert (tmp_path / "dwd.py").is_file()
    assert not (tmp_path / "dwd").exists()
    assert opened == [tmp_path / "dwd.py"]
    explorer.close()


@pytest.mark.parametrize(
    ("file_type", "typed_name", "expected_name"),
    [
        (".py", "notes.txt", "notes.txt"),
        (".py", "config.json", "config.json"),
        (".py", ".env", ".env"),
        (".txt", "notes", "notes.txt"),
        (".txt", "config.json", "config.json"),
        ("", "dwd", "dwd"),
        ("", "Dockerfile", "Dockerfile"),
        ("", "config.json", "config.json"),
    ],
)
def test_new_file_respects_explicit_extensions_and_chosen_file_type(
    tmp_path: Path, explorer_app, file_type: str, typed_name: str, expected_name: str
) -> None:
    dialog = NewFileDialog(tmp_path, tmp_path)
    dialog.file_type_combo.setCurrentIndex(dialog.file_type_combo.findData(file_type))
    dialog.name_edit.setText(typed_name)

    assert dialog.filename_preview.text() == f"File to create: {expected_name}"
    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_path == tmp_path / expected_name
    assert sorted(path.name for path in tmp_path.iterdir()) == [expected_name]
    dialog.close()


def test_new_file_collision_keeps_original_and_shows_error_in_same_dialog(
    tmp_path: Path, explorer_app
) -> None:
    existing = tmp_path / "dwd.py"
    existing.write_text("print('preserve')\n")
    dialog = NewFileDialog(tmp_path, tmp_path)
    dialog.name_edit.setText("dwd")
    dialog.accept()

    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_path is None
    assert dialog.name_edit.text() == "dwd"
    assert not dialog.error_label.isHidden()
    assert "dwd.py" in dialog.error_label.text()
    assert "already exists" in dialog.error_label.text()
    assert existing.read_text() == "print('preserve')\n"

    # The user can correct the name in the same dialog, without restarting it.
    dialog.name_edit.setText("another")
    assert dialog.error_label.isHidden()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.created_path == tmp_path / "another.py"
    assert existing.read_text() == "print('preserve')\n"
    dialog.close()


def test_new_file_does_not_hide_invalid_trailing_dot_with_python_extension(
    tmp_path: Path, explorer_app
) -> None:
    dialog = NewFileDialog(tmp_path, tmp_path)
    dialog.name_edit.setText("invalid.")

    assert not dialog.create_button.isEnabled()
    assert "full stop" in dialog.error_label.text()
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.created_path is None
    assert not list(tmp_path.iterdir())
    dialog.close()


def test_cancelling_new_file_leaves_project_unchanged(
    tmp_path: Path, monkeypatch, explorer_app
) -> None:
    explorer = ProjectExplorer()
    explorer.set_project(tmp_path)
    opened = []
    explorer.file_opened.connect(opened.append)

    def cancel_file(dialog):
        dialog.name_edit.setText("dwd")
        dialog.reject()
        return dialog.result()

    monkeypatch.setattr(NewFileDialog, "exec", cancel_file)
    explorer.new_file()

    assert not list(tmp_path.iterdir())
    assert opened == []
    explorer.close()


def test_delete_only_requests_main_window_action_and_never_fires_in_editor(
    tmp_path: Path, explorer_app
) -> None:
    existing = tmp_path / "main.py"
    existing.write_text("print('keep')\n")
    window = QWidget()
    layout = QHBoxLayout(window)
    explorer = ProjectExplorer()
    editor = QPlainTextEdit()
    editor.setPlainText("abc")
    layout.addWidget(explorer)
    layout.addWidget(editor)
    window.show()
    explorer.set_project(tmp_path)
    explorer.select_path(existing)
    requested = []
    explorer.delete_requested.connect(requested.append)
    editor.setFocus()
    explorer_app.processEvents()

    QTest.keyClick(editor, Qt.Key.Key_Delete)
    assert requested == []
    assert editor.toPlainText() == "bc"
    explorer.tree.setFocus()
    explorer_app.processEvents()
    QTest.keyClick(explorer.tree, Qt.Key.Key_Delete)
    assert requested == [existing]
    assert existing.read_text() == "print('keep')\n"
    window.close()


def test_root_destination_is_ready_after_selected_root_file_is_removed(
    tmp_path: Path, explorer_app
) -> None:
    existing = tmp_path / "only.py"
    existing.touch()
    explorer = ProjectExplorer()
    explorer.set_project(tmp_path)
    explorer.select_path(existing)
    explorer_app.processEvents()
    assert explorer.selected_path == existing

    existing.unlink()
    explorer.refresh()
    explorer.select_path(tmp_path)
    explorer_app.processEvents()

    assert explorer.selected_path is None
    assert not explorer.tree.currentIndex().isValid()
    assert explorer.destination_label.text() == "Add to: project root"
    assert explorer.new_file_button.isEnabled()
    assert explorer.new_folder_button.isEnabled()
    assert explorer.add_files_button.isEnabled()
    assert not explorer.delete_button.isEnabled()
    explorer.close()


def test_blank_space_click_chooses_project_root(tmp_path: Path, explorer_app) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    explorer = ProjectExplorer()
    explorer.resize(300, 500)
    explorer.show()
    explorer.set_project(tmp_path)
    explorer.select_path(nested)
    explorer_app.processEvents()
    assert explorer.destination_label.text() == "Add to: nested"
    viewport = explorer.tree.viewport()
    empty_position = QPoint(20, viewport.height() - 20)
    assert not explorer.tree.indexAt(empty_position).isValid()

    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=empty_position)

    assert explorer.selected_path is None
    assert explorer.destination_label.text() == "Add to: project root"
    assert not explorer.delete_button.isEnabled()
    explorer.close()
