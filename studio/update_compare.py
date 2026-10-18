"""Read-only comparisons of a verified release and saved application files.

The release is extracted and verified in a private temporary folder. Its code
is never imported or run. An installed package manifest establishes ownership;
the comparison itself always reads the desktop's current bytes. Text previews
are prepared here so selecting a file in the dialog does no filesystem work.
"""

from __future__ import annotations

import ast
import codecs
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import tomllib
from typing import Callable

from launcher import build_update_package, update_apply


MAX_TEXT_FILE_BYTES = 256 * 1024
MAX_TEXT_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DIFF_FILE_CHARS = 64 * 1024
MAX_DIFF_TOTAL_CHARS = 4 * 1024 * 1024
MAX_TEXT_LINES = 20_000
MAX_LINE_CHARS = 16_384
MAX_DIFF_LINE_PAIRS = 2_000_000
MAX_DIFF_TOTAL_LINE_PAIRS = 20_000_000
MAX_COMPARISON_SECONDS = 120.0
MAX_WARNINGS = 100
_CONTEXT_LINES = 3


class ComparisonError(RuntimeError):
    def __init__(self, message: str, *, cancelled: bool = False) -> None:
        super().__init__(message)
        self.cancelled = cancelled


class ReleaseArchiveError(ComparisonError):
    """The release ZIP failed validation, so it cannot be installed."""


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str
    desktop_size: int | None
    release_size: int | None
    detail: str
    locally_modified: bool = False


@dataclass(frozen=True)
class ReleaseComparison:
    current_version: str
    release_version: str
    changes: tuple[FileChange, ...]
    unchanged_count: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Snapshot:
    size: int
    digest: str
    data: bytes | None


@dataclass
class _Limits:
    cancelled: Callable[[], bool] | None
    deadline: float
    local_bytes: int = 0
    text_bytes: int = 0
    diff_chars: int = 0
    diff_line_pairs: int = 0

    def poll(self) -> bool:
        # Return cancellation to the verifier's own exception handling. Raising
        # an unrelated exception inside stage_archive would hide cancellation.
        if self.cancelled is not None and self.cancelled():
            return True
        if time.monotonic() > self.deadline:
            raise update_apply.UpdateError(
                "File comparison exceeded its time limit. Close other file operations and try again."
            )
        return False

    def check(self) -> None:
        update_apply.check_cancelled(self.poll)


class _FileUnavailable(Exception):
    def __init__(self, message: str, *, size: int | None = None) -> None:
        super().__init__(message)
        self.size = size


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _ordinary_file(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not _is_link(info)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _revision(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_path(root: Path, relative: str) -> Path:
    try:
        return update_apply.safe_destination(root, relative)
    except update_apply.UpdateError as error:
        raise _FileUnavailable(str(error)) from error


def _snapshot(
    root: Path,
    relative: str,
    limits: _Limits,
    *,
    capture_limit: int = MAX_TEXT_FILE_BYTES,
    maximum: int = update_apply.MAX_FILE_BYTES,
    count_local: bool = True,
) -> _Snapshot | None:
    """Read one ordinary file once, detecting replacement or concurrent edits."""
    limits.check()
    path = _safe_path(root, relative)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _FileUnavailable("The desktop file could not be inspected.") from error
    if not _ordinary_file(before):
        raise _FileUnavailable("Linked, redirected, hard-linked, or special files cannot be compared.")
    if before.st_size > maximum:
        raise _FileUnavailable("The file exceeds the comparison size limit.", size=before.st_size)
    if count_local and limits.local_bytes + before.st_size > update_apply.MAX_TOTAL_BYTES:
        raise update_apply.UpdateError("The desktop application exceeds the comparison size limit.")

    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if not _ordinary_file(opened) or _revision(opened) != _revision(before):
            raise _FileUnavailable("The file changed before it could be read. Save it and check again.")
        digest = hashlib.sha256()
        captured = bytearray() if before.st_size <= capture_limit else None
        received = 0
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            while True:
                limits.check()
                block = stream.read(update_apply.CHUNK_BYTES)
                if not block:
                    break
                received += len(block)
                if received > before.st_size or received > maximum:
                    raise _FileUnavailable("The file changed while it was being read. Save it and check again.")
                digest.update(block)
                if captured is not None:
                    captured.extend(block)
            after = os.fstat(stream.fileno())
        # Recheck the path too, without following a substituted link.
        after_path = _safe_path(root, relative).lstat()
        if (received != before.st_size or _revision(after) != _revision(before)
                or not _ordinary_file(after_path) or _revision(after_path) != _revision(before)):
            raise _FileUnavailable("The file changed while it was being read. Save it and check again.")
        if count_local:
            limits.local_bytes += received
        return _Snapshot(received, digest.hexdigest(), bytes(captured) if captured is not None else None)
    except OSError as error:
        raise _FileUnavailable("The file could not be read. Check its permissions and try again.", size=before.st_size) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata field")
        result[key] = value
    return result


def _load_installed_manifest(root: Path, current_version: str, limits: _Limits) -> tuple[dict[str, str], _Snapshot]:
    try:
        snapshot = _snapshot(
            root, update_apply.MANIFEST_NAME, limits,
            capture_limit=update_apply.MAX_MANIFEST_BYTES,
            maximum=update_apply.MAX_MANIFEST_BYTES,
        )
        if snapshot is None:
            raise ComparisonError(
                "File comparison is unavailable because this installation has no package manifest. "
                "Open Studio from a complete application ZIP."
            )
        value = json.loads(snapshot.data, object_pairs_hook=_json_pairs)
        if not isinstance(value, dict):
            raise ValueError("metadata is not an object")
        manifest = update_apply.validate_manifest(value, current_version)
        return manifest["files"], snapshot
    except (_FileUnavailable, UnicodeError, ValueError, TypeError) as error:
        raise ComparisonError("The installed package manifest could not be read safely. File comparison is unavailable.") from error


def _flat_inventory(root: Path, current_version: str, limits: _Limits) -> dict[str, None]:
    """Inspect only known source locations; never walk projects or environments."""
    try:
        init = _snapshot(root, "studio/__init__.py", limits, count_local=False)
        project = _snapshot(root, "pyproject.toml", limits, count_local=False)
        if init is None or project is None or init.data is None or project.data is None:
            raise ValueError("missing source identity")
        module = ast.parse(init.data.decode("utf-8"))
        versions = [ast.literal_eval(node.value) for node in module.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
        metadata = tomllib.loads(project.data.decode("utf-8"))
        if (versions != [current_version] or metadata.get("project", {}).get("name") != update_apply.APPLICATION
                or metadata.get("project", {}).get("version") != current_version):
            raise ValueError("source identity mismatch")
    except (_FileUnavailable, UnicodeError, ValueError, SyntaxError) as error:
        raise ComparisonError("This folder is not a supported Studio source or desktop installation. File comparison is unavailable.") from error

    inventory: dict[str, None] = {}

    def include(relative: str) -> bool:
        candidate = "app/" + relative
        try:
            update_apply.vendor_path(candidate)
        except update_apply.UpdateError:
            return False
        if Path(relative).suffix.lower() in build_update_package.EXCLUDED_SUFFIXES:
            return False
        if relative not in build_update_package.BUNDLED_CONFIGURATION and any(
            part.startswith(".") or part in build_update_package.EXCLUDED_DIRECTORIES
            for part in relative.split("/")
        ):
            return False
        return True

    for relative in build_update_package.ROOT_FILES:
        limits.check()
        path = root / relative
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        if include(relative):
            inventory["app/" + relative] = None
    try:
        (root / update_apply.EXE_NAME).lstat()
        inventory[update_apply.EXE_NAME] = None
    except FileNotFoundError:
        pass

    visited_entries = 0
    pending = [root / name for name in build_update_package.PACKAGE_DIRECTORIES]
    while pending:
        limits.check()
        directory = pending.pop()
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ComparisonError("A linked or inaccessible source folder prevents a complete file comparison.")
        with os.scandir(directory) as entries:
            for entry in entries:
                limits.check()
                visited_entries += 1
                if visited_entries > update_apply.MAX_FILES * 2:
                    raise ComparisonError("The application source folder contains too many entries to compare.")
                relative = (directory / entry.name).relative_to(root).as_posix()
                # The four bundled .studio files are the only permitted hidden
                # directory contents; all other hidden/runtime trees are skipped.
                allowed_metadata_directory = relative == "templates/streamlit_demo/.studio"
                if not include(relative) and not allowed_metadata_directory:
                    continue
                info = entry.stat(follow_symlinks=False)
                if _is_link(info):
                    raise ComparisonError("A linked source file or folder prevents a complete file comparison.")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(directory / entry.name)
                else:
                    inventory["app/" + relative] = None
                if len(inventory) > update_apply.MAX_FILES:
                    raise ComparisonError("The application source folder contains too many files to compare.")
    return inventory


def _decode_text(data: bytes) -> tuple[str, str] | None:
    try:
        if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
            value, encoding = data.decode("utf-32"), "UTF-32 with a byte-order mark"
        elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            value, encoding = data.decode("utf-16"), "UTF-16 with a byte-order mark"
        elif data.startswith(codecs.BOM_UTF8):
            value, encoding = data.decode("utf-8-sig"), "UTF-8 with a byte-order mark"
        else:
            value, encoding = data.decode("utf-8"), "UTF-8"
    except UnicodeError:
        return None
    if any((ord(character) < 32 and character not in "\t\r\n\f") or 127 <= ord(character) < 160 for character in value):
        return None
    return value, encoding


def _unified_range(start: int, stop: int) -> str:
    length = stop - start
    beginning = start + 1
    if length == 1:
        return str(beginning)
    if not length:
        beginning -= 1
    return f"{beginning},{length}"


def _line_endings(value: str) -> str:
    kinds = []
    if "\r\n" in value:
        kinds.append("CRLF")
    remaining = value.replace("\r\n", "")
    if "\n" in remaining:
        kinds.append("LF")
    if "\r" in remaining:
        kinds.append("CR")
    return ", ".join(kinds) if kinds else "no line breaks"


def _text_diff(
    path: str, desktop: _Snapshot | None, release: _Snapshot | None,
    version: str, limits: _Limits,
) -> str:
    if (desktop is not None and (desktop.data is None or desktop.size > MAX_TEXT_FILE_BYTES)) or (
        release is not None and (release.data is None or release.size > MAX_TEXT_FILE_BYTES)
    ):
        return "File contents differ. The file is too large for a text preview (256 KiB per file)."
    old = desktop.data if desktop is not None else b""
    new = release.data if release is not None else b""
    assert old is not None and new is not None
    input_size = len(old) + len(new)
    if limits.text_bytes + input_size > MAX_TEXT_TOTAL_BYTES:
        return "File contents differ. The comparison's total text-preview size limit has been reached."
    limits.text_bytes += input_size
    old_text, new_text = _decode_text(old), _decode_text(new)
    if old_text is None or new_text is None:
        lines = ["Binary or non-UTF text file. A readable text diff is not available."]
        if desktop is not None:
            lines.append(f"Desktop SHA-256: {desktop.digest}")
        if release is not None:
            lines.append(f"Release SHA-256: {release.digest}")
        return "\n".join(lines)
    if old_text[0] == new_text[0]:
        if desktop is None:
            return "The release adds an empty file."
        if release is None:
            return "The release does not include this empty desktop file."
        return f"The text is the same, but the file bytes or encoding differ.\nDesktop: {old_text[1]}\nRelease: {new_text[1]}"

    a, b = old_text[0].splitlines(keepends=True), new_text[0].splitlines(keepends=True)
    if len(a) + len(b) > MAX_TEXT_LINES or any(len(line) > MAX_LINE_CHARS for line in (*a, *b)):
        return "File contents differ. There are too many lines, or a line is too long, for a bounded text preview."
    # Trim equal ends before calculating the work bound. A small change in a
    # long source file can still have a useful diff without quadratic work.
    prefix = 0
    while prefix < min(len(a), len(b)) and a[prefix] == b[prefix]:
        if prefix % 128 == 0:
            limits.check()
        prefix += 1
    suffix = 0
    while suffix < min(len(a), len(b)) - prefix and a[-suffix - 1] == b[-suffix - 1]:
        if suffix % 128 == 0:
            limits.check()
        suffix += 1
    offset = max(0, prefix - _CONTEXT_LINES)
    end_a = min(len(a), len(a) - suffix + _CONTEXT_LINES)
    end_b = min(len(b), len(b) - suffix + _CONTEXT_LINES)
    a, b = a[offset:end_a], b[offset:end_b]
    line_pairs = len(a) * len(b)
    if line_pairs > MAX_DIFF_LINE_PAIRS or limits.diff_line_pairs + line_pairs > MAX_DIFF_TOTAL_LINE_PAIRS:
        return "File contents differ. Too many changing lines would make a text preview slow; the file comparison is still complete."
    limits.diff_line_pairs += line_pairs
    available = min(MAX_DIFF_FILE_CHARS, MAX_DIFF_TOTAL_CHARS - limits.diff_chars)
    if available < 1024:
        return "File contents differ. The comparison's total diff-preview limit has been reached."
    output: list[str] = []
    length = 0
    truncated = False

    def append(value: str) -> bool:
        nonlocal length, truncated
        limits.check()
        # Reserve space for a complete explanation of truncation.
        remaining = available - 100 - length
        if len(value) > remaining:
            if remaining > 0:
                output.append(value[:remaining])
                length += remaining
            truncated = True
            return False
        output.append(value)
        length += len(value)
        return True

    append(f"--- Desktop/{path}" + (" (missing)" if desktop is None else "") + "\n")
    append(f"+++ Release {version}/{path}" + (" (missing)" if release is None else "") + "\n")
    if old_text[1] != new_text[1] and desktop is not None and release is not None:
        append(f"Encoding: {old_text[1]} -> {new_text[1]}\n")
    if desktop is not None and release is not None and _line_endings(old_text[0]) != _line_endings(new_text[0]):
        append(f"Line endings: {_line_endings(old_text[0])} -> {_line_endings(new_text[0])}\n")
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=True)
    for group in matcher.get_grouped_opcodes(_CONTEXT_LINES):
        limits.check()
        first, last = group[0], group[-1]
        if not append(f"@@ -{_unified_range(first[1] + offset, last[2] + offset)} +{_unified_range(first[3] + offset, last[4] + offset)} @@\n"):
            break
        for tag, i1, i2, j1, j2 in group:
            sources = [(" ", a[i1:i2])] if tag == "equal" else []
            if tag in ("replace", "delete"):
                sources.append(("-", a[i1:i2]))
            if tag in ("replace", "insert"):
                sources.append(("+", b[j1:j2]))
            for marker, lines in sources:
                for line in lines:
                    # Qt displays these as plain text. Keep a marker for an
                    # absent final newline instead of silently normalizing it.
                    rendered = marker + line
                    if not line.endswith(("\n", "\r")):
                        rendered += "\n\\ No newline at end of file\n"
                    if not append(rendered):
                        break
                if truncated:
                    break
            if truncated:
                break
        if truncated:
            break
    if truncated:
        output.append("\n[Text preview truncated; the file's change status uses all file bytes.]\n")
    result = "".join(output)
    limits.diff_chars += len(result)
    return result


def compare_release_archive(
    archive: Path,
    app_root: Path,
    expected_version: str,
    *,
    current_version: str,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
) -> ReleaseComparison:
    """Compare saved desktop bytes with a complete release, in either direction.

    Run this function on a worker thread. ``added`` means present in the release
    but missing on this desktop; ``removed`` means present on the desktop but
    absent from the release. Unsafe/unreadable files use ``unavailable``. A new
    release path occupied by an unowned user file is reported without reading
    that file. Projects, environments, settings, and unsaved editor buffers are
    outside this comparison.
    """
    limits = _Limits(cancelled, time.monotonic() + MAX_COMPARISON_SECONDS)
    warnings: list[str] = []
    omitted_warnings = 0

    def warn(message: str) -> None:
        nonlocal omitted_warnings
        if len(warnings) < MAX_WARNINGS:
            warnings.append(message)
        else:
            omitted_warnings += 1

    try:
        limits.check()
        update_apply.version_tuple(current_version)
        update_apply.version_tuple(expected_version)
        if getattr(sys, "frozen", False):
            raise ComparisonError("File comparison is unavailable in a separately built executable. Use the complete desktop application package.")
        content = update_apply.safe_root(Path(app_root))
        compact = content.name == "app"
        root = update_apply.safe_root(content.parent) if compact else content
        old_manifest: _Snapshot | None = None
        if compact:
            owned, old_manifest = _load_installed_manifest(root, current_version, limits)
        else:
            owned = _flat_inventory(root, current_version, limits)
            warn("This development folder has no installed package baseline. Known application source files are compared, but local edits since installation cannot be identified.")
        temporary_root = update_apply.safe_root(Path(tempfile.gettempdir()))
        if temporary_root == root or temporary_root.is_relative_to(root):
            raise ComparisonError("File comparison needs a temporary folder outside the application folder.")
        archive = Path(os.path.abspath(archive))
        archive_parent = update_apply.safe_root(archive.parent)
        archive = _safe_path(archive_parent, archive.name)
        archive_info = archive.lstat()
        if not _ordinary_file(archive_info):
            raise ComparisonError("The downloaded release must be an ordinary ZIP file.")
        with tempfile.TemporaryDirectory(prefix="hoyo-compare-", dir=temporary_root) as temporary:
            staged = Path(temporary) / "release"
            try:
                new_manifest = update_apply.stage_archive(
                    archive, staged, expected_version, cancelled=limits.poll, progress=progress,
                )
            except update_apply.UpdateError as error:
                if error.cancelled:
                    raise ComparisonError("File comparison was cancelled.", cancelled=True) from error
                raise ReleaseArchiveError(str(error)) from error
            limits.check()
            if _revision(archive.lstat()) != _revision(archive_info):
                raise ComparisonError("The downloaded release changed during verification. Check for updates again.")
            new_files = dict(new_manifest["files"])
            # The ownership manifest is itself an application file, but cannot
            # hash itself. Include it once using the already verified staged ZIP.
            new_metadata = _snapshot(
                staged, update_apply.MANIFEST_NAME, limits,
                capture_limit=update_apply.MAX_MANIFEST_BYTES,
                maximum=update_apply.MAX_MANIFEST_BYTES, count_local=False,
            )
            assert new_metadata is not None
            new_files[update_apply.MANIFEST_NAME] = new_metadata.digest
            old_files: dict[str, str | None] = dict(owned)
            if compact:
                assert old_manifest is not None
                old_files[update_apply.MANIFEST_NAME] = old_manifest.digest
            names = sorted(old_files.keys() | new_files.keys(), key=lambda name: (name.casefold(), name))
            if len(names) > update_apply.MAX_FILES * 2 + 1:
                raise ComparisonError("There are too many application files to compare.")
            changes: list[FileChange] = []
            unchanged = 0
            local_changes = 0
            for index, name in enumerate(names, 1):
                limits.check()
                if progress is not None:
                    progress(f"Comparing saved application files ({index}/{len(names)})…")
                in_release = name in new_files
                new_path = staged / name if in_release else None
                new_size = new_path.stat().st_size if new_path is not None else None
                local_name = name if compact or name == update_apply.EXE_NAME else name.removeprefix("app/")
                try:
                    path = _safe_path(root, local_name)
                    if name not in old_files:
                        try:
                            path.lstat()
                        except FileNotFoundError:
                            pass
                        else:
                            # It is explicitly outside the installed ownership
                            # list: show the conflict without reading user data.
                            raise _FileUnavailable("An unowned local file occupies this release path. Its contents were not read or compared.")
                    desktop = old_manifest if compact and name == update_apply.MANIFEST_NAME else _snapshot(root, local_name, limits)
                except _FileUnavailable as error:
                    changes.append(FileChange(name, "unavailable", error.size, new_size, str(error)))
                    warn(f"Could not compare {name}: {error}")
                    continue
                baseline = old_files.get(name)
                locally_modified = baseline is not None and (desktop is None or desktop.digest != baseline)
                if locally_modified:
                    local_changes += 1
                if desktop is None and not in_release:
                    if locally_modified:
                        warn(f"Missing desktop file is also absent from this release: {name}")
                    # Neither side has a file; do not inflate unchanged counts.
                    continue
                if desktop is not None and in_release and desktop.digest == new_files[name]:
                    unchanged += 1
                    continue
                status = "added" if desktop is None else "removed" if not in_release else "modified"
                release_snapshot: _Snapshot | None = None
                if in_release:
                    if name == update_apply.MANIFEST_NAME:
                        release_snapshot = new_metadata
                    elif new_size is not None and new_size <= MAX_TEXT_FILE_BYTES:
                        release_snapshot = _snapshot(staged, name, limits, count_local=False)
                        if release_snapshot is None or release_snapshot.digest != new_files[name]:
                            raise ComparisonError("A verified release file changed during comparison. Check for updates again.")
                    else:
                        assert new_size is not None
                        release_snapshot = _Snapshot(new_size, new_files[name], None)
                detail = _text_diff(name, desktop, release_snapshot, expected_version, limits)
                if locally_modified:
                    prefix = ("This file is missing from the desktop although it belongs to the installed package."
                              if desktop is None else "This desktop file has local changes compared with the installed package.")
                    detail = prefix + "\n\n" + detail
                changes.append(FileChange(name, status, desktop.size if desktop is not None else None, new_size, detail, locally_modified))
            limits.check()
            if local_changes:
                warn(f"{local_changes} saved application file(s) differ from the installed package baseline, including missing files. Unsaved editor changes are not included.")
            if omitted_warnings:
                warnings.append(f"{omitted_warnings} additional file warnings are shown in the corresponding file details.")
            return ReleaseComparison(current_version, expected_version, tuple(changes), unchanged, tuple(warnings))
    except ComparisonError:
        raise
    except update_apply.UpdateError as error:
        message = "File comparison was cancelled." if error.cancelled else str(error)
        raise ComparisonError(message, cancelled=error.cancelled) from error
    except (_FileUnavailable, OSError, ValueError) as error:
        raise ComparisonError("The application files could not be compared safely. Check file access and try again.") from error
