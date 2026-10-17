"""Recycle project entries without a permanent-delete fallback."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFile

from .file_operations import project_entry


def move_project_entry_to_trash(project_root: Path, path: Path) -> Path:
    """Validate again at the mutation boundary and return the original path."""

    target = project_entry(project_root, path)
    item = QFile(str(target))
    if not item.moveToTrash():
        raise OSError(
            f"Could not move {target.name} to the Recycle Bin / Trash.\n"
            f"{item.errorString()}\n\n"
            "Close applications using this item and try again."
        )
    return target


def editor_path_is_removed(editor_path: Path, target: Path) -> bool:
    """A link's target remains on disk when only the link is recycled."""

    if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
        return False
    real_target = target.resolve()
    real_editor = editor_path.resolve()
    return real_editor == real_target or (
        target.is_dir() and real_editor.is_relative_to(real_target)
    )
