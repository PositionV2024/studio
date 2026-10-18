"""Verify Windows build decisions with simulated Windows subprocesses.

These tests do not build a Windows executable on Linux. They exercise the real
helper's reuse, installation, logging and failure paths, and run its real
package-version probe in the test Python interpreter.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from launcher import windows_build as build
from launcher.startup import installation_lock


class FakeProcess:
    def __init__(self, output: str = "", status: int = 0) -> None:
        self.stdout = io.StringIO(output)
        self.returncode = status

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 143

    def kill(self):
        self.returncode = 137


class WindowsBuildFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.base = root / "installed Python 3.15" / "python.exe"
        self.base.parent.mkdir()
        self.base.write_bytes(b"existing installed runtime")
        self.environment_root = root / ".venv"
        self.python = self.environment_root / "Scripts" / "python.exe"
        self.output = root / "dist" / "HoYo Project Studio" / "HoYo Project Studio.exe"
        self.commands: list[tuple[list[str], dict]] = []
        self.current_info = self.runtime_info(self.base, self.base.parent, self.base.parent)
        self.venv_info = self.runtime_info(self.python, self.environment_root, self.base.parent)
        self.runtime_status = 0
        self.runtime_output = ""
        self.parser_missing = False
        self.pip_available = True
        self.missing: list[str] = []
        self.install_status = 0
        self.keep_missing_after_install = False
        self.build_status = 0
        self.produce_exe = True
        self.explorer_error = False
        (root / "requirements.txt").write_text("PySide6>=6.8,<7\njedi>=0.19.2,<0.21\n", encoding="utf-8")
        (root / "HoYoProjectStudio.spec").write_text("# Simulated Windows build specification\n", encoding="utf-8")

    @staticmethod
    def runtime_info(executable: Path, prefix: Path, base_prefix: Path) -> dict:
        return {
            "executable": str(executable),
            "prefix": str(prefix),
            "base_prefix": str(base_prefix),
            "implementation": "CPython",
            "version": [3, 15, 0],
            "bits": 64,
            "platform": "win32",
        }

    def create_environment(self) -> None:
        self.python.parent.mkdir(parents=True, exist_ok=True)
        self.python.write_bytes(b"existing venv runtime")
        (self.environment_root / "pyvenv.cfg").write_text(f"home = {self.base.parent}\n", encoding="utf-8")

    def run(self, arguments: list[str], **kwargs):
        self.commands.append((list(arguments), kwargs))
        assert kwargs["cwd"] == self.root
        if build._RUNTIME_PROBE in arguments:
            assert Path(arguments[0]) == self.python
            return subprocess.CompletedProcess(
                arguments, self.runtime_status,
                json.dumps(self.venv_info) if not self.runtime_status else "",
                self.runtime_output,
            )
        assert build._DEPENDENCY_PROBE in arguments
        assert Path(arguments[0]) == self.python
        return subprocess.CompletedProcess(arguments, 0, json.dumps({
            "needs_parser": self.parser_missing,
            "pip_available": self.pip_available,
            "missing": list(self.missing),
            "installed": {"PySide6": "6.12.0", "jedi": "0.20.1", "PyInstaller": "6.22.3"},
        }), "")

    def popen(self, arguments: list[str], **kwargs):
        self.commands.append((list(arguments), kwargs))
        if "-m" not in arguments:
            assert arguments[1:] == ["/select,", str(self.output)]
            if self.explorer_error:
                raise OSError("simulated Explorer launch failure")
            return FakeProcess()
        module = arguments[arguments.index("-m") + 1]
        assert kwargs["cwd"] == self.root
        if module == "venv":
            assert Path(arguments[0]) == self.base
            assert arguments[-1] == str(self.environment_root)
            self.create_environment()
            return FakeProcess("Local virtual environment created.\n")
        assert Path(arguments[0]) == self.python
        if module == "ensurepip":
            self.parser_missing = False
            self.pip_available = True
            return FakeProcess("Bundled pip restored locally.\n")
        if module == "pip":
            if self.install_status == 0 and not self.keep_missing_after_install:
                self.missing = []
            return FakeProcess("Simulated package installation output.\n", self.install_status)
        assert module == "PyInstaller"
        if self.build_status == 0 and self.produce_exe:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_bytes(b"MZ simulated new Windows build output")
        return FakeProcess("Simulated PyInstaller output.\n", self.build_status)

    def modules(self) -> list[str]:
        return [arguments[arguments.index("-m") + 1] for arguments, _ in self.commands if "-m" in arguments]

    def launch(self) -> int:
        return build.main(["--project-root", str(self.root)])

    def log(self) -> str:
        return (self.root / build.LOG_NAME).read_text(encoding="utf-8")

    def explorer_calls(self) -> list[list[str]]:
        return [arguments for arguments, _ in self.commands if "/select," in arguments]


@pytest.fixture
def windows(tmp_path, monkeypatch):
    root = tmp_path / "Studio application with spaces"
    root.mkdir()
    fixture = WindowsBuildFixture(root)
    monkeypatch.setattr(build, "_is_windows", lambda: True)
    monkeypatch.setattr(build, "_current_runtime_info", lambda: fixture.current_info)
    monkeypatch.setattr(build.subprocess, "run", fixture.run)
    monkeypatch.setattr(build.subprocess, "Popen", fixture.popen)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("The build must never wait for user input."))
    return fixture


def test_ready_environment_reuses_python_and_packages_then_returns_without_input(windows):
    windows.create_environment()
    original_python = windows.python.read_bytes()

    assert windows.launch() == 0

    assert windows.modules() == ["PyInstaller"]
    assert windows.python.read_bytes() == original_python
    assert len(windows.explorer_calls()) == 1
    assert "Python 3.15.0, 64-bit" in windows.log()
    assert "Skipping package installation and downloads" in windows.log()
    assert "Simulated PyInstaller output." in windows.log()
    assert "Build complete:" in windows.log()
    assert str(windows.output) in windows.log()


def test_first_build_creates_only_local_venv_from_already_installed_python315(windows):
    assert not windows.environment_root.exists()

    assert windows.launch() == 0

    assert windows.modules() == ["venv", "PyInstaller"]
    creation = next(arguments for arguments, _ in windows.commands if "venv" in arguments)
    assert creation == [str(windows.base), "-I", "-X", "utf8", "-m", "venv", str(windows.environment_root)]
    assert windows.base.read_bytes() == b"existing installed runtime"
    assert "no Python download" in windows.log()


def test_incomplete_environment_is_preserved_and_never_recreated(windows):
    windows.environment_root.mkdir()
    retained = windows.environment_root / "existing user note.txt"
    retained.write_text("Keep this environment for diagnosis.", encoding="utf-8")

    assert windows.launch() == 1

    assert windows.commands == []
    assert retained.read_text(encoding="utf-8") == "Keep this environment for diagnosis."
    assert not windows.python.exists()
    assert "existing .venv is incomplete" in windows.log()


def test_unusable_existing_environment_fails_without_replacing_it(windows):
    windows.create_environment()
    windows.runtime_status = 103
    windows.runtime_output = "The original base Python was removed.\n"
    original = windows.python.read_bytes()

    assert windows.launch() == 103

    assert windows.modules() == []
    assert windows.python.read_bytes() == original
    assert "The original base Python was removed." in windows.log()
    assert "has not been deleted or replaced" in windows.log()


def test_python_must_belong_to_the_expected_virtual_environment(windows):
    windows.create_environment()
    windows.venv_info["prefix"] = str(windows.base.parent)

    assert windows.launch() == 1

    assert windows.modules() == []
    assert "does not belong to this application's virtual environment" in windows.log()


@pytest.mark.parametrize("override", [
    {"implementation": "PyPy"},
    {"version": [3, 10, 14]},
    {"bits": 32},
    {"platform": "linux"},
])
def test_unsupported_bootstrap_python_fails_before_any_creation_or_install(windows, override):
    windows.current_info.update(override)

    assert windows.launch() == 1

    assert windows.commands == []
    assert not windows.environment_root.exists()
    assert "64-bit CPython 3.11 or newer for Windows" in windows.log()


def test_only_missing_or_outdated_package_ranges_are_passed_to_pip(windows):
    windows.create_environment()
    windows.missing = ["jedi>=0.19.2,<0.21", "PyInstaller>=6.21,<7"]

    assert windows.launch() == 0

    assert windows.modules() == ["pip", "PyInstaller"]
    install = next(arguments for arguments, _ in windows.commands if "pip" in arguments)
    assert install == [
        str(windows.python), "-I", "-X", "utf8", "-m", "pip", "--disable-pip-version-check", "install", "--no-input",
        "jedi>=0.19.2,<0.21", "PyInstaller>=6.21,<7",
    ]
    assert "--upgrade" not in install
    assert not any("PySide6" in argument for argument in install)


def test_missing_pip_is_bootstrapped_from_local_bundled_files_only_when_needed(windows):
    windows.create_environment()
    windows.parser_missing = True
    windows.pip_available = False
    windows.missing = ["PyInstaller>=6.21,<7"]

    assert windows.launch() == 0

    assert windows.modules() == ["ensurepip", "pip", "PyInstaller"]
    assert "bundled files" in windows.log()
    assert not any("get-pip" in str(arguments) for arguments, _ in windows.commands)


def test_fully_prepared_environment_can_build_without_pip(windows):
    windows.create_environment()
    windows.pip_available = False

    assert windows.launch() == 0

    assert windows.modules() == ["PyInstaller"]


def test_package_install_failure_is_logged_and_prevents_build(windows):
    windows.create_environment()
    windows.missing = ["PyInstaller>=6.21,<7"]
    windows.install_status = 24

    assert windows.launch() == 24

    assert windows.modules() == ["pip"]
    assert "Simulated package installation output." in windows.log()
    assert "exit code 24" in windows.log()
    assert "Build complete:" not in windows.log()
    assert windows.explorer_calls() == []


def test_packages_are_rechecked_and_incomplete_install_does_not_start_build(windows):
    windows.create_environment()
    windows.missing = ["PyInstaller>=6.21,<7"]
    windows.keep_missing_after_install = True

    assert windows.launch() == 1

    assert windows.modules() == ["pip"]
    assert "still unavailable after installation" in windows.log()
    assert windows.explorer_calls() == []


def test_failed_pyinstaller_never_reports_an_old_executable_as_new_success(windows):
    windows.create_environment()
    windows.output.parent.mkdir(parents=True)
    windows.output.write_bytes(b"MZ old output from last week")
    windows.build_status = 7

    assert windows.launch() == 7

    assert windows.output.read_bytes() == b"MZ old output from last week"
    assert "exit code 7" in windows.log()
    assert "Build complete:" not in windows.log()
    assert windows.explorer_calls() == []


def test_zero_exit_without_the_expected_executable_is_still_failure(windows):
    windows.create_environment()
    windows.produce_exe = False

    assert windows.launch() == 1

    assert "without producing the expected executable" in windows.log()
    assert "Build complete:" not in windows.log()
    assert windows.explorer_calls() == []


def test_failure_to_open_explorer_does_not_invalidate_completed_build(windows):
    windows.create_environment()
    windows.explorer_error = True

    assert windows.launch() == 0

    assert windows.output.is_file()
    assert "build succeeded, but Explorer could not be opened" in windows.log()
    assert str(windows.output.parent) in windows.log()


def test_children_disable_runtime_installation_without_changing_users_environment(windows, monkeypatch):
    windows.create_environment()
    monkeypatch.setenv("PYTHON_MANAGER_AUTOMATIC_INSTALL", "true")
    monkeypatch.setenv("PYLAUNCHER_ALLOW_INSTALL", "0")  # Any legacy value enables installation.
    monkeypatch.setenv("PYLAUNCHER_ALWAYS_INSTALL", "1")
    monkeypatch.setenv("PyLaUnChEr_AlLoW_InStAlL", "yes")
    monkeypatch.setenv("PYTHONHOME", "a different Python installation")
    monkeypatch.setenv("PYTHONPATH", "unrelated module directory")
    original = dict(os.environ)

    assert windows.launch() == 0

    assert dict(os.environ) == original
    for _, options in windows.commands:
        environment = options["env"]
        assert environment["PYTHON_MANAGER_AUTOMATIC_INSTALL"] == "false"
        assert not any(name.casefold() in {
            "pylauncher_allow_install", "pylauncher_always_install", "pythonhome", "pythonpath",
        } for name in environment)
        assert environment["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
        assert environment["PIP_NO_INPUT"] == "1"


def test_actual_non_windows_host_is_rejected_without_starting_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "_is_windows", lambda: False)
    monkeypatch.setattr(build.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Must not launch a process."))
    monkeypatch.setattr(build.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("Must not launch a process."))

    assert build.main(["--project-root", str(tmp_path)]) == 1

    assert "must be built on Windows" in (tmp_path / build.LOG_NAME).read_text(encoding="utf-8")


@pytest.mark.parametrize("installer_version, expected", [
    ("6.10.0", ["jedi>=0.19.2,<0.21", "PyInstaller>=6.21,<7"]),
    ("6.22.3", ["jedi>=0.19.2,<0.21"]),
])
def test_real_dependency_probe_checks_versions_and_markers_without_installing(tmp_path, installer_version, expected):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "\ufeff# Real parser smoke\nPySide6>=6.8,<7\njedi>=0.19.2,<0.21 # Needs updating\n"
        "absent-inapplicable-package>=1; python_version < '3.1'\n",
        encoding="utf-8",
    )
    versions = {"PySide6": "6.12.0", "jedi": "0.18.2", "PyInstaller": installer_version}
    prefix = "import importlib.metadata as metadata\n" + f"metadata.version = {versions!r}.__getitem__\n"

    completed = subprocess.run(
        [sys.executable, "-I", "-c", prefix + build._DEPENDENCY_PROBE, str(requirements), build.PYINSTALLER_REQUIREMENT],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert not result["needs_parser"], "This smoke test needs pip or packaging in the test interpreter."
    assert result["missing"] == expected
    assert result["installed"]["PySide6"] == "6.12.0"


def test_rejected_second_build_preserves_the_active_build_log(windows):
    path = windows.root / build.LOG_NAME
    path.write_text("The active build's log must survive.\n", encoding="utf-8")
    with installation_lock(windows.root):
        assert windows.launch() == 1
    assert windows.commands == []
    assert path.read_text(encoding="utf-8") == "The active build's log must survive.\n"
