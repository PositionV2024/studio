"""Use real archives and files to exercise read-only desktop release diffs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
import zipfile

import pytest

from launcher import update_apply as apply
from studio import update_compare as compare


CURRENT = "0.4.15"
TARGET = "0.4.16"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_files(version: str) -> dict[str, bytes]:
    # Stable file bodies make changes unrelated to version metadata visible.
    files = {name: f"# Application file: {name}\n".encode() for name in apply.REQUIRED_FILES}
    header = bytearray(128)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 80)
    header[80:86] = b"PE\0\0\x64\x86"
    files[apply.EXE_NAME] = bytes(header)
    files["app/studio/__init__.py"] = f'__version__ = "{version}"\n'.encode()
    files["app/pyproject.toml"] = f'[project]\nname = "hoyo-project-studio"\nversion = "{version}"\n'.encode()
    files["app/studio/model.py"] = b"def greeting():\n    return 'Desktop'\n"
    files["app/studio/obsolete.py"] = b"# Removed in the next release\n"
    return files


def manifest_bytes(files: dict[str, bytes], version: str) -> bytes:
    return (json.dumps({
        "schema_version": 1, "application": apply.APPLICATION, "version": version,
        "platform": "windows-x64", "layout": "compact-v1",
        "files": {name: digest(data) for name, data in sorted(files.items())},
    }, indent=2) + "\n").encode()


def write_package(root: Path, files: dict[str, bytes], version: str) -> None:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / apply.MANIFEST_NAME).write_bytes(manifest_bytes(files, version))


def write_zip(path: Path, files: dict[str, bytes], version: str = TARGET, *, extras=(), prefix="hoyo_project_studio/") -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(prefix + name, data)
        archive.writestr(prefix + apply.MANIFEST_NAME, manifest_bytes(files, version))
        for name, data in extras:
            archive.writestr(name, data)
    return path


def snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "Desktop Studio 雪"
    old = package_files(CURRENT)
    write_package(root, old, CURRENT)
    new = package_files(TARGET)
    new["app/studio/model.py"] = b"def greeting():\n    return 'Release'\n"
    del new["app/studio/obsolete.py"]
    new["app/studio/new.py"] = b"# New application file\n"
    archive = write_zip(tmp_path / "release.zip", new)
    return SimpleNamespace(root=root, app_root=root / "app", archive=archive, old=old, new=new, tmp=tmp_path)


def run(package, **kwargs):
    return compare.compare_release_archive(
        package.archive, package.app_root, kwargs.pop("expected_version", TARGET),
        current_version=kwargs.pop("current_version", CURRENT), **kwargs,
    )


def rows(result):
    return {entry.path: entry for entry in result.changes}


def test_comparison_uses_actual_bytes_and_leaves_application_unchanged(package):
    path = package.app_root / "studio/model.py"
    path.write_bytes(b"def greeting():\n    return 'My saved edit'\n")
    before = snapshot(package.root)
    progress = []
    result = run(package, progress=progress.append)
    changed = rows(result)
    assert result.current_version == CURRENT and result.release_version == TARGET
    assert changed["app/studio/new.py"].status == "added"
    assert changed["app/studio/obsolete.py"].status == "removed"
    model = changed["app/studio/model.py"]
    assert model.status == "modified" and model.locally_modified
    assert "-    return 'My saved edit'" in model.detail
    assert "+    return 'Release'" in model.detail
    assert "-    return 'Desktop'" not in model.detail
    assert model.desktop_size == path.stat().st_size
    assert model.release_size == len(package.new[model.path])
    assert any("1 saved application file" in message for message in result.warnings)
    assert len(result.changes) + result.unchanged_count == len(package.old.keys() | package.new.keys()) + 1
    assert snapshot(package.root) == before
    assert progress[0].startswith("Verifying application files")
    assert progress[-1].startswith("Comparing saved application files")


@pytest.mark.parametrize("prefix", ["", "Studio renamed by me/", "My downloads/Studio 雪/"])
def test_archive_name_and_wrapper_do_not_affect_comparison_paths(package, prefix):
    package.archive = package.tmp / "My own release filename.zip"
    write_zip(package.archive, package.new, prefix=prefix)
    before = snapshot(package.root)
    result = run(package)
    changed = rows(result)
    assert changed["app/studio/model.py"].status == "modified"
    assert changed["app/studio/new.py"].status == "added"
    assert changed["app/studio/obsolete.py"].status == "removed"
    assert "-    return 'Desktop'" in changed["app/studio/model.py"].detail
    assert "+    return 'Release'" in changed["app/studio/model.py"].detail
    assert all(name == apply.EXE_NAME or name.startswith("app/") for name in changed)
    assert snapshot(package.root) == before


@pytest.mark.parametrize("version", [CURRENT, "0.4.14", TARGET])
def test_comparison_works_for_same_older_and_newer_releases(package, version):
    new = package_files(version)
    new["app/studio/model.py"] = b"latest source has a meaningful difference\n"
    write_zip(package.archive, new, version)
    result = run(package, expected_version=version)
    assert result.release_version == version
    assert rows(result)["app/studio/model.py"].status == "modified"


def test_identical_release_has_zero_changes_and_counts_each_file_once(package):
    write_zip(package.archive, package.old, CURRENT)
    result = run(package, expected_version=CURRENT)
    assert result.changes == ()
    assert result.unchanged_count == len(package.old) + 1
    assert result.warnings == ()


def test_matching_latest_local_edit_is_unchanged_but_warns_about_baseline(package):
    (package.app_root / "studio/model.py").write_bytes(package.new["app/studio/model.py"])
    result = run(package)
    assert "app/studio/model.py" not in rows(result)
    assert any("1 saved application file" in message for message in result.warnings)


def test_missing_installed_file_is_added_from_release_and_marked_local(package):
    (package.app_root / "studio/model.py").unlink()
    result = rows(run(package))["app/studio/model.py"]
    assert result.status == "added" and result.locally_modified
    assert result.desktop_size is None
    assert "missing from the desktop" in result.detail
    assert "+    return 'Release'" in result.detail


def test_absent_on_both_sides_is_not_counted_as_unchanged_or_removed(package):
    (package.app_root / "studio/obsolete.py").unlink()
    result = run(package)
    assert "app/studio/obsolete.py" not in rows(result)
    assert any("also absent" in warning for warning in result.warnings)
    assert len(result.changes) + result.unchanged_count == len(package.old.keys() | package.new.keys())


def test_locally_edited_obsolete_file_shows_actual_removed_text(package):
    (package.app_root / "studio/obsolete.py").write_bytes(b"my local source edit\n")
    change = rows(run(package))["app/studio/obsolete.py"]
    assert change.locally_modified and change.status == "removed"
    assert "-my local source edit" in change.detail


def test_unowned_data_is_ignored_and_new_release_collision_is_never_read(package, monkeypatch):
    unowned = {
        "app/projects/test/main.py": b"private project",
        "app/.venv/Lib/site-packages/private.py": b"private dependency",
        "app/notes.txt": b"private notes",
        "app/credentials.json": b"secret credential",
        "app/studio/my_personal_script.py": b"personal addition",
        "app/studio/new.py": b"PRIVATE_UNOWNED_COLLISION",
    }
    for name, data in unowned.items():
        path = package.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    real_open = compare.os.open

    def guarded_open(path, *args, **kwargs):
        assert Path(path) not in {package.root / name for name in unowned}
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(compare.os, "open", guarded_open)
    result = run(package)
    changed = rows(result)
    assert set(unowned).intersection(changed) == {"app/studio/new.py"}
    conflict = changed["app/studio/new.py"]
    assert conflict.status == "unavailable" and conflict.desktop_size is None
    assert "unowned local file" in conflict.detail
    assert "PRIVATE_UNOWNED_COLLISION" not in str(result)


def test_unreadable_file_is_unavailable_never_fabricated_as_removed(package, monkeypatch):
    real_open = compare.os.open
    target = package.app_root / "studio/obsolete.py"

    def guarded_open(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError("private path hidden")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(compare.os, "open", guarded_open)
    result = run(package)
    change = rows(result)["app/studio/obsolete.py"]
    assert change.status == "unavailable" and change.release_size is None
    assert "could not be read" in change.detail
    assert "private path hidden" not in str(result)
    assert any("Could not compare" in message for message in result.warnings)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "parent_symlink"])
def test_nonordinary_desktop_files_are_unavailable_without_reading_target(package, kind):
    target = package.app_root / "studio/model.py"
    secret = package.tmp / "private.txt"
    secret.write_bytes(b"DO_NOT_READ_PRIVATE_TARGET")
    if kind == "parent_symlink":
        backup = package.app_root / "real-studio"
        (package.app_root / "studio").rename(backup)
        (package.app_root / "studio").symlink_to(backup, target_is_directory=True)
    else:
        target.unlink()
        if kind == "symlink":
            target.symlink_to(secret)
        elif kind == "hardlink":
            os.link(secret, target)
        else:
            target.mkdir()
    result = run(package)
    assert rows(result)["app/studio/model.py"].status == "unavailable"
    assert "DO_NOT_READ_PRIVATE_TARGET" not in str(result)


def test_reparse_file_is_not_considered_ordinary():
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_file_attributes=0x400)
    assert not compare._ordinary_file(info)


def test_linked_application_root_is_rejected(package):
    linked = package.tmp / "linked application"
    linked.symlink_to(package.root, target_is_directory=True)
    package.app_root = linked / "app"
    with pytest.raises(compare.ComparisonError, match="linked|redirected"):
        run(package)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_linked_download_is_rejected(package, kind):
    path = package.tmp / "linked.zip"
    if kind == "symlink":
        path.symlink_to(package.archive)
    else:
        os.link(package.archive, path)
    package.archive = path
    with pytest.raises(compare.ComparisonError) as caught:
        run(package)
    assert not isinstance(caught.value, compare.ReleaseArchiveError)


@pytest.mark.parametrize("kind", ["missing", "malformed", "version", "unowned", "duplicate"])
def test_missing_or_invalid_ownership_manifest_is_explicit_failure(package, kind):
    path = package.root / apply.MANIFEST_NAME
    if kind == "missing":
        path.unlink()
    elif kind == "malformed":
        path.write_bytes(b"not JSON")
    elif kind == "version":
        value = json.loads(path.read_bytes())
        value["version"] = "0.4.14"
        path.write_text(json.dumps(value))
    elif kind == "unowned":
        value = json.loads(path.read_bytes())
        value["files"]["app/credentials.json"] = "0" * 64
        path.write_text(json.dumps(value))
    else:
        path.write_bytes(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(compare.ComparisonError) as caught:
        run(package)
    # A local installation issue cannot classify a separately valid release as
    # unsafe: the dialog must still allow the complete ZIP for manual install.
    assert not isinstance(caught.value, compare.ReleaseArchiveError)


@pytest.mark.parametrize("bad_path", ["../escape.py", "hoyo_project_studio/app/../escape.py", "hoyo_project_studio/app/projects/main.py"])
def test_invalid_archive_is_rejected_before_any_comparison(package, bad_path):
    before = snapshot(package.root)
    write_zip(package.archive, package.new, extras=[(bad_path, b"unsafe")])
    progress = []
    with pytest.raises(compare.ReleaseArchiveError):
        run(package, progress=progress.append)
    assert snapshot(package.root) == before
    assert not any(message.startswith("Comparing") for message in progress)


def test_corrupt_release_checksum_never_becomes_a_diff(package):
    with zipfile.ZipFile(package.archive, "w") as archive:
        for name, data in package.new.items():
            archive.writestr("hoyo_project_studio/" + name, b"tampered" if name.endswith("model.py") else data)
        archive.writestr("hoyo_project_studio/" + apply.MANIFEST_NAME, manifest_bytes(package.new, TARGET))
    with pytest.raises(compare.ReleaseArchiveError, match="checksum"):
        run(package)


def test_downloaded_python_is_never_executed(package):
    marker = package.tmp / "downloaded-code-executed"
    package.new["app/studio/application.py"] = f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\n'.encode()
    write_zip(package.archive, package.new)
    run(package)
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["before", "verify", "compare", "diff"])
def test_cancellation_is_cooperative_and_temporary_files_are_cleaned(package, monkeypatch, mode):
    temporary = package.tmp / "comparison temporary"
    temporary.mkdir()
    monkeypatch.setattr(compare.tempfile, "gettempdir", lambda: str(temporary))
    cancelled = mode == "before"

    def progress(message):
        nonlocal cancelled
        if mode == "verify" and message.startswith("Verifying"):
            cancelled = True
        if mode == "compare" and message.startswith("Comparing"):
            cancelled = True

    if mode == "diff":
        original = compare._text_diff

        def intercept(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return original(*args, **kwargs)

        monkeypatch.setattr(compare, "_text_diff", intercept)
    before = snapshot(package.root)
    with pytest.raises(compare.ComparisonError) as caught:
        run(package, cancelled=lambda: cancelled, progress=progress)
    assert caught.value.cancelled
    assert not isinstance(caught.value, compare.ReleaseArchiveError)
    assert "cancelled" in str(caught.value)
    assert snapshot(package.root) == before
    assert list(temporary.iterdir()) == []


def test_expired_time_budget_fails_explicitly(package, monkeypatch):
    monkeypatch.setattr(compare, "MAX_COMPARISON_SECONDS", -1.0)
    with pytest.raises(compare.ComparisonError, match="time limit") as caught:
        run(package)
    assert not caught.value.cancelled


def test_never_creates_temporary_files_inside_application(package, monkeypatch):
    temporary = package.app_root / "temptarget"
    temporary.mkdir()
    before = snapshot(package.root)
    monkeypatch.setattr(compare.tempfile, "gettempdir", lambda: str(temporary))
    with pytest.raises(compare.ComparisonError, match="temporary folder outside"):
        run(package)
    assert list(temporary.iterdir()) == []
    assert snapshot(package.root) == before


def test_binary_changes_have_reason_and_actual_hashes(package):
    package.new[apply.EXE_NAME] += b"\x00\xff\x01new launcher"
    write_zip(package.archive, package.new)
    change = rows(run(package))[apply.EXE_NAME]
    assert change.status == "modified" and "Binary" in change.detail
    assert digest(package.old[apply.EXE_NAME]) in change.detail
    assert digest(package.new[apply.EXE_NAME]) in change.detail


@pytest.mark.parametrize("contents", [b"\xfflegacy text", b"nul\0inside", b"control\x1bcode"])
def test_unreadable_text_encoding_is_described_without_fake_diff(package, contents):
    package.new["app/studio/model.py"] = contents
    write_zip(package.archive, package.new)
    change = rows(run(package))["app/studio/model.py"]
    assert change.status == "modified" and "not available" in change.detail
    assert "@@" not in change.detail


def test_utf16_bom_text_has_a_readable_diff(package):
    package.new["app/studio/model.py"] = "Updated Unicode text 雪\n".encode("utf-16")
    write_zip(package.archive, package.new)
    detail = rows(run(package))["app/studio/model.py"].detail
    assert "+Updated Unicode text 雪" in detail and "UTF-16" in detail


def test_final_newline_difference_is_visible(package):
    package.new["app/studio/model.py"] = package.old["app/studio/model.py"].rstrip(b"\n")
    write_zip(package.archive, package.new)
    assert "No newline at end of file" in rows(run(package))["app/studio/model.py"].detail


def test_line_ending_only_difference_is_visible_in_plain_text_ui(package):
    package.new["app/studio/model.py"] = package.old["app/studio/model.py"].replace(b"\n", b"\r\n")
    write_zip(package.archive, package.new)
    detail = rows(run(package))["app/studio/model.py"].detail
    assert "Line endings: LF -> CRLF" in detail


def test_encoding_only_difference_is_described(package):
    package.new["app/studio/model.py"] = b"\xef\xbb\xbf" + package.old["app/studio/model.py"]
    write_zip(package.archive, package.new)
    detail = rows(run(package))["app/studio/model.py"].detail
    assert "text is the same" in detail and "byte-order mark" in detail


def test_empty_file_addition_and_removal_are_explicit(package):
    old = dict(package.old, **{"app/studio/empty_old.py": b""})
    write_package(package.root, old, CURRENT)
    package.new["app/studio/empty_new.py"] = b""
    write_zip(package.archive, package.new)
    result = rows(run(package))
    assert result["app/studio/empty_old.py"].status == "removed"
    assert "empty desktop file" in result["app/studio/empty_old.py"].detail
    assert result["app/studio/empty_new.py"].status == "added"
    assert "adds an empty file" in result["app/studio/empty_new.py"].detail


def test_large_file_status_is_exact_but_preview_is_bounded(package):
    # No huge compressible ZIP: this is below the archive's compression-ratio
    # guard, and only the preview's smaller budget should apply.
    package.new["app/studio/model.py"] = b"# a real source line\n" * 15_000
    write_zip(package.archive, package.new)
    change = rows(run(package))["app/studio/model.py"]
    assert change.status == "modified"
    assert "too large" in change.detail
    assert len(change.detail) < 500


def test_small_change_in_long_file_retains_correct_original_line_numbers(package):
    old = [f"line {index}\n" for index in range(5000)]
    new = old.copy()
    new[4500] = "new line 4500\n"
    package.old["app/studio/model.py"] = "".join(old).encode()
    package.new["app/studio/model.py"] = "".join(new).encode()
    write_package(package.root, package.old, CURRENT)
    write_zip(package.archive, package.new)
    detail = rows(run(package))["app/studio/model.py"].detail
    assert "@@ -4498,7 +4498,7 @@" in detail
    assert "-line 4500" in detail and "+new line 4500" in detail
    assert len(detail) < 1000


def test_many_changing_lines_do_not_trigger_quadratic_diff_work(package):
    package.old["app/studio/model.py"] = b"".join(f"old line {i}\n".encode() for i in range(1500))
    package.new["app/studio/model.py"] = b"".join(f"new line {i}\n".encode() for i in range(1500))
    write_package(package.root, package.old, CURRENT)
    write_zip(package.archive, package.new)
    detail = rows(run(package))["app/studio/model.py"].detail
    assert "Too many changing lines" in detail


def test_large_diff_is_truncated_without_losing_exact_change_status(package, monkeypatch):
    monkeypatch.setattr(compare, "MAX_DIFF_FILE_CHARS", 1200)
    package.old["app/studio/model.py"] = b"".join(f"old line {i}\n".encode() for i in range(100))
    package.new["app/studio/model.py"] = b"".join(f"new line {i}\n".encode() for i in range(100))
    write_package(package.root, package.old, CURRENT)
    write_zip(package.archive, package.new)
    change = rows(run(package))["app/studio/model.py"]
    assert change.status == "modified" and "preview truncated" in change.detail
    assert len(change.detail) <= 1200


def test_total_text_preview_budget_is_bounded_and_explained(package, monkeypatch):
    monkeypatch.setattr(compare, "MAX_TEXT_TOTAL_BYTES", 1)
    change = rows(run(package))["app/studio/model.py"]
    assert change.status == "modified" and "total text-preview size limit" in change.detail


def test_total_diff_preview_budget_does_not_hide_change_rows(package, monkeypatch):
    monkeypatch.setattr(compare, "MAX_DIFF_TOTAL_CHARS", 500)
    change = rows(run(package))["app/studio/model.py"]
    assert change.status == "modified" and "total diff-preview limit" in change.detail


def test_total_diff_work_budget_does_not_hide_change_rows(package, monkeypatch):
    monkeypatch.setattr(compare, "MAX_DIFF_TOTAL_LINE_PAIRS", 1)
    change = rows(run(package))["app/studio/model.py"]
    assert change.status == "modified" and "Too many changing lines" in change.detail


def test_bounded_manifest_preview_is_not_exempt_from_text_limits(package, monkeypatch):
    monkeypatch.setattr(compare, "MAX_TEXT_FILE_BYTES", 10)
    change = rows(run(package))[apply.MANIFEST_NAME]
    assert change.status == "modified" and "too large" in change.detail


def test_file_edited_during_read_is_unavailable(package, monkeypatch):
    original_open = compare.os.open
    target = package.app_root / "studio/model.py"

    def changing_open(path, *args, **kwargs):
        if Path(path) == target:
            target.write_bytes(b"File changed after initial inspection\n")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(compare.os, "open", changing_open)
    change = rows(run(package))["app/studio/model.py"]
    assert change.status == "unavailable" and "changed before" in change.detail
    assert "File changed after initial inspection" not in change.detail


def test_download_changed_during_verification_is_rejected(package, monkeypatch):
    original_stage = compare.update_apply.stage_archive

    def changing_stage(*args, **kwargs):
        result = original_stage(*args, **kwargs)
        with package.archive.open("ab") as stream:
            stream.write(b"tampered after verification")
        return result

    monkeypatch.setattr(compare.update_apply, "stage_archive", changing_stage)
    with pytest.raises(compare.ComparisonError, match="changed during verification"):
        run(package)


def test_desktop_size_limit_is_not_presented_as_normal_file_removal(package, monkeypatch):
    # Apply a small per-file limit through the reader call for this one file.
    original = compare._snapshot

    def bounded(root, name, limits, **kwargs):
        if Path(root) == package.root and name == "app/studio/obsolete.py":
            kwargs["maximum"] = 2
        return original(root, name, limits, **kwargs)

    monkeypatch.setattr(compare, "_snapshot", bounded)
    change = rows(run(package))["app/studio/obsolete.py"]
    assert change.status == "unavailable" and "size limit" in change.detail


def test_flat_source_layout_compares_only_known_application_locations(package):
    root = package.tmp / "source checkout"
    root.mkdir()
    for name, data in package.old.items():
        path = root / name.removeprefix("app/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name in ("notes.txt", "projects/main.py", ".venv/library.py", "qa/check.py", "studio/__pycache__/module.pyc"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private data, should be ignored")
    package.app_root = root
    result = run(package)
    assert rows(result)["app/studio/model.py"].status == "modified"
    assert rows(result)["app/studio/new.py"].status == "added"
    assert rows(result)["app/studio/obsolete.py"].status == "removed"
    assert all("private data" not in row.detail for row in result.changes)
    assert not any(row.locally_modified for row in result.changes)
    assert any("development folder" in warning for warning in result.warnings)


def test_unsupported_folder_and_frozen_build_fail_explicitly(package, monkeypatch):
    root = package.tmp / "other app"
    root.mkdir()
    package.app_root = root
    with pytest.raises(compare.ComparisonError, match="not a supported"):
        run(package)
    monkeypatch.setattr(compare.sys, "frozen", True, raising=False)
    with pytest.raises(compare.ComparisonError, match="separately built executable"):
        run(package)
