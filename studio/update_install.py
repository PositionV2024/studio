"""Prepare compact Windows updates without touching the running application."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from typing import Callable

from launcher import update_apply

from . import __version__


class UpdateInstallError(RuntimeError):
    def __init__(self, message: str, *, cancelled: bool = False) -> None:
        super().__init__(message)
        self.cancelled = cancelled


class UpdateInstallCancelled(UpdateInstallError):
    def __init__(self) -> None:
        super().__init__("Update preparation was cancelled.", cancelled=True)


@dataclass(frozen=True)
class PreparedUpdate:
    job_directory: Path
    job_file: Path
    version: str
    repository: str
    archive: Path
    warnings: tuple[str, ...] = ()


class RunningInstance:
    def __init__(self, lock: update_apply.FileRangeLock) -> None:
        self._lock = lock

    def close(self) -> None:
        self._lock.close()


def _is_windows() -> bool:
    return sys.platform == "win32"


def _compact_root(app_root: Path) -> Path:
    content = update_apply.safe_root(Path(app_root))
    if content.name != "app" or not (content.parent / update_apply.EXE_NAME).is_file():
        raise UpdateInstallError("This is a development or older layout. Extract the complete update into a new folder and open HoYo Project Studio.exe.")
    update_apply.safe_destination(content.parent, update_apply.EXE_NAME)
    return content.parent


def installation_support(app_root: Path) -> tuple[bool, str]:
    if not _is_windows() or struct.calcsize("P") != 8:
        return False, "Automatic installation is available in the Windows x64 desktop application. Download the complete ZIP for a manual installation."
    if getattr(sys, "frozen", False):
        return False, "This is a separately built executable. Download the complete ZIP and extract it into a new folder to use the updated launcher."
    try:
        root = _compact_root(app_root)
        manifest_path = update_apply.safe_destination(root, update_apply.MANIFEST_NAME)
        if not manifest_path.is_file():
            return False, "This older installation has no package ownership manifest. Extract the complete update into a new folder and open its launcher."
        update_apply.validate_manifest(update_apply.read_json(manifest_path), __version__)
        if (root / "app" / update_apply.PENDING_NAME).exists():
            return False, "An earlier update needs to finish or recover. Close Studio and open HoYo Project Studio.exe before trying another update."
    except (UpdateInstallError, update_apply.UpdateError, OSError) as error:
        return False, str(error)
    return True, "Studio can install this update after saving your work and restarting. Your projects and existing Python environment are kept."


def register_running_instance(app_root: Path) -> RunningInstance | None:
    """Hold through app.exec(); the installer cannot change a live instance."""
    if not _is_windows() or getattr(sys, "frozen", False):
        return None
    content = Path(app_root)
    if content.name != "app" or not (content.parent / update_apply.EXE_NAME).is_file():
        return None
    try:
        lock = update_apply.acquire_running_instance(content)
        return RunningInstance(lock) if lock is not None else None
    except (update_apply.UpdateError, OSError) as error:
        raise UpdateInstallError(str(error)) from error


def update_cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        root = Path(base) / "HoYo Project Studio" / "Updates"
    else:
        root = Path.home() / "AppData" / "Local" / "HoYo Project Studio" / "Updates"
    if not root.is_absolute():
        raise UpdateInstallError("A safe per-user update folder is not available.")
    return root


def validate_download_archive(archive: Path, expected_version: str, *, cancelled: Callable[[], bool] | None = None, progress: Callable[[str], None] | None = None) -> None:
    """Check manual-install downloads too, independent of OS/current layout."""
    try:
        update_apply.check_cancelled(cancelled)
        with tempfile.TemporaryDirectory(prefix="hoyo-verify-update-") as temporary:
            update_apply.stage_archive(Path(archive), Path(temporary) / "staged", expected_version, cancelled=cancelled, progress=progress)
        update_apply.check_cancelled(cancelled)
    except update_apply.UpdateError as error:
        raise UpdateInstallError(str(error), cancelled=error.cancelled) from error
    except OSError as error:
        raise UpdateInstallError("The downloaded update could not be verified: " + str(error)) from error


def prepare_install(archive: Path, app_root: Path, expected_version: str, repository: str, *, cancelled: Callable[[], bool] | None = None, progress: Callable[[str], None] | None = None, cache_root: Path | None = None) -> PreparedUpdate:
    supported, reason = installation_support(app_root)
    if not supported:
        raise UpdateInstallError(reason)
    directory: Path | None = None
    try:
        update_apply.check_cancelled(cancelled)
        root = _compact_root(Path(app_root))
        cache = update_cache_root() if cache_root is None else Path(cache_root)
        cache = Path(os.path.abspath(cache))
        if root == cache or root in cache.parents or cache in root.parents:
            raise UpdateInstallError("The update cache must be outside the application folder.")
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache = update_apply.safe_root(cache)
        directory = Path(tempfile.mkdtemp(prefix="hoyo-update-", dir=cache))
        if progress is not None:
            progress("Preparing a verified update; Studio's files have not changed…")
        update_apply.stage_archive(Path(archive), directory / "staged", expected_version, cancelled=cancelled, progress=progress)
        _, warnings, old_hash = update_apply.build_plan(root, directory / "staged", __version__, expected_version, cancelled=cancelled)
        helper = Path(update_apply.__file__).resolve()
        installed_helper = update_apply.safe_destination(root, "app/launcher/update_apply.py")
        # This is the already installed helper, never the candidate's helper.
        if helper != installed_helper.resolve() and file_hash_same(helper, installed_helper) is False:
            raise UpdateInstallError("The update helper does not match the running installation.")
        shutil.copyfile(helper, directory / "update_apply.py")
        shutil.copyfile(root / update_apply.MANIFEST_NAME, directory / "original-manifest.json")
        job = {
            "schema_version": 1, "application": update_apply.APPLICATION,
            "install_root": str(root), "current_version": __version__, "target_version": expected_version,
            "repository": repository, "parent_pid": os.getpid(), "created_at": time.time(),
            "old_manifest_sha256": old_hash,
            "new_manifest_sha256": update_apply.file_hash(directory / "staged" / update_apply.MANIFEST_NAME),
            "helper_sha256": update_apply.file_hash(directory / "update_apply.py"),
        }
        update_apply.write_json(directory / "job.json", job)
        update_apply.load_job(directory / "job.json")
        update_apply.check_cancelled(cancelled)
        if progress is not None:
            progress("Update verified. Ready to save your work and restart.")
        return PreparedUpdate(directory, directory / "job.json", expected_version, repository, Path(archive), warnings)
    except Exception as error:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        if isinstance(error, UpdateInstallError):
            raise
        if isinstance(error, update_apply.UpdateError):
            raise UpdateInstallError(str(error), cancelled=error.cancelled) from error
        raise UpdateInstallError("The update could not be prepared: " + str(error)) from error


def file_hash_same(first: Path, second: Path) -> bool:
    return update_apply.file_hash(first) == update_apply.file_hash(second)


def launch_prepared_update(prepared: PreparedUpdate) -> None:
    if not _is_windows() or getattr(sys, "frozen", False):
        raise UpdateInstallError("Restart-to-update is available only in the compact Windows launcher installation.")
    process: subprocess.Popen | None = None
    try:
        job, directory, _ = update_apply.load_job(prepared.job_file)
        if directory != prepared.job_directory or job["target_version"] != prepared.version or job["repository"] != prepared.repository or job["parent_pid"] != os.getpid():
            raise UpdateInstallError("The prepared update no longer belongs to this Studio session.")
        if (directory / "ready.json").exists() or (directory / "launch.json").exists() or (directory / "cancel").exists():
            raise UpdateInstallError("This update restart was already attempted. Check for updates again to prepare a fresh restart.")
        update_apply.write_json(directory / "launch.json", {"parent_pid": os.getpid(), "started_at": time.time()})
        executable = Path(sys.executable)
        if executable.name.lower() == "python.exe" and executable.with_name("pythonw.exe").is_file():
            executable = executable.with_name("pythonw.exe")
        with (directory / "update.log").open("ab") as output:
            process = subprocess.Popen(
                [str(executable), "-I", "-X", "utf8", str(directory / "update_apply.py"), "--job", str(prepared.job_file)],
                cwd=str(directory), stdin=subprocess.DEVNULL, stdout=output, stderr=output, close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            ready_path = directory / "ready.json"
            if ready_path.exists():
                ready = update_apply.read_json(ready_path)
                if ready.get("ready") is True and ready.get("parent_pid") == os.getpid() and ready.get("helper_pid") == process.pid:
                    return
                raise UpdateInstallError("The update helper did not confirm a safe restart.")
            if process.poll() is not None:
                result_path = directory / "result.json"
                message = update_apply.read_json(result_path).get("message") if result_path.exists() else None
                raise UpdateInstallError(message or "The update helper could not start. Studio is still open and its files have not changed.")
            time.sleep(0.04)
        raise UpdateInstallError("The update helper did not become ready. Studio is still open; prepare the update again.")
    except Exception as error:
        # A late helper must not install if the caller keeps Studio open.
        try:
            (prepared.job_directory / "cancel").touch(exist_ok=True)
        except OSError:
            pass
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if isinstance(error, UpdateInstallError):
            raise
        raise UpdateInstallError(str(error)) from error
