"""Verified, file-preserving updates for the compact Windows distribution.

This module deliberately uses only the Python standard library. Studio copies
the *installed* copy to a private staging directory before closing. The copy
holds a handle to Studio's process, waits for its exit, and only then changes
managed application files. It never imports or executes the downloaded code.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import time
import tomllib
from typing import Any, Callable, Iterator
import uuid
import zipfile


APPLICATION = "hoyo-project-studio"
EXE_NAME = "HoYo Project Studio.exe"
MANIFEST_NAME = "app/package-manifest.json"
LOCK_NAME = ".hoyo_install.lock"
PENDING_NAME = ".hoyo_update_pending.json"
MAX_FILES = 10000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
REQUIRED_FILES = frozenset({
    EXE_NAME, "app/studio/__init__.py", "app/studio/application.py",
    "app/studio/update_install.py", "app/pyproject.toml", "app/main.py",
    "app/requirements.txt", "app/launcher/launcher.pyw", "app/launcher/startup.py",
    "app/launcher/startup.ps1", "app/launcher/python_discovery.ps1",
    "app/launcher/update_apply.py",
})
_VENDOR_DIRECTORIES = frozenset({"studio", "launcher", "resources", "templates", "tests"})
_SHIPPED_METADATA_FILES = frozenset({
    "app/.gitignore",
    "app/templates/streamlit_demo/.studio/defaults.json",
    "app/templates/streamlit_demo/.studio/draft.json",
    "app/templates/streamlit_demo/.studio/project.json",
    "app/templates/streamlit_demo/.studio/published.json",
})
_ROOT_EXTENSIONS = frozenset({".py", ".pyw", ".toml", ".txt", ".md", ".cmd", ".vbs", ".spec", ".json"})
_FORBIDDEN_PARTS = frozenset({
    ".venv", "venv", "__pycache__", "node_modules", ".git", ".env",
    "projects", "user_projects", "logs", "cache", "downloads", "dist", "build",
    "updater-state", "update-backups", "credentials", "credentials.json", "secrets.json",
})
_RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$"}) | frozenset(
    f"{prefix}{suffix}" for prefix in ("com", "lpt") for suffix in "123456789¹²³"
)


class UpdateError(RuntimeError):
    def __init__(self, message: str, *, cancelled: bool = False) -> None:
        super().__init__(message)
        self.cancelled = cancelled


def check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise UpdateError("Update preparation was cancelled.", cancelled=True)


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})", value):
        raise UpdateError("The update must use a stable version such as 0.4.13.")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("The update contains duplicate JSON fields.")
        result[key] = value
    return result


def read_json(path: Path, maximum: int = MAX_MANIFEST_BYTES) -> dict[str, Any]:
    try:
        if path.stat().st_size > maximum:
            raise UpdateError("The update metadata is too large.")
        data = path.read_bytes()
        result = json.loads(data, object_pairs_hook=_json_pairs)
    except (OSError, UnicodeError, ValueError) as error:
        raise UpdateError("The update metadata could not be read.") from error
    if not isinstance(result, dict):
        raise UpdateError("The update metadata must be a JSON object.")
    return result


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path: Path, cancelled: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            check_cancelled(cancelled)
            block = stream.read(CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def relative_path(value: str, *, directory: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or "\\" in value or value.startswith("/"):
        raise UpdateError("The update contains an unsafe file path.")
    name = value[:-1] if directory and value.endswith("/") else value
    parts = name.split("/")
    for part in parts:
        if (not part or len(part) > 255 or part in (".", "..") or part[-1:] in (".", " ")
                or any(ord(character) < 32 or ord(character) == 127 or character in '<>:"|?*' for character in part)
                or part.split(".", 1)[0].rstrip(" ").casefold() in _RESERVED_NAMES):
            raise UpdateError("The update contains a file name that is unsafe on Windows.")
    return name


def vendor_path(value: str) -> str:
    name = relative_path(value)
    # These five files ship with the application and demo template. Every
    # other hidden path remains excluded, including user-project metadata.
    if name == EXE_NAME or name in _SHIPPED_METADATA_FILES:
        return name
    parts = name.split("/")
    if len(parts) < 2 or parts[0] != "app":
        raise UpdateError("The package can contain only the Studio launcher and its app folder.")
    if any(part.startswith(".") or part.casefold() in _FORBIDDEN_PARTS or part.casefold().endswith(".log") for part in parts[1:]):
        raise UpdateError("The update attempts to include user data, environments, or runtime files.")
    if len(parts) == 2:
        if Path(parts[1]).suffix.lower() not in _ROOT_EXTENSIONS:
            raise UpdateError("The update contains an unsupported application file.")
    elif parts[1] not in _VENDOR_DIRECTORIES:
        raise UpdateError("The update contains a folder outside Studio's application files.")
    return name


def _check_case_paths(paths: Iterator[str] | list[str] | set[str]) -> None:
    seen: dict[str, str] = {}
    file_names = set(paths)
    for name in file_names:
        components = name.split("/")
        for index in range(1, len(components) + 1):
            part = "/".join(components[:index])
            previous = seen.setdefault(part.casefold(), part)
            if previous != part:
                raise UpdateError("The update contains file names that collide on Windows.")
            if index < len(components) and part in file_names:
                raise UpdateError("An update file also appears as a folder.")


def validate_manifest(value: dict[str, Any], expected_version: str | None = None) -> dict[str, Any]:
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("application") != APPLICATION or value.get("platform") != "windows-x64"
            or value.get("layout") != "compact-v1"):
        raise UpdateError("This package is not a compatible HoYo Project Studio Windows update.")
    version_tuple(value.get("version"))
    if expected_version is not None and value["version"] != expected_version:
        raise UpdateError("The package version does not match the selected GitHub release.")
    files = value.get("files")
    if not isinstance(files, dict) or not REQUIRED_FILES.issubset(files) or len(files) > MAX_FILES:
        raise UpdateError("The update package is missing required application files.")
    if MANIFEST_NAME in files:
        raise UpdateError("The package manifest must not include its own hash.")
    for name, digest in files.items():
        vendor_path(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateError("The update contains an invalid file checksum.")
    _check_case_paths(set(files) | {MANIFEST_NAME})
    return value


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def safe_root(path: Path) -> Path:
    root = Path(os.path.abspath(path))
    if os.name == "nt" and str(root).startswith("\\\\"):
        raise UpdateError("Automatic installation is not available in a network or device path.")
    for item in reversed((root, *root.parents)):
        try:
            info = item.lstat()
        except OSError as error:
            raise UpdateError("The application or update folder is not accessible.") from error
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise UpdateError("Automatic updates cannot use linked or redirected application folders.")
    return root


def safe_destination(root: Path, name: str) -> Path:
    relative_path(name)
    current = root
    parts = name.split("/")
    for index, component in enumerate(parts):
        if current.exists():
            try:
                aliases = [entry.name for entry in current.iterdir() if entry.name.casefold() == component.casefold()]
            except OSError as error:
                raise UpdateError("An application folder could not be checked for safe installation.") from error
            if aliases and aliases != [component]:
                raise UpdateError(f"A differently capitalized file conflicts with the update: {name}")
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise UpdateError(f"The application file could not be inspected: {name}") from error
        if _is_link(info):
            raise UpdateError(f"A linked file or folder blocks safe installation: {name}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise UpdateError(f"A file blocks an application folder: {name}")
        if index == len(parts) - 1 and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
            raise UpdateError(f"An application destination is not an ordinary file: {name}")
    return current


def _assert_package_version(root: Path, expected_version: str) -> None:
    try:
        module = ast.parse((root / "app/studio/__init__.py").read_text(encoding="utf-8"))
        versions = [ast.literal_eval(node.value) for node in module.body
                    if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
        project = tomllib.loads((root / "app/pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, SyntaxError, UnicodeError) as error:
        raise UpdateError("The update's application version metadata is invalid.") from error
    if versions != [expected_version] or project.get("project", {}).get("version") != expected_version or project.get("project", {}).get("name") != APPLICATION:
        raise UpdateError("The Python package, project metadata, and update version do not agree.")
    with (root / EXE_NAME).open("rb") as executable:
        dos = executable.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise UpdateError("The update does not contain a Windows launcher.")
        offset = struct.unpack_from("<I", dos, 60)[0]
        if offset > MAX_FILE_BYTES - 6:
            raise UpdateError("The update's Windows launcher is invalid.")
        executable.seek(offset)
        if executable.read(6) != b"PE\0\0\x64\x86":
            raise UpdateError("The update launcher is not a Windows x64 application.")


def _inventory(root: Path) -> set[str]:
    result: set[str] = set()
    pending = [root]
    while pending:
        parent = pending.pop()
        for item in parent.iterdir():
            info = item.lstat()
            if _is_link(info):
                raise UpdateError("The prepared update contains a linked file or folder.")
            if stat.S_ISDIR(info.st_mode):
                pending.append(item)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                result.add(item.relative_to(root).as_posix())
            else:
                raise UpdateError("The prepared update contains a special file.")
            if len(result) + len(pending) > MAX_FILES * 2:
                raise UpdateError("The prepared update contains too many files.")
    return result


def validate_staging(root: Path, expected_version: str, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    root = safe_root(root)
    manifest = validate_manifest(read_json(safe_destination(root, MANIFEST_NAME)), expected_version)
    if _inventory(root) != set(manifest["files"]) | {MANIFEST_NAME}:
        raise UpdateError("The prepared update contains missing or unlisted files.")
    total = 0
    for name, expected in manifest["files"].items():
        check_cancelled(cancelled)
        path = safe_destination(root, name)
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or file_hash(path, cancelled) != expected:
            raise UpdateError(f"An update file failed verification: {name}")
    _assert_package_version(root, expected_version)
    return manifest


def stage_archive(archive: Path, destination: Path, expected_version: str, *, cancelled: Callable[[], bool] | None = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    version_tuple(expected_version)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise UpdateError("The update ZIP is too large.")
    if destination.exists():
        raise UpdateError("The update staging folder is already in use.")
    destination.mkdir()
    safe_root(destination)
    try:
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if not entries or len(entries) > MAX_FILES * 2:
                raise UpdateError("The update ZIP contains too many entries or is empty.")
            files: dict[str, zipfile.ZipInfo] = {}
            directories: set[str] = set()
            seen: set[str] = set()
            total = 0
            prefix = "hoyo_project_studio/"
            for entry in entries:
                check_cancelled(cancelled)
                if entry.orig_filename != entry.filename or not entry.filename.startswith(prefix):
                    raise UpdateError("The ZIP must contain the complete hoyo_project_studio folder.")
                whole = relative_path(entry.filename, directory=entry.is_dir())
                if whole in seen:
                    raise UpdateError("The update ZIP contains duplicate file entries.")
                seen.add(whole)
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (entry.flag_bits & 1 or entry.external_attr & 0x400
                        or kind not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise UpdateError("The update ZIP contains encrypted, linked, or unsupported entries.")
                if whole == "hoyo_project_studio" and entry.is_dir():
                    continue
                name = whole[len(prefix):]
                if entry.is_dir():
                    directories.add(name)
                    continue
                vendor_path(name)
                if entry.file_size < 0 or entry.file_size > MAX_FILE_BYTES or (entry.file_size > CHUNK_BYTES and entry.file_size > max(entry.compress_size, 1) * 1000):
                    raise UpdateError("The update ZIP has an unsafe expanded file size.")
                total += entry.file_size
                if total > MAX_TOTAL_BYTES:
                    raise UpdateError("The update ZIP expands beyond the installation size limit.")
                files[name] = entry
            _check_case_paths(set(files))
            manifest_info = files.get(MANIFEST_NAME)
            if manifest_info is None or manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise UpdateError("The update ZIP has no valid package-manifest.json.")
            try:
                manifest = json.loads(package.read(manifest_info), object_pairs_hook=_json_pairs)
            except (ValueError, UnicodeError) as error:
                raise UpdateError("The update package manifest is invalid.") from error
            if not isinstance(manifest, dict):
                raise UpdateError("The update package manifest is invalid.")
            validate_manifest(manifest, expected_version)
            if set(files) != set(manifest["files"]) | {MANIFEST_NAME}:
                raise UpdateError("Every update file must be listed in the package manifest.")
            allowed_directories = {"/".join(name.split("/")[:index]) for name in files for index in range(1, len(name.split("/")))}
            if not directories.issubset(allowed_directories):
                raise UpdateError("The update contains an unlisted or incorrectly capitalized folder.")
            for index, (name, entry) in enumerate(files.items(), 1):
                check_cancelled(cancelled)
                if progress is not None:
                    progress(f"Verifying application files ({index}/{len(files)})…")
                path = safe_destination(destination, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                count = 0
                with package.open(entry) as source, path.open("xb") as target:
                    while True:
                        check_cancelled(cancelled)
                        block = source.read(CHUNK_BYTES)
                        if not block:
                            break
                        count += len(block)
                        if count > entry.file_size:
                            raise UpdateError("An update file exceeds its declared size.")
                        digest.update(block)
                        target.write(block)
                if count != entry.file_size or (name != MANIFEST_NAME and digest.hexdigest() != manifest["files"][name]):
                    raise UpdateError(f"An update file failed checksum verification: {name}")
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError("The update ZIP could not be verified or extracted.") from error
    _assert_package_version(destination, expected_version)
    return manifest


def build_plan(install_root: Path, staged: Path, current_version: str, target_version: str, *, expected_manifest_hash: str | None = None, cancelled: Callable[[], bool] | None = None) -> tuple[list[dict[str, Any]], tuple[str, ...], str]:
    root = safe_root(install_root)
    new = validate_staging(staged, target_version, cancelled)
    old_path = safe_destination(root, MANIFEST_NAME)
    if not old_path.is_file():
        raise UpdateError("This installation has no ownership manifest. Extract the complete update into a new folder and open its launcher.")
    old_hash = file_hash(old_path, cancelled)
    if expected_manifest_hash is not None and old_hash != expected_manifest_hash:
        raise UpdateError("The installed application changed after the update was prepared. Check for updates again.")
    old = validate_manifest(read_json(old_path), current_version)
    if version_tuple(target_version) <= version_tuple(current_version):
        raise UpdateError("Only a newer application version can be installed.")
    _assert_package_version(root, current_version)
    operations: list[dict[str, Any]] = []
    warnings: list[str] = []
    for name, expected_new in new["files"].items():
        check_cancelled(cancelled)
        path = safe_destination(root, name)
        expected_old = old["files"].get(name)
        if expected_old is None:
            if path.exists():
                raise UpdateError(f"A user file conflicts with this update: {name}. Move it to a safe location or use a new installation folder.")
        elif not path.is_file() or file_hash(path, cancelled) != expected_old:
            raise UpdateError(f"An application file has local changes: {name}. Save those changes separately or extract the update into a new folder.")
        if expected_old != expected_new:
            operations.append({"path": name, "old_hash": expected_old, "new_hash": expected_new})
    for name, expected_old in old["files"].items():
        if name in new["files"]:
            continue
        path = safe_destination(root, name)
        if not path.exists():
            continue
        if file_hash(path, cancelled) == expected_old:
            operations.append({"path": name, "old_hash": expected_old, "new_hash": None})
        else:
            warnings.append(f"Kept a locally changed file that is no longer shipped: {name}")
    # The launcher follows application files; the manifest is the final commit.
    operations.sort(key=lambda entry: (entry["path"] == EXE_NAME, entry["path"]))
    operations.append({"path": MANIFEST_NAME, "old_hash": old_hash, "new_hash": file_hash(staged / MANIFEST_NAME, cancelled)})
    return operations, tuple(warnings), old_hash


class FileRangeLock:
    """Byte 0 serializes setup; byte 1 protects running Studio instances."""

    def __init__(self, path: Path, *, shared: bool, offset: int) -> None:
        try:
            self.stream = path.open("a+b")
        except OSError as error:
            raise UpdateError("Studio could not open its installation lock. Check that this application folder is writable.") from error
        self.offset = offset
        self.closed = False
        try:
            if os.name == "nt":
                import msvcrt
                from ctypes import wintypes

                class OVERLAPPED(ctypes.Structure):
                    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

                self.overlapped = OVERLAPPED()
                self.overlapped.Offset = offset
                self.handle = msvcrt.get_osfhandle(self.stream.fileno())
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                lock = kernel.LockFileEx
                lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
                lock.restype = wintypes.BOOL
                self.unlock = kernel.UnlockFileEx
                self.unlock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(OVERLAPPED)]
                self.unlock.restype = wintypes.BOOL
                if not lock(self.handle, 1 | (0 if shared else 2), 0, 1, 0, ctypes.byref(self.overlapped)):
                    raise OSError(ctypes.get_last_error(), "Application lock is in use")
            else:
                import fcntl
                fcntl.lockf(self.stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB, 1, offset)
        except OSError as error:
            self.stream.close()
            raise UpdateError("Close other Studio windows and wait for any setup or EXE build to finish, then try the update again.") from error

    def close(self) -> None:
        if self.closed:
            return
        try:
            if os.name == "nt":
                self.unlock(self.handle, 0, 1, 0, ctypes.byref(self.overlapped))
            else:
                import fcntl
                fcntl.lockf(self.stream.fileno(), fcntl.LOCK_UN, 1, self.offset)
        finally:
            self.closed = True
            self.stream.close()

    def __enter__(self) -> "FileRangeLock":
        return self

    def __exit__(self, *unused: Any) -> None:
        self.close()


def acquire_running_instance(content_root: Path) -> FileRangeLock | None:
    """Acquire before importing Studio/Qt, including launcher.pyw bootstrap."""
    if sys.platform != "win32" or getattr(sys, "frozen", False):
        return None
    content = Path(content_root)
    if content.name != "app" or not (content.parent / EXE_NAME).is_file():
        return None
    content = safe_root(content)
    lock_path = safe_destination(content.parent, "app/" + LOCK_NAME)
    lock = FileRangeLock(lock_path, shared=True, offset=1)
    try:
        if os.path.lexists(content / PENDING_NAME):
            raise UpdateError("An application update needs recovery. Close Studio and open HoYo Project Studio.exe to finish it safely.")
        return lock
    except Exception:
        lock.close()
        raise


@contextmanager
def apply_locks(install_root: Path) -> Iterator[None]:
    lock_path = safe_destination(install_root, "app/" + LOCK_NAME)
    with FileRangeLock(lock_path, shared=False, offset=1):
        if os.name == "nt":
            with FileRangeLock(lock_path, shared=False, offset=0):
                yield
        else:
            # startup.py uses flock on POSIX; byte locks and flock are separate.
            import fcntl
            with lock_path.open("a+b") as stream:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise UpdateError("A Studio setup or EXE build is still running. Try the update again when it finishes.") from error
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_copy(source: Path, target: Path, expected_hash: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = target.stat() if target.exists() else None
    temporary = target.with_name(f".hoyo-update-{uuid.uuid4().hex}.tmp")
    try:
        digest = hashlib.sha256()
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            while True:
                block = incoming.read(CHUNK_BYTES)
                if not block:
                    break
                outgoing.write(block)
                digest.update(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if digest.hexdigest() != expected_hash:
            raise UpdateError("An update or backup file changed during installation.")
        shutil.copymode(source, temporary, follow_symlinks=False)
        if previous is not None and target.suffix.casefold() in (".py", ".pyw"):
            current = temporary.stat()
            if int(current.st_mtime) == int(previous.st_mtime):
                # Python's timestamp .pyc format uses whole seconds and size.
                # A fast same-size update must not reuse the previous code.
                # Two seconds also covers the timestamp resolution of FAT.
                next_timestamp = (previous.st_mtime_ns // 1_000_000_000 + 2) * 1_000_000_000
                os.utime(temporary, ns=(current.st_atime_ns, next_timestamp))
                if int(temporary.stat().st_mtime) == int(previous.st_mtime):
                    raise UpdateError("A Python file's timestamp could not be refreshed safely. The update was stopped.")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def rollback(job_directory: Path, root: Path, journal: dict[str, Any]) -> None:
    failures: list[str] = []
    for operation in reversed(journal["operations"]):
        if operation.get("state") not in ("pending", "done"):
            continue
        name = operation["path"]
        try:
            destination = safe_destination(root, name)
            current = file_hash(destination) if destination.exists() else None
            if current == operation["old_hash"]:
                continue
            if current != operation["new_hash"]:
                raise UpdateError("A destination changed outside the installer.")
            if operation["old_hash"] is None:
                destination.unlink()
            else:
                source = safe_destination(job_directory / "backup", name)
                if file_hash(source) != operation["old_hash"]:
                    raise UpdateError("A rollback backup failed verification.")
                atomic_copy(source, destination, operation["old_hash"])
        except (OSError, UpdateError):
            failures.append(name)
    journal["status"] = "rollback_failed" if failures else "rolled_back"
    journal["rollback_failures"] = failures
    write_json(job_directory / "journal.json", journal)
    if failures:
        raise UpdateError("The update stopped and some original files could not be restored. Your backups are in " + str(job_directory / "backup"))


def load_job(job_file: Path) -> tuple[dict[str, Any], Path, Path]:
    job_directory = safe_root(job_file.parent)
    if job_file.name != "job.json" or job_file != job_directory / "job.json":
        raise UpdateError("The prepared update job has an invalid location.")
    job = read_json(job_file)
    if type(job.get("schema_version")) is not int or job["schema_version"] != 1 or job.get("application") != APPLICATION:
        raise UpdateError("The prepared update job is incompatible.")
    root_text = job.get("install_root")
    if not isinstance(root_text, str) or not Path(root_text).is_absolute():
        raise UpdateError("The prepared update has no absolute application folder.")
    root = safe_root(Path(root_text))
    if root == job_directory or root in job_directory.parents or job_directory in root.parents:
        raise UpdateError("The update staging folder must be outside the installation.")
    version_tuple(job.get("current_version"))
    version_tuple(job.get("target_version"))
    if type(job.get("parent_pid")) is not int or job["parent_pid"] <= 0:
        raise UpdateError("The update has no valid Studio process to wait for.")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}", job.get("repository", "")):
        raise UpdateError("The update has no valid GitHub repository.")
    for key in ("old_manifest_sha256", "new_manifest_sha256", "helper_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", job.get(key, "")):
            raise UpdateError("The prepared update job has invalid verification metadata.")
    helper = safe_destination(job_directory, "update_apply.py")
    if file_hash(helper) != job["helper_sha256"]:
        raise UpdateError("The trusted update helper has changed. Prepare the update again.")
    original = safe_destination(job_directory, "original-manifest.json")
    candidate = safe_destination(job_directory / "staged", MANIFEST_NAME)
    if file_hash(original) != job["old_manifest_sha256"] or file_hash(candidate) != job["new_manifest_sha256"]:
        raise UpdateError("The prepared update's ownership metadata has changed.")
    validate_manifest(read_json(original), job["current_version"])
    validate_manifest(read_json(candidate), job["target_version"])
    return job, job_directory, root


def validate_journal(job: dict[str, Any], directory: Path, journal: dict[str, Any]) -> None:
    old = validate_manifest(read_json(safe_destination(directory, "original-manifest.json")), job["current_version"])
    new = validate_manifest(read_json(safe_destination(directory / "staged", MANIFEST_NAME)), job["target_version"])
    operations = journal.get("operations")
    if (journal.get("schema_version") != 1 or journal.get("status") not in {"applying", "installed", "rolled_back", "rollback_failed"}
            or not isinstance(operations, list) or len(operations) > MAX_FILES * 2):
        raise UpdateError("The interrupted update journal is invalid. Its backups were kept.")
    seen: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            raise UpdateError("The interrupted update journal is invalid.")
        name = vendor_path(operation.get("path"))
        if name in seen or operation.get("state") not in (None, "pending", "done"):
            raise UpdateError("The interrupted update journal contains duplicate or invalid operations.")
        seen.add(name)
        old_hash = job["old_manifest_sha256"] if name == MANIFEST_NAME else old["files"].get(name)
        new_hash = job["new_manifest_sha256"] if name == MANIFEST_NAME else new["files"].get(name)
        if (old_hash is None and new_hash is None) or operation.get("old_hash") != old_hash or operation.get("new_hash") != new_hash:
            raise UpdateError("The interrupted update journal does not match its verified packages.")
    mandatory = {name for name, value in new["files"].items() if old["files"].get(name) != value} | {MANIFEST_NAME}
    if not mandatory.issubset(seen):
        raise UpdateError("The interrupted update journal is missing required file operations.")


def validate_terminal_state(job: dict[str, Any], directory: Path, root: Path, journal: dict[str, Any]) -> None:
    installed = journal["status"] == "installed"
    old = read_json(directory / "original-manifest.json")
    new = read_json(directory / "staged" / MANIFEST_NAME)
    hashes = dict(new["files"]) if installed else {name: value for name, value in old["files"].items() if name in new["files"]}
    hashes[MANIFEST_NAME] = job["new_manifest_sha256"] if installed else job["old_manifest_sha256"]
    for operation in journal["operations"]:
        hashes[operation["path"]] = operation["new_hash"] if installed else operation["old_hash"]
    for name, expected in hashes.items():
        path = safe_destination(root, name)
        current = file_hash(path) if path.exists() else None
        if current != expected:
            raise UpdateError("The recovered application files are incomplete or have changed. Your recovery records and backups were kept: " + name)
    _assert_package_version(root, job["target_version"] if installed else job["current_version"])


def _set_pending(root: Path, directory: Path, job: dict[str, Any]) -> None:
    path = safe_destination(root, "app/" + PENDING_NAME)
    if path.exists():
        raise UpdateError("Another application update needs recovery. Open Studio's launcher before preparing another update.")
    write_json(path, {
        "schema_version": 1, "application": APPLICATION, "install_root": str(root),
        "job_file": str(directory / "job.json"), "helper_sha256": job["helper_sha256"],
    })


def _clear_pending(root: Path, directory: Path) -> None:
    path = safe_destination(root, "app/" + PENDING_NAME)
    if not path.exists():
        return
    pointer = read_json(path)
    if pointer.get("job_file") != str(directory / "job.json") or pointer.get("install_root") != str(root):
        raise UpdateError("The recovery pointer changed; the update records were kept for inspection.")
    path.unlink()


def apply_job(job_file: Path) -> tuple[str, ...]:
    job, directory, root = load_job(job_file)
    with apply_locks(root):
        # Rebind both manifests and the trusted helper after acquiring locks.
        job, directory, root = load_job(job_file)
        journal_path = directory / "journal.json"
        if journal_path.exists():
            previous = read_json(journal_path, 8 * MAX_MANIFEST_BYTES)
            validate_journal(job, directory, previous)
            if previous.get("status") in ("applying", "rollback_failed"):
                rollback(directory, root, previous)
                _clear_pending(root, directory)
                raise UpdateError("An interrupted update was rolled back. Open Studio and prepare the update again.")
            raise UpdateError("This prepared update has already been used. Check for updates again in Studio.")
        operations, warnings, _ = build_plan(root, directory / "staged", job["current_version"], job["target_version"], expected_manifest_hash=job["old_manifest_sha256"])
        backup = directory / "backup"
        backup.mkdir()
        journal = {"schema_version": 1, "status": "applying", "operations": operations}
        write_json(journal_path, journal)
        _set_pending(root, directory, job)
        try:
            for operation in operations:
                name = operation["path"]
                destination = safe_destination(root, name)
                current = file_hash(destination) if destination.exists() else None
                if current != operation["old_hash"]:
                    raise UpdateError(f"An application file changed during update installation: {name}")
                if current is not None:
                    saved = safe_destination(backup, name)
                    atomic_copy(destination, saved, current)
                operation["state"] = "pending"
                write_json(journal_path, journal)
                if operation["new_hash"] is None:
                    destination.unlink()
                else:
                    atomic_copy(safe_destination(directory / "staged", name), destination, operation["new_hash"])
                operation["state"] = "done"
                write_json(journal_path, journal)
            journal["status"] = "installed"
            write_json(journal_path, journal)
            write_json(directory / "result.json", {"status": "installed", "version": job["target_version"], "warnings": list(warnings)})
        except Exception as error:
            try:
                rollback(directory, root, journal)
                _clear_pending(root, directory)
            except UpdateError as rollback_error:
                raise rollback_error from error
            raise UpdateError("The update could not be installed. The previous version was restored. " + str(error)) from error
        _clear_pending(root, directory)
        return warnings


def recover_job(job_file: Path) -> int:
    """Called by startup before its setup lock, using the original helper."""
    try:
        job, directory, root = load_job(job_file)
        with FileRangeLock(safe_destination(directory, "helper.lock"), shared=False, offset=0), apply_locks(root):
            pointer_path = safe_destination(root, "app/" + PENDING_NAME)
            pointer = read_json(pointer_path)
            if (pointer.get("job_file") != str(job_file) or pointer.get("install_root") != str(root)
                    or pointer.get("helper_sha256") != job["helper_sha256"]):
                raise UpdateError("The update recovery pointer does not match its original helper.")
            journal_path = directory / "journal.json"
            if not journal_path.exists():
                raise UpdateError("The interrupted update journal is missing. Recovery stopped without changing your files; keep the update backups and extract the complete package into a new folder.")
            journal = read_json(journal_path, 8 * MAX_MANIFEST_BYTES)
            validate_journal(job, directory, journal)
            if journal["status"] in ("applying", "rollback_failed"):
                rollback(directory, root, journal)
                print("The interrupted Studio update was rolled back. Your previous version and projects were kept.", flush=True)
            elif journal["status"] == "installed":
                print("The Studio update had completed; verifying its startup recovery record.", flush=True)
            validate_terminal_state(job, directory, root, journal)
            _clear_pending(root, directory)
        return 0
    except (UpdateError, OSError) as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1


class ParentProcess:
    def __init__(self, pid: int) -> None:
        if os.name != "nt":
            raise UpdateError("The automatic update helper runs only on Windows.")
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        self.handle = open_process(0x00100000, False, pid)  # SYNCHRONIZE
        self.wait_function = kernel.WaitForSingleObject
        self.wait_function.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.wait_function.restype = wintypes.DWORD
        self.close_function = kernel.CloseHandle
        self.close_function.argtypes = [wintypes.HANDLE]
        self.close_function.restype = wintypes.BOOL
        if not self.handle:
            raise UpdateError("Studio's process could not be verified. Keep Studio open and prepare the update again.")

    def wait(self, cancel_path: Path, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if cancel_path.exists():
                raise UpdateError("The update was cancelled before Studio closed.", cancelled=True)
            result = self.wait_function(self.handle, 200)
            if result == 0:
                return
            if result != 258:
                raise UpdateError("Studio's shutdown could not be confirmed. No application files were changed.")
            if time.monotonic() >= deadline:
                raise UpdateError("Studio is still running. No files were changed. Close Studio and prepare the update again.")

    def close(self) -> None:
        self.close_function(self.handle)


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "HoYo Project Studio update", 0x10)
    else:
        print(message, file=sys.stderr)


def relaunch(root: Path) -> None:
    subprocess.Popen([str(root / EXE_NAME)], cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def run_helper(job_file: Path, *, parent_factory: Callable[[int], Any] = ParentProcess) -> int:
    directory = Path(job_file).parent
    root: Path | None = None
    parent_exited = False
    try:
        job, directory, root = load_job(job_file)
        # Lock the job too: a double-click/retry cannot start two installers.
        with FileRangeLock(safe_destination(directory, "helper.lock"), shared=False, offset=0):
            parent = parent_factory(job["parent_pid"])
            try:
                build_plan(root, directory / "staged", job["current_version"], job["target_version"], expected_manifest_hash=job["old_manifest_sha256"])
                write_json(directory / "ready.json", {"ready": True, "parent_pid": job["parent_pid"], "helper_pid": os.getpid()})
                parent.wait(directory / "cancel")
                parent_exited = True
                apply_job(job_file)
            finally:
                parent.close()
        relaunch(root)
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, UpdateError) else "The update could not finish: " + str(error)
        try:
            write_json(directory / "result.json", {"status": "cancelled" if getattr(error, "cancelled", False) else "failed", "message": message})
        except OSError:
            pass
        show_error(message + "\n\nUpdate details and any backups: " + str(directory))
        # A partial rollback must not start a potentially inconsistent app.
        journal_path = directory / "journal.json"
        try:
            rollback_failed = journal_path.exists() and read_json(journal_path, 8 * MAX_MANIFEST_BYTES).get("status") not in ("installed", "rolled_back")
        except UpdateError:
            rollback_failed = True
        if parent_exited and root is not None and not rollback_failed:
            try:
                relaunch(root)
            except OSError:
                pass
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a verified Studio update after the application exits.")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--job", type=Path)
    actions.add_argument("--recover", type=Path)
    arguments = parser.parse_args(argv)
    return recover_job(arguments.recover) if arguments.recover is not None else run_helper(arguments.job)


if __name__ == "__main__":
    raise SystemExit(main())
