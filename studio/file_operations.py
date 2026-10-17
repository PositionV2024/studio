"""Small, non-destructive helpers shared by project file actions."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_WINDOWS_RESERVED_NAMES.update(
    f"{prefix}{number}"
    for prefix in ("COM", "LPT")
    for number in "123456789¹²³"
)


def validate_entry_name(name: str) -> str:
    """Validate one file/folder name, including Windows restrictions.

    This application is also developed on Linux, so host-dependent ``Path``
    validation alone would accept names that cannot be used on Windows.
    """

    if not name or not name.strip() or name in {".", ".."}:
        raise ValueError("Enter a file or folder name.")
    if any(character in '<>:"/\\|?*' or ord(character) < 32 for character in name):
        raise ValueError('Names cannot contain < > : " / \\ | ? * or control characters.')
    if name.endswith((" ", ".")):
        raise ValueError("Names cannot end with a space or a full stop.")
    if name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{name!r} is a reserved Windows name. Choose another name.")
    return name


def project_entry(
    project_root: Path, path: Path, *, allow_root: bool = False
) -> Path:
    """Return an existing, lexical project path or raise ``ValueError``.

    Check both the entry's location and its resolved target. This rejects
    symlinks/junctions that lead outside the project, including those in a
    parent directory. Return the lexical entry so moving a permitted symlink
    to the recycle bin moves that link, rather than its target.
    """

    try:
        root = Path(project_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("The project folder is no longer available.")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        # Keep the final path lexical; resolve below is only a boundary check.
        candidate = Path(os.path.abspath(candidate))
        resolved = candidate.resolve(strict=False)
        if not candidate.is_relative_to(root) or not resolved.is_relative_to(root):
            raise ValueError("Choose a file or folder inside the current project.")
        if not allow_root and (candidate == root or resolved == root):
            raise ValueError("The project root folder cannot be deleted.")
        if not candidate.exists() and not candidate.is_symlink():
            raise ValueError("The selected file or folder no longer exists.")
        return candidate
    except (OSError, RuntimeError) as error:
        raise ValueError(f"The selected file or folder cannot be accessed: {error}") from error


def new_entry_path(project_root: Path, folder: Path, name: str) -> Path:
    """Choose an unused child name without replacing existing content."""

    validate_entry_name(name)
    directory = project_entry(project_root, folder, allow_root=True)
    if not directory.is_dir():
        raise ValueError("Choose a destination folder in the current project.")
    # Case-insensitive checks keep projects portable between Windows and Linux
    # and also catch existing directories and broken symlinks with that name.
    for entry in directory.iterdir():
        if entry.name.casefold() == name.casefold():
            raise FileExistsError(f"{entry.name!r} already exists in {directory.name}.")
    return directory / name


def create_project_file(project_root: Path, folder: Path, name: str) -> Path:
    destination = new_entry_path(project_root, folder, name)
    # Exclusive creation also protects against an exact-name race after the
    # friendly collision check above.
    with destination.open("xb"):
        pass
    return destination


def create_project_folder(project_root: Path, folder: Path, name: str) -> Path:
    destination = new_entry_path(project_root, folder, name)
    destination.mkdir()
    return destination


def copy_project_file(project_root: Path, folder: Path, source: Path) -> Path:
    """Copy one existing file into the project; never replace a destination."""

    source = Path(source).expanduser()
    if not source.is_file():
        raise ValueError("Only existing files can be added. Select files, not folders.")
    destination = new_entry_path(project_root, folder, source.name)
    created = False
    try:
        with source.open("rb") as source_stream:
            with destination.open("xb") as destination_stream:
                created = True
                shutil.copyfileobj(source_stream, destination_stream)
    except (OSError, ValueError):
        if created:
            # A failed import must not leave a misleading partial file.
            destination.unlink(missing_ok=True)
        raise
    # Retain timestamps where supported; source permissions intentionally do
    # not carry over, so a read-only source can still be edited after import.
    try:
        source_stat = source.stat()
        os.utime(destination, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    except OSError:
        pass
    return destination
