"""Prepare Studio in stages using an already installed Windows Python.

The core phase prepares the window, code completion and syntax highlighting.
The background phase prepares Preview and network features while Studio is
open. Every stdout line is a JSON event for the setup window or the application.
Neither phase downloads or installs a Python interpreter.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterator, TextIO


CORE_REQUIREMENTS = ("PySide6>=6.8,<7", "jedi>=0.19.2,<0.21", "Pygments>=2.18,<3")
LOG_NAME = "setup_hoyo_studio.log"
LOCK_NAME = ".hoyo_install.lock"
READY_NAME = ".hoyo_studio_ready"
MIN_PYTHON = (3, 11)

_RUNTIME_PROBE = """
import json, platform, struct, sys
print(json.dumps({
    "executable": sys.executable, "prefix": sys.prefix, "base_prefix": sys.base_prefix,
    "implementation": platform.python_implementation(), "version": list(sys.version_info[:3]),
    "bits": struct.calcsize("P") * 8, "platform": sys.platform,
}))
"""

_PACKAGE_PROBE = """
import importlib.metadata as metadata
import importlib.util
import json
import sys
try:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import InvalidVersion, Version
    from pip._vendor.packaging.utils import canonicalize_name
except ImportError:
    try:
        from packaging.requirements import Requirement
        from packaging.version import InvalidVersion, Version
        from packaging.utils import canonicalize_name
    except ImportError:
        print(json.dumps({"needs_parser": True, "missing": [], "installed": {}, "pins": []}))
        raise SystemExit(0)

missing = []
installed = {}
core_missing = []
for text in json.loads(sys.argv[1]):
    requirement = Requirement(text)
    if requirement.url is not None or requirement.extras:
        raise SystemExit("Studio setup requires plain package names and version ranges.")
    if requirement.marker is not None and not requirement.marker.evaluate():
        continue
    name = canonicalize_name(requirement.name)
    try:
        version = metadata.version(requirement.name)
        compatible = Version(version) in requirement.specifier
    except (metadata.PackageNotFoundError, InvalidVersion):
        version = None
        compatible = False
    installed[name] = version
    if not compatible:
        missing.append(text)
        if name in ("pyside6", "jedi", "pygments"):
            core_missing.append(text)

# Exact constraints let the parent protect every existing package except the
# Streamlit/Requests feature entry points, which are not imported by Studio.
# A shared dependency conflict is reported instead of an in-process upgrade.
pins = {}
for distribution in metadata.distributions():
    name = distribution.metadata.get("Name")
    if not name:
        continue
    try:
        parsed = Requirement(name + "==" + distribution.version)
        Version(distribution.version)
    except (ValueError, InvalidVersion):
        raise SystemExit("An installed package has invalid version metadata: " + name)
    normalized = canonicalize_name(parsed.name)
    pin = str(parsed)
    if normalized in pins and pins[normalized] != pin:
        raise SystemExit("Multiple installed versions were found for " + name + ". Close Studio and repair its environment.")
    pins[normalized] = pin
print(json.dumps({
    "needs_parser": False,
    "pip_available": importlib.util.find_spec("pip") is not None,
    "missing": missing,
    "core_missing": core_missing,
    "installed": installed,
    "pins": sorted(pins.values()),
}))
"""

_IMPORT_PROBE = """
import json
import sys
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
import jedi
from pygments import lex
from pygments.lexers import PythonLexer
if sys.argv[1] == "background":
    import requests
    import streamlit
print(json.dumps({"ready": True, "phase": sys.argv[1]}))
"""


class StartupFailure(RuntimeError):
    """A setup error that can be displayed without exposing a traceback."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code if exit_code > 0 else 1


class SetupCancelled(StartupFailure):
    def __init__(self) -> None:
        super().__init__("Setup cancelled. You can open Studio again to resume preparing its features.", 130)


class EventLog:
    """Keep JSONL stdout separate from the readable, persistent setup log."""

    def __init__(self, path: Path, phase: str, stream: TextIO | None = None) -> None:
        self.path = path
        self.phase = phase
        self.stream = sys.stdout if stream is None else stream
        self.file = path.open("a", encoding="utf-8", newline="")

    def emit(self, event: str, message: str, **details: Any) -> None:
        self.file.write(f"[{self.phase}] {event}: {message}\n")
        self.file.flush()
        value = {"event": event, "phase": self.phase, "message": message, **details}
        # ASCII JSON is also safe in an older Windows command prompt. JSON
        # readers restore the original Unicode strings and paths.
        self.stream.write(json.dumps(value, ensure_ascii=True) + "\n")
        self.stream.flush()

    def close(self) -> None:
        self.file.close()


class Cancellation:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.requested = threading.Event()

    def check(self) -> None:
        if self.requested.is_set() or (self.path is not None and self.path.exists()):
            raise SetupCancelled()


@contextmanager
def installation_lock(root: Path) -> Iterator[None]:
    """Serialize setup and EXE builds without deleting a live lock file.

    The file remains on disk; the operating system releases ownership when a
    worker exits, including an unexpected exit. Removing this file on unlock
    would let two later processes lock different file handles concurrently.
    """

    lock = (Path(root) / LOCK_NAME).open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise StartupFailure(
                    "Another Studio setup or EXE build is already running for this folder. "
                    "Wait for it to finish, then try again."
                ) from error
        else:
            import fcntl

            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StartupFailure(
                    "Another Studio setup or EXE build is already running for this folder. "
                    "Wait for it to finish, then try again."
                ) from error
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _is_windows() -> bool:
    return os.name == "nt"


def _current_runtime_info() -> dict[str, Any]:
    return {
        "executable": sys.executable, "prefix": sys.prefix, "base_prefix": sys.base_prefix,
        "implementation": platform.python_implementation(), "version": list(sys.version_info[:3]),
        "bits": struct.calcsize("P") * 8, "platform": sys.platform,
    }


def child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    cleared = {
        "python_manager_automatic_install", "pylauncher_allow_install", "pylauncher_always_install",
        "pythonhome", "pythonpath", "pip_target", "pip_prefix", "pip_user",
        "pip_ignore_installed", "pip_force_reinstall", "pip_upgrade", "pip_upgrade_strategy", "pip_dry_run",
    }
    for name in list(environment):
        if name.casefold() in cleared:
            del environment[name]
    environment.update({
        "PYTHON_MANAGER_AUTOMATIC_INSTALL": "false",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
        "PIP_REQUIRE_VIRTUALENV": "true", "PIP_CONFIG_FILE": os.devnull,
    })
    return environment


def _validate_runtime(info: dict[str, Any], *, environment_root: Path | None = None) -> None:
    try:
        valid = (
            info["platform"] == "win32" and info["implementation"] == "CPython"
            and tuple(int(part) for part in info["version"][:2]) >= MIN_PYTHON
            and int(info["bits"]) == 64 and bool(info["executable"])
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise StartupFailure(
            "Studio needs an installed 64-bit CPython 3.11 or newer for Windows. "
            "No Python interpreter has been downloaded or installed."
        )
    if environment_root is not None:
        try:
            prefix = Path(info["prefix"]).resolve()
            base_prefix = Path(info["base_prefix"]).resolve()
            valid_environment = prefix == environment_root.resolve() and prefix != base_prefix
        except (KeyError, TypeError, ValueError, OSError):
            valid_environment = False
        if not valid_environment:
            raise StartupFailure(
                "The .venv Python does not belong to this application's environment. "
                "The existing environment has been left unchanged. Restore it or move it aside before trying again."
            )


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop only a subprocess owned by this worker and that process's children."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=2)


def _run_child(
    arguments: list[str], root: Path, environment: dict[str, str], log: EventLog,
    cancellation: Cancellation, *, description: str, capture: bool = False, timeout: float | None = None,
) -> str:
    cancellation.check()
    log.emit("status", description)
    process: subprocess.Popen[str] | None = None
    readers: list[threading.Thread] = []
    output: list[str] = []
    messages: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def read_pipe(name: str, pipe: TextIO) -> None:
        try:
            for line in pipe:
                messages.put((name, line))
        except (OSError, ValueError):
            pass
        finally:
            messages.put((name, None))

    try:
        platform_options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        process = subprocess.Popen(
            arguments, cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **platform_options,
        )
        assert process.stdout is not None and process.stderr is not None
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            reader = threading.Thread(target=read_pipe, args=(name, pipe), daemon=True)
            reader.start()
            readers.append(reader)
        started = time.monotonic()
        finished_streams = 0
        while finished_streams < 2 or process.poll() is None:
            cancellation.check()
            if timeout is not None and time.monotonic() - started > timeout:
                raise StartupFailure(f"{description} timed out. Review {LOG_NAME} and try again.")
            try:
                name, line = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                finished_streams += 1
            elif capture and name == "stdout":
                output.append(line)
            else:
                log.emit("log", line.rstrip("\r\n"))
        status = process.wait()
        if status != 0:
            for line in output:
                log.emit("log", line.rstrip("\r\n"))
            raise StartupFailure(f"{description} failed (exit code {status}). See {LOG_NAME} for details.", status)
        cancellation.check()
        return "".join(output)
    except BaseException:
        if process is not None:
            _stop_process_tree(process)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=0.5)
        if process is not None and all(not reader.is_alive() for reader in readers):
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _capture_json(
    arguments: list[str], root: Path, environment: dict[str, str], log: EventLog,
    cancellation: Cancellation, *, description: str, timeout: float = 60,
) -> dict[str, Any]:
    output = _run_child(
        arguments, root, environment, log, cancellation,
        description=description, capture=True, timeout=timeout,
    )
    try:
        result = json.loads(output.strip())
    except (TypeError, ValueError) as error:
        raise StartupFailure(f"{description} returned an invalid result.") from error
    if not isinstance(result, dict):
        raise StartupFailure(f"{description} returned an invalid result.")
    return result


def _choose_environment(root: Path, environment: dict[str, str], log: EventLog, cancellation: Cancellation) -> Path:
    current = _current_runtime_info()
    _validate_runtime(current)
    base_python = Path(current["executable"]).resolve()
    environment_root = root / ".venv"
    executable = environment_root / "Scripts" / "python.exe"
    if environment_root.exists() or environment_root.is_symlink():
        if not executable.is_file():
            raise StartupFailure(
                "The existing .venv is incomplete: .venv\\Scripts\\python.exe is missing. "
                "It has been left unchanged. Restore it or move it aside before trying again."
            )
        log.emit("status", "Reusing Studio's existing Python environment.")
    else:
        _run_child(
            [str(base_python), "-I", "-X", "utf8", "-m", "venv", str(environment_root)],
            root, environment, log, cancellation,
            description="Creating a local environment from installed Python; no Python download is needed.",
        )
        if not executable.is_file():
            raise StartupFailure("Python did not create .venv\\Scripts\\python.exe. Review the setup log.")
    try:
        info = _capture_json(
            [str(executable), "-I", "-X", "utf8", "-c", _RUNTIME_PROBE],
            root, environment, log, cancellation, description="Checking Studio's Python environment",
        )
        _validate_runtime(info, environment_root=environment_root)
    except SetupCancelled:
        raise
    except StartupFailure as error:
        raise StartupFailure(
            f"{error} The .venv has not been deleted or replaced. Restore it or move it aside before trying again.",
            error.exit_code,
        ) from error
    log.emit("log", f"Installed Python: {base_python}; Studio Python: {executable}")
    return executable


def _read_requirements(root: Path) -> list[str]:
    requirements = []
    for raw in (root / "requirements.txt").read_text(encoding="utf-8-sig").splitlines():
        value = raw.split(" #", 1)[0].strip()
        if value and not value.startswith("#"):
            requirements.append(value)
    return requirements


def _package_status(
    executable: Path, requirements: list[str], root: Path, environment: dict[str, str],
    log: EventLog, cancellation: Cancellation,
) -> dict[str, Any]:
    result = _capture_json(
        [str(executable), "-I", "-X", "utf8", "-c", _PACKAGE_PROBE, json.dumps(requirements)],
        root, environment, log, cancellation, description="Checking installed feature packages",
    )
    for name in ("missing", "pins"):
        if not isinstance(result.get(name), list) or any(not isinstance(value, str) for value in result[name]):
            raise StartupFailure("The installed package check returned an invalid result.")
    return result


def _check_imports(
    executable: Path, phase: str, root: Path, environment: dict[str, str], log: EventLog, cancellation: Cancellation,
) -> None:
    result = _capture_json(
        [str(executable), "-I", "-X", "utf8", "-c", _IMPORT_PROBE, phase],
        root, environment, log, cancellation, description="Verifying that Studio's feature packages can load",
        timeout=90,
    )
    if result.get("ready") is not True or result.get("phase") != phase:
        raise StartupFailure("Studio's feature import check did not confirm readiness.")


def _prepare_packages(
    executable: Path, phase: str, root: Path, environment: dict[str, str], log: EventLog, cancellation: Cancellation,
) -> None:
    requirements = list(CORE_REQUIREMENTS) if phase == "core" else _read_requirements(root)
    # The background installer always checks the core requirements as well,
    # even if requirements.txt has been edited, before touching the live env.
    if phase == "background":
        requirements.extend(requirement for requirement in CORE_REQUIREMENTS if requirement not in requirements)
    status = _package_status(executable, requirements, root, environment, log, cancellation)
    if status.get("needs_parser") or (status["missing"] and not status.get("pip_available")):
        _run_child(
            [str(executable), "-I", "-X", "utf8", "-m", "ensurepip"],
            root, environment, log, cancellation,
            description="Preparing pip from this Python's bundled files; no Python download is needed.",
        )
        status = _package_status(executable, requirements, root, environment, log, cancellation)
        if status.get("needs_parser") or (status["missing"] and not status.get("pip_available")):
            raise StartupFailure("pip could not be prepared locally. Review the setup log.")
    if phase == "background" and status.get("core_missing"):
        raise StartupFailure(
            "The editor packages need repair before background setup can continue. "
            "Close Studio and open it again to prepare the editor first."
        )
    if not status["missing"]:
        log.emit("status", "The required packages are already installed. No downloads are needed.")
        _check_imports(executable, phase, root, environment, log, cancellation)
        return

    constraint_path: Path | None = None
    try:
        arguments = [
            str(executable), "-I", "-X", "utf8", "-m", "pip", "--disable-pip-version-check",
            "install", "--no-input",
        ]
        if phase == "background":
            if not status["pins"]:
                raise StartupFailure("Installed package protection could not be verified. Close Studio and try setup again.")
            _check_imports(executable, "core", root, environment, log, cancellation)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", prefix=".hoyo-setup-constraints-", suffix=".txt",
                dir=root, delete=False,
            ) as constraints:
                constraint_path = Path(constraints.name)
                protected = [
                    pin for pin in status["pins"]
                    if pin.partition("==")[0].casefold() not in {"streamlit", "requests"}
                ]
                constraints.write("\n".join(protected) + "\n")
            arguments.extend(["--constraint", str(constraint_path)])
            log.emit("log", "Installed editor and shared packages are protected at their current versions while Studio is open.")
        arguments.extend(status["missing"])
        log.emit("log", "Preparing only: " + ", ".join(status["missing"]))
        try:
            _run_child(
                arguments, root, environment, log, cancellation,
                description=(
                    "Preparing Preview and network features; you can continue editing."
                    if phase == "background" else "Preparing the interface, code completion and syntax highlighting."
                ),
            )
        except SetupCancelled:
            raise
        except StartupFailure as error:
            if phase == "background":
                raise StartupFailure(
                    f"{error} Existing editor packages were protected from upgrades. "
                    "Check the setup log for a connection or package compatibility error, then retry. "
                    "For a dependency conflict, close Studio, open a terminal in this application folder, "
                    "and run .venv\\Scripts\\python.exe -m pip install -r requirements.txt before reopening it.",
                    error.exit_code,
                ) from error
            raise
        after = _package_status(executable, requirements, root, environment, log, cancellation)
        if after.get("needs_parser") or after["missing"]:
            remaining = ", ".join(after["missing"]) or "the package checker"
            raise StartupFailure("These packages are still unavailable after setup: " + remaining)
        _check_imports(executable, phase, root, environment, log, cancellation)
    finally:
        if constraint_path is not None:
            constraint_path.unlink(missing_ok=True)


def _recover_interrupted_update(root: Path, environment: dict[str, str], log: EventLog, cancellation: Cancellation) -> None:
    """Run only the original, checksum-verified helper before the setup lock.

    The downloaded application may be only partly installed after a power loss.
    Recovery therefore never imports any application module from this folder.
    Its pointer is restricted to Studio's private, durable per-user update area.
    """
    pointer_path = root / ".hoyo_update_pending.json"
    if not os.path.lexists(pointer_path):
        return

    def regular_path(path: Path, *, directory: bool = False) -> Path:
        if not path.is_absolute() or str(path).startswith("\\\\"):
            raise StartupFailure("The update recovery path is not a safe local folder.")
        for item in reversed((path, *path.parents)):
            information = item.lstat()
            if stat.S_ISLNK(information.st_mode) or getattr(information, "st_file_attributes", 0) & 0x400:
                raise StartupFailure("A linked update recovery file or folder is not allowed.")
            expected_directory = item != path or directory
            if expected_directory and not stat.S_ISDIR(information.st_mode):
                raise StartupFailure("The update recovery folder is invalid.")
            if not expected_directory and (not stat.S_ISREG(information.st_mode) or information.st_nlink > 1):
                raise StartupFailure("The update recovery record is not an ordinary file.")
        return path

    def read_record(path: Path) -> dict[str, Any]:
        def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise StartupFailure("An update recovery record contains duplicate fields.")
                result[key] = value
            return result

        regular_path(path)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise StartupFailure("The update recovery record is too large.")
        try:
            result = json.loads(path.read_bytes(), object_pairs_hook=unique_fields)
        except (UnicodeError, ValueError) as error:
            raise StartupFailure("The update recovery record could not be read.") from error
        if not isinstance(result, dict):
            raise StartupFailure("The update recovery record is invalid.")
        return result

    pointer = read_record(pointer_path)
    if (pointer.get("schema_version") != 1 or pointer.get("application") != "hoyo-project-studio"
            or root.name != "app" or pointer.get("install_root") != str(root.parent)):
        raise StartupFailure("The update recovery record does not belong to this application folder.")
    base = os.environ.get("LOCALAPPDATA")
    cache = (Path(base) if base else Path.home() / "AppData" / "Local") / "HoYo Project Studio" / "Updates"
    regular_path(cache, directory=True)
    job_text = pointer.get("job_file")
    if not isinstance(job_text, str):
        raise StartupFailure("The update recovery job is missing.")
    job_file = Path(job_text)
    if (job_file.name != "job.json" or job_file.parent.parent != cache
            or not re.fullmatch(r"hoyo-update-[A-Za-z0-9_-]+", job_file.parent.name)):
        raise StartupFailure("The update recovery job is outside Studio's private update folder.")
    job = read_record(job_file)
    expected = pointer.get("helper_sha256")
    if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or job.get("helper_sha256") != expected or job.get("install_root") != str(root.parent)
            or job.get("application") != "hoyo-project-studio" or job.get("schema_version") != 1):
        raise StartupFailure("The update recovery job does not match the trusted helper.")
    helper = regular_path(job_file.parent / "update_apply.py")
    if helper.stat().st_size > 2 * 1024 * 1024 or hashlib.sha256(helper.read_bytes()).hexdigest() != expected:
        raise StartupFailure("The original update recovery helper failed verification. Extract the complete Studio package into a new folder; your old projects and backups have been kept.")
    cancellation.check()
    _run_child(
        [sys.executable, "-I", "-X", "utf8", str(helper), "--recover", str(job_file)],
        root, environment, log, cancellation,
        description="Recovering an interrupted application update before opening Studio.", timeout=120,
    )
    if os.path.lexists(pointer_path):
        raise StartupFailure("The interrupted update is not yet recovered. Close other Studio windows and open the launcher again.")


def prepare(root: Path, phase: str, log: EventLog, cancellation: Cancellation) -> Path:
    if not _is_windows():
        raise StartupFailure("Use the Windows launcher to prepare Studio on your Windows PC.")
    cancellation.check()
    environment = child_environment()
    _recover_interrupted_update(root, environment, log, cancellation)
    if not (root / "requirements.txt").is_file():
        raise StartupFailure("requirements.txt is missing from the application folder.")
    with installation_lock(root):
        cancellation.check()
        executable = _choose_environment(root, environment, log, cancellation)
        marker = root / ".venv" / READY_NAME
        marker.unlink(missing_ok=True)
        _prepare_packages(executable, phase, root, environment, log, cancellation)
        cancellation.check()
        if phase == "background":
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".hoyo-ready-", suffix=".tmp", dir=marker.parent, delete=False,
            ) as ready_file:
                temporary_marker = Path(ready_file.name)
                ready_file.write(json.dumps({"ready": True, "executable": str(executable), "completed_at": time.time()}) + "\n")
            try:
                cancellation.check()
                temporary_marker.replace(marker)
            finally:
                temporary_marker.unlink(missing_ok=True)
        log.emit(
            "ready", "Studio's editor is ready." if phase == "core" else "Preview and network features are ready.",
            executable=str(executable),
        )
        return executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--phase", choices=("core", "background"), required=True)
    parser.add_argument("--cancel-file", help="Absolute, caller-owned path created to request cooperative cancellation")
    options = parser.parse_args(argv)
    root = Path(options.project_root)
    log: EventLog | None = None
    cancellation = Cancellation(Path(options.cancel_file) if options.cancel_file else None)
    previous_sigterm: Any = None
    installed_signal_handler = False
    try:
        if not root.is_absolute() or not root.is_dir():
            raise StartupFailure("--project-root must be an existing absolute Studio application folder.")
        if cancellation.path is not None and not cancellation.path.is_absolute():
            raise StartupFailure("--cancel-file must be an absolute path.")
        root = root.resolve()
        log = EventLog(root / LOG_NAME, options.phase)
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: cancellation.requested.set())
            installed_signal_handler = True
        log.emit("log", "HoYo Project Studio setup reuses installed Python; no interpreter will be downloaded.")
        prepare(root, options.phase, log, cancellation)
        return 0
    except (StartupFailure, OSError, KeyboardInterrupt) as error:
        if isinstance(error, KeyboardInterrupt):
            error = SetupCancelled()
        message = str(error)
        try:
            if log is None:
                print(json.dumps({"event": "error", "phase": options.phase, "message": message}), flush=True)
            else:
                log.emit("error", message)
        except (OSError, UnicodeError):
            pass
        return error.exit_code if isinstance(error, StartupFailure) else 1
    finally:
        if installed_signal_handler:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if log is not None:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
