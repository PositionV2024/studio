from pathlib import Path

import pytest

from studio.file_operations import (
    copy_project_file,
    create_project_file,
    create_project_folder,
    project_entry,
    validate_entry_name,
)


@pytest.mark.parametrize(
    "name", ["", "..", "../escape.py", "folder\\escape.py", "NUL.txt", "com1", "LPT¹.log", "bad:name", "name.", "name "]
)
def test_names_cannot_escape_or_break_windows_paths(name: str) -> None:
    with pytest.raises(ValueError):
        validate_entry_name(name)


def test_creation_and_import_never_replace_existing_content(tmp_path: Path) -> None:
    project = create_project_folder(tmp_path, tmp_path, "project")
    source_folder = create_project_folder(tmp_path, tmp_path, "source")
    existing = project / "main.py"
    existing.write_text("keep this\n")
    source = source_folder / "main.py"
    source.write_text("different contents\n")

    with pytest.raises(FileExistsError):
        create_project_file(project, project, "main.py")
    with pytest.raises(FileExistsError):
        create_project_folder(project, project, "MAIN.PY")
    with pytest.raises(FileExistsError):
        copy_project_file(project, project, source)
    assert existing.read_text() == "keep this\n"
    assert source.read_text() == "different contents\n"


def test_import_copies_bytes_without_moving_source(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "image.bin"
    source.write_bytes(bytes(range(256)))
    destination = copy_project_file(project, project, source)
    assert destination == project / source.name
    assert destination.read_bytes() == source.read_bytes()


def test_project_root_and_outside_entries_are_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "outside.py"
    external.touch()
    with pytest.raises(ValueError):
        project_entry(project, project)
    with pytest.raises(ValueError):
        project_entry(project, external)
    with pytest.raises(ValueError):
        project_entry(project, project / ".." / "outside.py")
    assert project_entry(project, project, allow_root=True) == project


def test_symlink_outside_project_cannot_be_a_destination_or_delete_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "keep.py").write_text("important\n")
    link = project / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform.")
    with pytest.raises(ValueError):
        project_entry(project, link)
    with pytest.raises(ValueError):
        project_entry(project, link / "keep.py")
    with pytest.raises(ValueError):
        create_project_file(project, link, "new.py")
    assert not (outside / "new.py").exists()
    assert (outside / "keep.py").read_text() == "important\n"


def test_internal_symlink_validation_keeps_the_link_path(tmp_path: Path) -> None:
    target = tmp_path / "actual.py"
    target.write_text("print('hello')\n")
    link = tmp_path / "alias.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform.")
    assert project_entry(tmp_path, link) == link

