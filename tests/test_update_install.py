"""Exercise archive ownership, rollback and actual recovery/lock processes.

Windows process handles/GUI launch boundaries are substituted on Linux; the
archive, filesystem, journal, recovery subprocess and POSIX locks are real.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import py_compile
import stat
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
import warnings
import zipfile

import pytest

from launcher import startup, update_apply as apply
from studio import update_install as install


CURRENT = "0.4.13"
TARGET = "0.4.14"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def launcher_bytes() -> bytes:
    header = bytearray(128)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 80)
    header[80:86] = b"PE\0\0\x64\x86"
    return bytes(header)


def package_files(version: str) -> dict[str, bytes]:
    result = {name: f"# Studio {version}: {name}\n".encode() for name in apply.REQUIRED_FILES}
    result[apply.EXE_NAME] = launcher_bytes() + version.encode()
    result["app/studio/__init__.py"] = f'__version__ = "{version}"\n'.encode()
    result["app/pyproject.toml"] = f'[project]\nname = "hoyo-project-studio"\nversion = "{version}"\n'.encode()
    result["app/launcher/update_apply.py"] = Path(apply.__file__).read_bytes()
    result["app/studio/obsolete.py"] = b"# An old, removable application module\n"
    return result


def manifest_bytes(files: dict[str, bytes], version: str) -> bytes:
    return json.dumps({
        "schema_version": 1, "application": "hoyo-project-studio", "version": version,
        "platform": "windows-x64", "layout": "compact-v1",
        "files": {name: digest(value) for name, value in files.items()},
    }, indent=2).encode()


def write_package(root: Path, files: dict[str, bytes], version: str) -> None:
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    (root / apply.MANIFEST_NAME).write_bytes(manifest_bytes(files, version))


def archive_at(path: Path, files: dict[str, bytes], version: str = TARGET, *, extras: list[tuple[object, bytes]] = ()) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name, value in files.items():
            package.writestr("hoyo_project_studio/" + name, value)
        package.writestr("hoyo_project_studio/" + apply.MANIFEST_NAME, manifest_bytes(files, version))
        for name, value in extras:
            package.writestr(name, value)
    return path


def snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and path.name not in (apply.LOCK_NAME, apply.PENDING_NAME)}


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / "Studio application 雪 with spaces"
    files = package_files(CURRENT)
    write_package(root, files, CURRENT)
    for name, contents in {
        "app/.venv/Scripts/python.exe": b"existing interpreter",
        "app/.venv/Lib/site-packages/user-package.txt": b"installed packages",
        "app/my-project/main.py": b"user project, do not overwrite",
        "app/notes.txt": b"user notes",
        "app/credentials.json": b"user connection data",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    monkeypatch.setattr(install, "_is_windows", lambda: True)
    monkeypatch.setattr(install, "__version__", CURRENT)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "User local data"))
    archive = archive_at(tmp_path / "update.zip", package_files(TARGET))
    return SimpleNamespace(root=root, app_root=root / "app", archive=archive, files=files, tmp=tmp_path)


def prepared(package, **kwargs):
    return install.prepare_install(package.archive, package.app_root, TARGET, "owner/studio", **kwargs)


def test_preparation_does_not_change_live_files_and_installs_only_managed_files(package):
    before = snapshot(package.root)
    progress = []
    result = prepared(package, progress=progress.append)
    assert snapshot(package.root) == before
    assert result.job_directory.is_relative_to(install.update_cache_root())
    assert result.version == TARGET and result.repository == "owner/studio"
    assert result.job_directory / "update_apply.py" != result.job_directory / "staged/app/launcher/update_apply.py"
    assert progress[-1] == "Update verified. Ready to save your work and restart."

    apply.apply_job(result.job_file)

    manifest = json.loads((package.root / apply.MANIFEST_NAME).read_bytes())
    assert manifest["version"] == TARGET
    assert (package.app_root / "studio/__init__.py").read_text() == f'__version__ = "{TARGET}"\n'
    for name in before.keys() - package.files.keys() - {apply.MANIFEST_NAME}:
        assert (package.root / name).read_bytes() == before[name]
    assert {path.name for path in package.root.iterdir()} == {apply.EXE_NAME, "app"}
    assert not (package.app_root / apply.PENDING_NAME).exists()
    assert json.loads((result.job_directory / "journal.json").read_text())["status"] == "installed"
    assert (result.job_directory / "backup/app/studio/__init__.py").read_bytes() == before["app/studio/__init__.py"]


@pytest.mark.parametrize("version", [CURRENT, "0.4.12", "0.3.999"])
def test_never_installs_current_or_older_version(package, version):
    archive_at(package.archive, package_files(version), version)
    with pytest.raises(install.UpdateInstallError, match="newer"):
        install.prepare_install(package.archive, package.app_root, version, "owner/studio")


@pytest.mark.parametrize("version", ["0.4.13b1", "v0.4.13", "00.4.13", "0.4", "0.4.13.0", "-1.4.13", "0.4.9999999999", "0.4.13+local"])
def test_invalid_versions_are_rejected(version):
    with pytest.raises(apply.UpdateError, match="stable version"):
        apply.version_tuple(version)


def test_numeric_versions_compare_correctly():
    assert apply.version_tuple("0.4.9") < apply.version_tuple("0.4.13") < apply.version_tuple("0.10.0")


def test_missing_ownership_manifest_requires_manual_install(package):
    (package.root / apply.MANIFEST_NAME).unlink()
    supported, reason = install.installation_support(package.app_root)
    assert not supported and "ownership manifest" in reason
    with pytest.raises(install.UpdateInstallError, match="ownership manifest"):
        prepared(package)


def test_running_version_disagrees_with_disk_manifest(package, monkeypatch):
    monkeypatch.setattr(install, "__version__", "0.4.12")
    with pytest.raises(install.UpdateInstallError, match="version"):
        prepared(package)


@pytest.mark.parametrize("changed", ["app/studio/application.py", "app/requirements.txt", apply.EXE_NAME])
def test_local_application_changes_are_not_overwritten(package, changed):
    path = package.root / changed
    path.write_bytes(b"my custom version")
    before = snapshot(package.root)
    with pytest.raises(install.UpdateInstallError, match="local changes|launcher"):
        prepared(package)
    assert snapshot(package.root) == before


def test_new_application_file_cannot_overwrite_an_unowned_user_file(package):
    candidate = package_files(TARGET)
    candidate["app/studio/my_module.py"] = b"release module"
    archive_at(package.archive, candidate)
    (package.app_root / "studio/my_module.py").write_bytes(b"user's module")
    with pytest.raises(install.UpdateInstallError, match="user file conflicts"):
        prepared(package)
    assert (package.app_root / "studio/my_module.py").read_bytes() == b"user's module"


def test_update_keeps_all_five_shipped_metadata_files_without_touching_project_metadata(package):
    shipped = {
        "app/.gitignore",
        "app/templates/streamlit_demo/.studio/defaults.json",
        "app/templates/streamlit_demo/.studio/draft.json",
        "app/templates/streamlit_demo/.studio/project.json",
        "app/templates/streamlit_demo/.studio/published.json",
    }
    old = package_files(CURRENT)
    new = package_files(TARGET)
    for name in shipped:
        old[name] = b"# original ignore rules\n" if name.endswith(".gitignore") else b'{"template": "original"}\n'
        new[name] = b"# updated ignore rules\n" if name.endswith(".gitignore") else b'{"template": "updated"}\n'
    write_package(package.root, old, CURRENT)
    project_metadata = package.app_root / "my-project/.studio/defaults.json"
    project_metadata.parent.mkdir()
    project_metadata.write_bytes(b'{"user_work": true}\n')
    archive_at(package.archive, new)

    result = prepared(package)
    apply.apply_job(result.job_file)

    for name in shipped:
        assert (package.root / name).read_bytes() == new[name]
        assert (result.job_directory / "backup" / name).read_bytes() == old[name]
    assert project_metadata.read_bytes() == b'{"user_work": true}\n'


@pytest.mark.parametrize("name", [
    "app/templates/another_demo/.studio/defaults.json",
    "app/templates/streamlit_demo/.studio/credentials.json",
    "app/templates/streamlit_demo/.studio/private.json",
    "app/templates/streamlit_demo/.studio/project.json/extra.py",
    "app/templates/streamlit_demo/.studio/defaults.json:secret",
    "app/templates/streamlit_demo/.studio/defaults.json ",
    "app/templates/streamlit_demo/.studio/../project.json",
    "app/templates/streamlit_demo/.STUDIO/defaults.json",
    "app/.studio/defaults.json",
    "app/studio/.gitignore",
    "app/.git/config",
    "app/.env.local",
])
def test_shipped_metadata_exception_does_not_allow_other_hidden_paths(name):
    with pytest.raises(apply.UpdateError):
        apply.vendor_path(name)


@pytest.mark.parametrize("modified", [False, True])
def test_obsolete_files_removed_only_if_unchanged(package, modified):
    candidate = package_files(TARGET)
    del candidate["app/studio/obsolete.py"]
    archive_at(package.archive, candidate)
    old = package.app_root / "studio/obsolete.py"
    if modified:
        old.write_bytes(b"my own work")
    result = prepared(package)
    apply.apply_job(result.job_file)
    assert old.exists() is modified
    assert bool(result.warnings) is modified
    if modified:
        assert old.read_bytes() == b"my own work"


@pytest.mark.parametrize("name", [
    "../outside.txt", "/absolute.txt", "hoyo_project_studio/../outside.txt",
    "hoyo_project_studio/app/../../outside.txt", "hoyo_project_studio/app//file.py",
    "hoyo_project_studio/app\\studio\\evil.py", "hoyo_project_studio/app/C:/file.py",
    "hoyo_project_studio/app/studio/file.py:secret", "hoyo_project_studio/app/studio/file.py ",
    "hoyo_project_studio/app/studio/file.py.", "hoyo_project_studio/app/studio/CON.py",
    "hoyo_project_studio/app/studio/CON .py", "hoyo_project_studio/app/studio/LPT1 .txt",
    "hoyo_project_studio/app/studio/COM¹.py", "hoyo_project_studio/app/studio/NUL",
    "hoyo_project_studio/app/studio/bad?.py", "hoyo_project_studio/app/studio/bad\x01.py",
    "hoyo_project_studio/app/.venv/Scripts/python.exe", "hoyo_project_studio/app/.env",
    "hoyo_project_studio/app/projects/main.py", "hoyo_project_studio/app/logs/setup.log",
    "hoyo_project_studio/app/credentials.json", "hoyo_project_studio/app/cache/data.py",
    "hoyo_project_studio/app/__pycache__/data.py", "hoyo_project_studio/unowned.exe",
    "hoyo_project_studio/app/unknown/module.py", "wrong_project/app/studio/file.py",
])
def test_unsafe_and_user_data_zip_paths_rejected(package, name):
    archive_at(package.archive, package_files(TARGET), extras=[(name, b"untrusted")])
    before = snapshot(package.root)
    with pytest.raises(install.UpdateInstallError):
        prepared(package)
    assert snapshot(package.root) == before
    assert not (package.tmp / "outside.txt").exists()


def test_zip_symlink_rejected(package):
    entry = zipfile.ZipInfo("hoyo_project_studio/app/studio/link.py")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive_at(package.archive, package_files(TARGET), extras=[(entry, b"../../outside.txt")])
    with pytest.raises(install.UpdateInstallError, match="linked"):
        prepared(package)


def test_archive_duplicate_file_rejected(package):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        archive_at(package.archive, package_files(TARGET), extras=[("hoyo_project_studio/app/main.py", b"duplicate")])
    with pytest.raises(install.UpdateInstallError, match="duplicate"):
        prepared(package)


def test_archive_case_collisions_rejected(package):
    candidate = package_files(TARGET)
    candidate["app/studio/Application.py"] = b"case alias"
    archive_at(package.archive, candidate)
    with pytest.raises(install.UpdateInstallError, match="collide"):
        prepared(package)


def test_unlisted_archive_file_rejected(package):
    archive_at(package.archive, package_files(TARGET), extras=[("hoyo_project_studio/app/studio/surprise.py", b"unlisted")])
    with pytest.raises(install.UpdateInstallError, match="listed"):
        prepared(package)


def test_unlisted_archive_directory_rejected(package):
    archive_at(package.archive, package_files(TARGET), extras=[("hoyo_project_studio/app/unused/", b"")])
    with pytest.raises(install.UpdateInstallError, match="unlisted"):
        prepared(package)


def test_archive_size_and_entry_limits(package, monkeypatch):
    monkeypatch.setattr(apply, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(install.UpdateInstallError, match="too large"):
        prepared(package)
    monkeypatch.setattr(apply, "MAX_ARCHIVE_BYTES", 512 * 1024 * 1024)
    monkeypatch.setattr(apply, "MAX_FILES", 5)
    with pytest.raises(apply.UpdateError, match="too many"):
        apply.stage_archive(package.archive, package.tmp / "too-many-files", TARGET)


def test_dangerous_compression_ratio_rejected(package):
    candidate = package_files(TARGET)
    candidate["app/resources/bomb.txt"] = b"0" * (2 * 1024 * 1024)
    archive_at(package.archive, candidate)
    with pytest.raises(install.UpdateInstallError, match="unsafe expanded"):
        prepared(package)


def test_package_code_version_is_checked_without_executing_it(package):
    marker = package.tmp / "executed.txt"
    candidate = package_files(TARGET)
    candidate["app/studio/__init__.py"] = f'__version__ = __import__("pathlib").Path({str(marker)!r}).write_text("executed")\n'.encode()
    archive_at(package.archive, candidate)
    with pytest.raises(install.UpdateInstallError, match="version metadata"):
        prepared(package)
    assert not marker.exists()


def test_package_project_version_disagrees(package):
    candidate = package_files(TARGET)
    candidate["app/pyproject.toml"] = candidate["app/pyproject.toml"].replace(TARGET.encode(), b"9.9.9")
    archive_at(package.archive, candidate)
    with pytest.raises(install.UpdateInstallError, match="do not agree"):
        prepared(package)


def test_prepared_file_and_manifest_cannot_be_changed_together(package):
    result = prepared(package)
    file = result.job_directory / "staged/app/main.py"
    file.write_bytes(b"changed after review")
    manifest_path = result.job_directory / "staged" / apply.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["app/main.py"] = digest(file.read_bytes())
    manifest_path.write_text(json.dumps(manifest))
    before = snapshot(package.root)
    with pytest.raises(apply.UpdateError, match="ownership metadata"):
        apply.apply_job(result.job_file)
    assert snapshot(package.root) == before


def test_prepared_extra_file_and_hash_failure_rejected(package):
    result = prepared(package)
    staged = result.job_directory / "staged"
    (staged / "app/studio/surprise.py").write_bytes(b"surprise")
    with pytest.raises(apply.UpdateError, match="unlisted"):
        apply.apply_job(result.job_file)
    (staged / "app/studio/surprise.py").unlink()
    (staged / "app/main.py").write_bytes(b"bad checksum")
    with pytest.raises(apply.UpdateError, match="verification"):
        apply.apply_job(result.job_file)


def test_symlink_and_hardlink_destinations_never_modified(package):
    path = package.app_root / "studio/application.py"
    external = package.tmp / "external.py"
    external.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(install.UpdateInstallError, match="linked"):
        prepared(package)
    path.unlink()
    os.link(external, path)
    with pytest.raises(install.UpdateInstallError, match="ordinary"):
        prepared(package)
    assert external.read_bytes() == package.files["app/studio/application.py"]


def test_case_alias_destination_is_preserved(package):
    (package.app_root / "studio/application.py").rename(package.app_root / "studio/Application.py")
    with pytest.raises(install.UpdateInstallError, match="capitalized"):
        prepared(package)


def test_cache_inside_install_rejected_without_creating_folder(package):
    location = package.app_root / "update-cache"
    with pytest.raises(install.UpdateInstallError, match="outside"):
        prepared(package, cache_root=location)
    assert not location.exists()


def test_cancelled_preparation_removes_only_its_own_temporary_job(package):
    count = 0
    def cancelled():
        nonlocal count
        count += 1
        return count > 5
    before = snapshot(package.root)
    with pytest.raises(install.UpdateInstallError) as caught:
        prepared(package, cancelled=cancelled)
    assert caught.value.cancelled
    assert package.archive.is_file()
    assert snapshot(package.root) == before
    assert not list(install.update_cache_root().glob("hoyo-update-*"))


def test_handled_mid_install_failure_rolls_back_every_written_file(package, monkeypatch):
    result = prepared(package)
    before = snapshot(package.root)
    real_copy = apply.atomic_copy
    applied = []
    def interrupted_copy(source, destination, expected):
        if source.is_relative_to(result.job_directory / "staged"):
            applied.append(destination)
            if len(applied) == 3:
                raise OSError("Simulated Windows file access failure")
        return real_copy(source, destination, expected)
    monkeypatch.setattr(apply, "atomic_copy", interrupted_copy)
    with pytest.raises(apply.UpdateError, match="previous version was restored"):
        apply.apply_job(result.job_file)
    assert len(applied) == 3
    assert snapshot(package.root) == before
    assert json.loads((result.job_directory / "journal.json").read_text())["status"] == "rolled_back"
    assert not (package.app_root / apply.PENDING_NAME).exists()
    assert not list(package.root.rglob(".hoyo-update-*.tmp"))


class PowerLoss(BaseException):
    pass


def simulate_power_loss(package, monkeypatch):
    result = prepared(package)
    real_copy = apply.atomic_copy
    count = 0
    def crash_after_copy(source, destination, expected):
        nonlocal count
        real_copy(source, destination, expected)
        if source.is_relative_to(result.job_directory / "staged"):
            count += 1
            if count == 2:
                raise PowerLoss()
    with monkeypatch.context() as patch:
        patch.setattr(apply, "atomic_copy", crash_after_copy)
        with pytest.raises(PowerLoss):
            apply.apply_job(result.job_file)
    assert (package.app_root / apply.PENDING_NAME).exists()
    return result


def test_power_loss_recovers_pending_file_even_before_done_journal_record(package, monkeypatch):
    before = snapshot(package.root)
    result = simulate_power_loss(package, monkeypatch)
    assert snapshot(package.root) != before
    assert apply.recover_job(result.job_file) == 0
    assert snapshot(package.root) == before
    assert not (package.app_root / apply.PENDING_NAME).exists()


def test_startup_runs_original_helper_as_real_recovery_subprocess(package, monkeypatch):
    before = snapshot(package.root)
    result = simulate_power_loss(package, monkeypatch)
    log = startup.EventLog(package.tmp / "recovery.log", "core", stream=io.StringIO())
    try:
        startup._recover_interrupted_update(package.app_root, startup.child_environment(), log, startup.Cancellation())
    finally:
        log.close()
    assert snapshot(package.root) == before
    assert not (package.app_root / apply.PENDING_NAME).exists()
    assert json.loads((result.job_directory / "journal.json").read_text())["status"] == "rolled_back"


def test_recovery_will_not_overwrite_user_changes_after_crash(package, monkeypatch):
    result = simulate_power_loss(package, monkeypatch)
    journal = json.loads((result.job_directory / "journal.json").read_text())
    changed = next(operation["path"] for operation in journal["operations"] if operation.get("state") == "done")
    (package.root / changed).write_bytes(b"work saved after interruption")
    assert apply.recover_job(result.job_file) == 1
    assert (package.root / changed).read_bytes() == b"work saved after interruption"
    assert (package.app_root / apply.PENDING_NAME).exists()
    assert (result.job_directory / "backup" / changed).is_file()


def test_recovery_journal_cannot_target_unowned_data(package, monkeypatch):
    result = simulate_power_loss(package, monkeypatch)
    journal_path = result.job_directory / "journal.json"
    journal = json.loads(journal_path.read_text())
    journal["operations"].append({"path": "app/notes.txt", "old_hash": digest(b"user notes"), "new_hash": None, "state": "done"})
    journal_path.write_text(json.dumps(journal))
    assert apply.recover_job(result.job_file) == 1
    assert (package.app_root / "notes.txt").read_bytes() == b"user notes"


def test_recovery_missing_journal_fails_without_clearing_pointer(package, monkeypatch):
    result = simulate_power_loss(package, monkeypatch)
    (result.job_directory / "journal.json").unlink()
    interrupted = snapshot(package.root)
    assert apply.recover_job(result.job_file) == 1
    assert snapshot(package.root) == interrupted
    assert (package.app_root / apply.PENDING_NAME).exists()


def test_recovery_missing_required_operation_fails_closed(package, monkeypatch):
    result = simulate_power_loss(package, monkeypatch)
    path = result.job_directory / "journal.json"
    journal = json.loads(path.read_bytes())
    journal["operations"] = [operation for operation in journal["operations"] if operation["path"] != apply.MANIFEST_NAME]
    path.write_text(json.dumps(journal))
    interrupted = snapshot(package.root)
    assert apply.recover_job(result.job_file) == 1
    assert snapshot(package.root) == interrupted
    assert (package.app_root / apply.PENDING_NAME).exists()


@pytest.mark.parametrize("changed", [False, True])
def test_completed_update_recovery_verifies_files_before_clearing_record(package, changed):
    result = prepared(package)
    apply.apply_job(result.job_file)
    job, directory, root = apply.load_job(result.job_file)
    apply._set_pending(root, directory, job)
    if changed:
        (package.app_root / "main.py").write_bytes(b"incomplete storage write")
    assert apply.recover_job(result.job_file) == int(changed)
    assert (package.app_root / apply.PENDING_NAME).exists() is changed
    assert json.loads((package.root / apply.MANIFEST_NAME).read_bytes())["version"] == TARGET


def test_pending_update_cannot_follow_a_broken_install_lock_link(package):
    result = prepared(package)
    outside = package.tmp / "outside-lock"
    (package.app_root / apply.LOCK_NAME).symlink_to(outside)
    with pytest.raises(apply.UpdateError, match="linked"):
        apply.apply_job(result.job_file)
    assert not outside.exists()


def test_manual_archive_verification_works_without_windows_or_installation(package, monkeypatch):
    monkeypatch.setattr(install, "_is_windows", lambda: False)
    before = snapshot(package.root)
    install.validate_download_archive(package.archive, TARGET)
    assert snapshot(package.root) == before


def test_manual_archive_refuses_inconsistent_version(package):
    with pytest.raises(install.UpdateInstallError, match="version does not match"):
        install.validate_download_archive(package.archive, "0.4.15")


def test_manual_archive_refuses_a_file_checksum_mismatch(package):
    candidate = package_files(TARGET)
    manifest = manifest_bytes(candidate, TARGET)
    candidate["app/main.py"] = b"modified after manifest creation"
    with zipfile.ZipFile(package.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in candidate.items():
            archive.writestr("hoyo_project_studio/" + name, contents)
        archive.writestr("hoyo_project_studio/" + apply.MANIFEST_NAME, manifest)
    with pytest.raises(install.UpdateInstallError, match="checksum verification"):
        install.validate_download_archive(package.archive, TARGET)


def test_startup_rejects_modified_or_external_recovery_helper(package, monkeypatch):
    result = simulate_power_loss(package, monkeypatch)
    helper = result.job_directory / "update_apply.py"
    helper.write_bytes(b"print('untrusted helper')")
    log = startup.EventLog(package.tmp / "recovery.log", "core", stream=io.StringIO())
    try:
        with pytest.raises(startup.StartupFailure, match="verification"):
            startup._recover_interrupted_update(package.app_root, startup.child_environment(), log, startup.Cancellation())
        pointer_path = package.app_root / apply.PENDING_NAME
        pointer = json.loads(pointer_path.read_text())
        pointer["job_file"] = str(package.tmp / "evil/job.json")
        pointer_path.write_text(json.dumps(pointer))
        with pytest.raises(startup.StartupFailure, match="outside"):
            startup._recover_interrupted_update(package.app_root, startup.child_environment(), log, startup.Cancellation())
    finally:
        log.close()


def test_helper_waits_for_parent_exit_before_application_mutation(package, monkeypatch):
    result = prepared(package)
    before = snapshot(package.root)
    events = []
    class Parent:
        def wait(self, cancellation):
            assert snapshot(package.root) == before
            assert (result.job_directory / "ready.json").exists()
            events.append("parent-exited")
        def close(self):
            events.append("handle-closed")
    def reopen(root):
        assert json.loads((root / apply.MANIFEST_NAME).read_text())["version"] == TARGET
        events.append("reopened")
    monkeypatch.setattr(apply, "relaunch", reopen)
    assert apply.run_helper(result.job_file, parent_factory=lambda pid: Parent()) == 0
    assert events == ["parent-exited", "handle-closed", "reopened"]


def test_helper_timeout_leaves_live_application_untouched(package, monkeypatch):
    result = prepared(package)
    before = snapshot(package.root)
    messages = []
    class Parent:
        def wait(self, cancellation):
            raise apply.UpdateError("Studio is still running. No files were changed.")
        def close(self):
            pass
    monkeypatch.setattr(apply, "show_error", messages.append)
    monkeypatch.setattr(apply, "relaunch", lambda *_: pytest.fail("Live app must not be relaunched"))
    assert apply.run_helper(result.job_file, parent_factory=lambda pid: Parent()) == 1
    assert snapshot(package.root) == before
    assert "still running" in messages[0]
    assert not (package.app_root / apply.PENDING_NAME).exists()


def test_installed_files_revalidated_after_parent_exit(package, monkeypatch):
    result = prepared(package)
    class Parent:
        def wait(self, cancellation):
            (package.app_root / "main.py").write_bytes(b"changed while waiting")
        def close(self):
            pass
    monkeypatch.setattr(apply, "show_error", lambda *_: None)
    monkeypatch.setattr(apply, "relaunch", lambda *_: None)
    assert apply.run_helper(result.job_file, parent_factory=lambda pid: Parent()) == 1
    assert (package.app_root / "main.py").read_bytes() == b"changed while waiting"
    assert json.loads((package.root / apply.MANIFEST_NAME).read_text())["version"] == CURRENT


@pytest.mark.skipif(os.name == "nt", reason="Real POSIX test substitutes the Windows LockFileEx boundary")
@pytest.mark.parametrize("guard", ["running", "setup"])
def test_actual_other_process_blocks_update_for_running_instance_or_setup(package, guard):
    result = prepared(package)
    lock = package.app_root / apply.LOCK_NAME
    script = (
        "import fcntl,sys; stream=open(sys.argv[1],'a+b'); "
        + ("fcntl.lockf(stream.fileno(),fcntl.LOCK_SH,1,1); " if guard == "running" else "fcntl.flock(stream.fileno(),fcntl.LOCK_EX); ")
        + "print('ready',flush=True); sys.stdin.read()"
    )
    child = subprocess.Popen([sys.executable, "-I", "-c", script, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    before = snapshot(package.root)
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(apply.UpdateError, match="Studio|setup"):
            apply.apply_job(result.job_file)
        assert snapshot(package.root) == before
    finally:
        child.communicate(timeout=5)
    apply.apply_job(result.job_file)


def test_launcher_handshake_is_bound_to_parent_and_helper_process(package, monkeypatch):
    result = prepared(package)
    arguments = []
    class Child:
        pid = 98765
        def poll(self):
            return None
    def start(command, **options):
        arguments.append((command, options))
        apply.write_json(result.job_directory / "ready.json", {"ready": True, "parent_pid": os.getpid(), "helper_pid": Child.pid})
        return Child()
    monkeypatch.setattr(install.subprocess, "Popen", start)
    install.launch_prepared_update(result)
    command, options = arguments[0]
    assert command[1:4] == ["-I", "-X", "utf8"]
    assert command[4] == str(result.job_directory / "update_apply.py")
    assert command[-2:] == ["--job", str(result.job_file)]
    assert options["close_fds"] and options["cwd"] == str(result.job_directory)


def test_platform_frozen_and_flat_layout_explain_manual_install(package, monkeypatch):
    monkeypatch.setattr(install, "_is_windows", lambda: False)
    assert not install.installation_support(package.app_root)[0]
    monkeypatch.setattr(install, "_is_windows", lambda: True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert "separately built" in install.installation_support(package.app_root)[1]
    monkeypatch.delattr(sys, "frozen")
    assert "development" in install.installation_support(package.root)[1]


def test_windows_reparse_metadata_is_rejected():
    assert apply._is_link(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))


def test_updated_python_module_invalidates_same_second_same_size_bytecode(tmp_path, monkeypatch):
    module = tmp_path / "sample.py"
    old = b'VERSION = "old"\n'
    new = b'VERSION = "new"\n'
    timestamp = int(time.time())
    module.write_bytes(old)
    os.utime(module, (timestamp, timestamp))
    py_compile.compile(str(module), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    command = [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import sample; print(sample.VERSION)", str(tmp_path)]
    module.write_bytes(new)
    os.utime(module, (timestamp, timestamp))
    # Prove this fixture really would load old bytecode without the safeguard.
    assert subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip() == "old"
    module.write_bytes(old)
    os.utime(module, (timestamp, timestamp))
    candidate = tmp_path / "candidate.py"
    candidate.write_bytes(new)
    real_mode = apply.shutil.copymode
    def same_second_copy(source, temporary, **options):
        real_mode(source, temporary, **options)
        os.utime(temporary, (timestamp, timestamp))
    monkeypatch.setattr(apply.shutil, "copymode", same_second_copy)

    apply.atomic_copy(candidate, module, digest(new))

    assert int(module.stat().st_mtime) != timestamp
    assert subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip() == "new"

