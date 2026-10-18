"""Build Studio with an existing Windows Python installation.

The PowerShell entry point discovers an installed executable before launching
this file. This helper never invokes a Python launcher or an interpreter
installer. Creating a missing virtual environment uses that local executable;
only missing or incompatible application/build packages may need downloading.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
import platform
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO


PYINSTALLER_REQUIREMENT = "PyInstaller>=6.21,<7"
LOG_NAME = "build_windows_exe.log"
MIN_PYTHON = (3, 11)

_RUNTIME_PROBE = """
import json, platform, struct, sys
print(json.dumps({
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "implementation": platform.python_implementation(),
    "version": list(sys.version_info[:3]),
    "bits": struct.calcsize("P") * 8,
    "platform": sys.platform,
}))
"""

# This runs under the application environment, not the bootstrap interpreter.
# pip's bundled packaging parser avoids adding another bootstrap dependency.
# An existing standalone packaging installation also permits a completely
# prepared environment without pip to build without installing pip first.
_DEPENDENCY_PROBE = """
import importlib.metadata as metadata
import importlib.util
import json
import pathlib
import sys

try:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.version import InvalidVersion, Version
except ImportError:
    try:
        from packaging.requirements import Requirement
        from packaging.version import InvalidVersion, Version
    except ImportError:
        print(json.dumps({"needs_parser": True, "missing": []}))
        raise SystemExit(0)

requirements = []
for number, raw in enumerate(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8-sig").splitlines(), 1):
    line = raw.split(" #", 1)[0].strip()
    if not line or line.startswith("#"):
        continue
    try:
        requirement = Requirement(line)
    except Exception as error:
        raise SystemExit(f"Unsupported requirement at line {number}: {error}")
    if requirement.url is not None:
        raise SystemExit(f"Use a package name and version range at requirements.txt line {number}.")
    requirements.append((line, requirement))
requirements.append((sys.argv[2], Requirement(sys.argv[2])))

missing = []
installed = {}
for text, requirement in requirements:
    if requirement.marker is not None and not requirement.marker.evaluate():
        continue
    try:
        version = metadata.version(requirement.name)
        compatible = Version(version) in requirement.specifier
    except (metadata.PackageNotFoundError, InvalidVersion):
        version = None
        compatible = False
    installed[requirement.name] = version
    if not compatible:
        missing.append(text)
print(json.dumps({
    "needs_parser": False,
    "pip_available": importlib.util.find_spec("pip") is not None,
    "missing": missing,
    "installed": installed,
}))
"""


class BuildFailure(RuntimeError):
    """A reported build failure with a subprocess-compatible exit status."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code if exit_code != 0 else 1


class BuildLog:
    """Write live progress to the terminal and a UTF-8 troubleshooting log."""

    def __init__(self, path: Path, stream: TextIO | None = None) -> None:
        self.path = path
        self.stream = sys.stdout if stream is None else stream
        self.file = path.open("w", encoding="utf-8", newline="")

    def write(self, text: str) -> None:
        self.file.write(text)
        self.file.flush()
        if self.stream is not None:
            try:
                self.stream.write(text)
                self.stream.flush()
            except (OSError, UnicodeError):
                # A legacy console encoding must not interrupt a build. The
                # complete Unicode output remains available in the log file.
                pass

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    def close(self) -> None:
        self.file.close()


def _is_windows() -> bool:
    return os.name == "nt"


def _current_runtime_info() -> dict[str, Any]:
    return {
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "implementation": platform.python_implementation(),
        "version": list(sys.version_info[:3]),
        "bits": struct.calcsize("P") * 8,
        "platform": sys.platform,
    }


def child_environment() -> dict[str, str]:
    """Disable runtime auto-installation for this process's children only."""

    environment = dict(os.environ)
    # Windows environment names are case-insensitive, including when callers
    # supplied mixed-case names before launching this helper directly.
    cleared = {
        "python_manager_automatic_install",
        "pylauncher_allow_install",
        "pylauncher_always_install",
        "pythonhome",
        "pythonpath",
    }
    for name in list(environment):
        if name.casefold() in cleared:
            del environment[name]
    environment["PYTHON_MANAGER_AUTOMATIC_INSTALL"] = "false"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_NO_INPUT"] = "1"
    return environment


def _validate_runtime(info: dict[str, Any], description: str, *, environment_root: Path | None = None) -> None:
    try:
        version = tuple(int(part) for part in info["version"][:2])
        valid = (
            info["platform"] == "win32"
            and info["implementation"] == "CPython"
            and version >= MIN_PYTHON
            and int(info["bits"]) == 64
            and bool(info["executable"])
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise BuildFailure(
            f"{description} must be 64-bit CPython 3.11 or newer for Windows. "
            "No Python interpreter has been downloaded or installed."
        )
    if environment_root is not None:
        try:
            prefix = Path(info["prefix"]).resolve()
            base_prefix = Path(info["base_prefix"]).resolve()
            is_environment = prefix == environment_root.resolve() and prefix != base_prefix
        except (KeyError, TypeError, OSError, ValueError):
            is_environment = False
        if not is_environment:
            raise BuildFailure(
                "The existing .venv Python does not belong to this application's virtual environment. "
                "The environment has been left unchanged. Restore it or move it aside yourself before building again."
            )


def _capture_json(
    arguments: list[str], root: Path, environment: dict[str, str], log: BuildLog, *, description: str,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            arguments, cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildFailure(f"{description} could not finish: {error}") from error
    if completed.stderr:
        log.write(completed.stderr)
        if not completed.stderr.endswith("\n"):
            log.line()
    if completed.returncode != 0:
        if completed.stdout:
            log.write(completed.stdout)
            if not completed.stdout.endswith("\n"):
                log.line()
        raise BuildFailure(f"{description} failed (exit code {completed.returncode}).", completed.returncode)
    try:
        value = json.loads(completed.stdout.strip())
    except (TypeError, ValueError) as error:
        raise BuildFailure(f"{description} returned an invalid result.") from error
    if not isinstance(value, dict):
        raise BuildFailure(f"{description} returned an invalid result.")
    return value


def _stop_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except OSError:
        pass


def _run_stage(
    arguments: list[str], root: Path, environment: dict[str, str], log: BuildLog, *, description: str,
) -> None:
    log.line(description)
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            arguments, cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
        status = process.wait()
    except KeyboardInterrupt:
        if process is not None:
            _stop_child(process)
        raise
    except OSError as error:
        if process is not None:
            _stop_child(process)
        raise BuildFailure(f"{description} could not finish: {error}") from error
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()
    if status != 0:
        raise BuildFailure(f"{description} failed (exit code {status}).", status)


def _choose_environment(root: Path, environment: dict[str, str], log: BuildLog) -> Path:
    current = _current_runtime_info()
    _validate_runtime(current, "The selected installed Python")
    base_python = Path(current["executable"]).resolve()
    log.line(f"Installed Python: {base_python}")
    environment_root = root / ".venv"
    executable = environment_root / "Scripts" / "python.exe"
    if environment_root.exists() or environment_root.is_symlink():
        if not executable.is_file():
            raise BuildFailure(
                "The existing .venv is incomplete: .venv\\Scripts\\python.exe is missing. "
                "It has been left unchanged. Restore it or move it aside yourself before building again."
            )
        log.line("Reusing the application's existing .venv; Python will not be downloaded.")
    else:
        _run_stage(
            [str(base_python), "-I", "-X", "utf8", "-m", "venv", str(environment_root)], root, environment, log,
            description="Creating the local .venv from installed Python (no Python download)...",
        )
        if not executable.is_file():
            raise BuildFailure("Python did not create .venv\\Scripts\\python.exe. Review the environment creation output above.")
    try:
        info = _capture_json(
            [str(executable), "-I", "-c", _RUNTIME_PROBE], root, environment, log,
            description="Application Python check",
        )
        _validate_runtime(info, "The application's .venv Python", environment_root=environment_root)
    except BuildFailure as error:
        raise BuildFailure(
            f"{error}\nThe .venv has not been deleted or replaced. Restore it or move it aside yourself before building again.",
            error.exit_code,
        ) from error
    version = ".".join(str(part) for part in info["version"])
    log.line(f"Build Python: {executable} (Python {version}, 64-bit)")
    return executable


def _dependency_status(executable: Path, root: Path, environment: dict[str, str], log: BuildLog) -> dict[str, Any]:
    result = _capture_json(
        [str(executable), "-I", "-c", _DEPENDENCY_PROBE, str(root / "requirements.txt"), PYINSTALLER_REQUIREMENT],
        root, environment, log, description="Installed package check",
    )
    if not isinstance(result.get("missing"), list) or any(not isinstance(item, str) for item in result["missing"]):
        raise BuildFailure("Installed package check returned an invalid package list.")
    return result


def _prepare_packages(executable: Path, root: Path, environment: dict[str, str], log: BuildLog) -> None:
    log.line("Checking installed application and build packages...")
    status = _dependency_status(executable, root, environment, log)
    if status.get("needs_parser") or (status["missing"] and not status.get("pip_available")):
        _run_stage(
            [str(executable), "-I", "-X", "utf8", "-m", "ensurepip"], root, environment, log,
            description="Restoring pip from this installed Python's bundled files (no Python download)...",
        )
        status = _dependency_status(executable, root, environment, log)
        if status.get("needs_parser") or (status["missing"] and not status.get("pip_available")):
            raise BuildFailure("pip could not be restored in .venv. Review the local ensurepip output above.")
    for name, version in status.get("installed", {}).items():
        log.line(f"  {name}: {version or 'not installed'}")
    if not status["missing"]:
        log.line("All required packages are already suitable. Skipping package installation and downloads.")
        return
    log.line("Only these missing or incompatible packages will be installed:")
    for requirement in status["missing"]:
        log.line(f"  {requirement}")
    _run_stage(
        [str(executable), "-I", "-X", "utf8", "-m", "pip", "--disable-pip-version-check", "install", "--no-input", *status["missing"]],
        root, environment, log,
        description="Installing required packages in .venv (this may download packages, not Python)...",
    )
    after = _dependency_status(executable, root, environment, log)
    if after.get("needs_parser") or after["missing"]:
        remaining = ", ".join(after["missing"]) or "the package version checker"
        raise BuildFailure(f"The required packages are still unavailable after installation: {remaining}")


def _open_output(executable: Path, environment: dict[str, str], log: BuildLog) -> None:
    explorer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
    try:
        subprocess.Popen(
            [str(explorer), "/select,", str(executable)], env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
        )
    except OSError as error:
        log.line(f"The build succeeded, but Explorer could not be opened: {error}")
        log.line(f"Open this folder to find your executable: {executable.parent}")


def _build_locked(root: Path, log: BuildLog) -> Path:
    """Prepare local packages and build the reviewed application specification."""

    if not _is_windows():
        raise BuildFailure("A Windows EXE must be built on Windows. Run Build Windows EXE.cmd on your Windows PC.")
    for name in ("requirements.txt", "HoYoProjectStudio.spec"):
        if not (root / name).is_file():
            raise BuildFailure(f"{name} is missing from the application folder: {root}")
    environment = child_environment()
    executable = _choose_environment(root, environment, log)
    _prepare_packages(executable, root, environment, log)
    _run_stage(
        [str(executable), "-I", "-X", "utf8", "-m", "PyInstaller", "--noconfirm", "--clean", str(root / "HoYoProjectStudio.spec")],
        root, environment, log, description="Building the Windows application...",
    )
    output = root / "dist" / "HoYo Project Studio" / "HoYo Project Studio.exe"
    if not output.is_file() or output.stat().st_size == 0:
        raise BuildFailure(
            "PyInstaller finished without producing the expected executable. "
            f"Build output was not confirmed: {output}"
        )
    log.line()
    log.line("Build complete:")
    log.line(str(output))
    log.line(f"Build log: {log.path}")
    _open_output(output, environment, log)
    return output


@contextmanager
def _installation_guard(root: Path):
    """Prevent a build and staged setup from installing into .venv together."""

    support_path = Path(__file__).with_name("startup.py")
    if not support_path.is_file():
        raise BuildFailure("The setup helper is missing. Extract the complete update before building.")
    specification = importlib.util.spec_from_file_location("hoyo_studio_startup_support", support_path)
    if specification is None or specification.loader is None:
        raise BuildFailure("The setup helper could not be loaded.")
    support = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(support)
    try:
        with support.installation_lock(root):
            yield
    except support.StartupFailure as error:
        raise BuildFailure(str(error), error.exit_code) from error


def build(root: Path, log: BuildLog) -> Path:
    with _installation_guard(root):
        return _build_locked(root, log)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, help="Absolute path to the extracted Studio application folder")
    options = parser.parse_args(argv)
    root = Path(options.project_root)
    if not root.is_absolute() or not root.is_dir():
        print("[ERROR] --project-root must be an existing absolute application folder.", file=sys.stderr)
        return 1
    root = root.resolve()
    log: BuildLog | None = None
    try:
        # Take ownership before truncating the log. A rejected second build
        # must leave the active build's output intact.
        with _installation_guard(root):
            log = BuildLog(root / LOG_NAME)
            log.line("HoYo Project Studio - Build Windows EXE")
            log.line(f"Application folder: {root}")
            log.line("This build reuses installed Python. It does not download or install a Python interpreter.")
            log.line()
            _build_locked(root, log)
        return 0
    except BuildFailure as error:
        if log is None:
            print(f"[ERROR] {error}", file=sys.stderr)
        else:
            log.line()
            log.line(f"[ERROR] {error}")
            log.line(f"Build log: {log.path}")
        return error.exit_code
    except KeyboardInterrupt:
        if log is not None:
            log.line("\n[ERROR] Build cancelled. No successful build was confirmed.")
            log.line(f"Build log: {log.path}")
        else:
            print("[ERROR] Build cancelled.", file=sys.stderr)
        return 130
    except OSError as error:
        # Includes a full disk, missing permissions and unreadable output files.
        # Keep the command wrapper's failure path available rather than treating
        # a completed subprocess as a successful artifact.
        message = f"[ERROR] The build could not finish: {error}"
        try:
            if log is not None:
                log.line(message)
            else:
                print(message, file=sys.stderr)
        except OSError:
            print(message, file=sys.stderr)
        return 1
    finally:
        if log is not None:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
