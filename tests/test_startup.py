"""Staged setup decisions and real cancellation/lock boundaries.

Windows interpreter and package operations are simulated on Linux. The package
metadata probe, JSON protocol, interprocess lock and cancellation tree cleanup
also run as real subprocesses; these tests do not claim a native Windows run.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from launcher import startup


class SetupFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.base = root / "Installed Python 3.15" / "python.exe"
        self.base.parent.mkdir()
        self.base.write_bytes(b"existing Python")
        self.environment_root = root / ".venv"
        self.python = self.environment_root / "Scripts" / "python.exe"
        self.marker = self.environment_root / startup.READY_NAME
        self.current = self.runtime_info(self.base, self.base.parent, self.base.parent)
        self.runtime = self.runtime_info(self.python, self.environment_root, self.base.parent)
        self.calls: list[list[str]] = []
        self.constraints: list[str] = []
        self.core_missing: list[str] = []
        self.background_missing: list[str] = []
        self.pins = [
            "PySide6==6.12.0", "PySide6_Addons==6.12.0", "PySide6_Essentials==6.12.0", "shiboken6==6.12.0",
            "jedi==0.20.1", "parso==0.8.6", "Pygments==2.21.0", "packaging==26.0",
        ]
        self.parser_missing = False
        self.pip_available = True
        self.install_exit = 0
        self.keep_missing = False
        self.import_error: str | None = None
        self.cancel_during_install = False
        (root / "requirements.txt").write_text(
            "PySide6>=6.8,<7\nstreamlit>=1.40\nrequests>=2.32\njedi>=0.19.2,<0.21\nPygments>=2.18,<3\n",
            encoding="utf-8",
        )

    @staticmethod
    def runtime_info(executable: Path, prefix: Path, base_prefix: Path) -> dict:
        return {
            "executable": str(executable), "prefix": str(prefix), "base_prefix": str(base_prefix),
            "implementation": "CPython", "version": [3, 15, 0], "bits": 64, "platform": "win32",
        }

    def create_environment(self) -> None:
        self.python.parent.mkdir(parents=True, exist_ok=True)
        self.python.write_bytes(b"existing venv interpreter")
        (self.environment_root / "pyvenv.cfg").write_text(f"home = {self.base.parent}\n", encoding="utf-8")

    def run_child(self, arguments, root, environment, log, cancellation, *, description, capture=False, timeout=None):
        cancellation.check()
        self.calls.append(list(arguments))
        assert root == self.root
        assert environment["PYTHON_MANAGER_AUTOMATIC_INSTALL"] == "false"
        log.emit("status", description)
        if startup._RUNTIME_PROBE in arguments:
            return json.dumps(self.runtime)
        if startup._PACKAGE_PROBE in arguments:
            requirements = json.loads(arguments[-1])
            is_background = any(requirement.startswith("streamlit") for requirement in requirements)
            return json.dumps({
                "needs_parser": self.parser_missing,
                "pip_available": self.pip_available,
                "missing": self.core_missing + (self.background_missing if is_background else []),
                "core_missing": self.core_missing,
                "installed": {"pyside6": "6.12.0", "jedi": "0.20.1", "pygments": "2.21.0"},
                "pins": self.pins,
            })
        if startup._IMPORT_PROBE in arguments:
            if self.import_error is not None:
                raise startup.StartupFailure(self.import_error, 13)
            return json.dumps({"ready": True, "phase": arguments[-1]})
        module = arguments[arguments.index("-m") + 1]
        if module == "venv":
            assert arguments[0] == str(self.base)
            self.create_environment()
        elif module == "ensurepip":
            self.parser_missing = False
            self.pip_available = True
        elif module == "pip":
            if "--constraint" in arguments:
                path = Path(arguments[arguments.index("--constraint") + 1])
                self.constraints = path.read_text(encoding="utf-8").splitlines()
            log.emit("log", "Downloading a missing feature package: simulated test output.")
            if self.cancel_during_install:
                cancellation.requested.set()
                cancellation.check()
            if self.install_exit:
                raise startup.StartupFailure("Simulated package download failed.", self.install_exit)
            if not self.keep_missing:
                self.core_missing = [value for value in self.core_missing if value not in arguments]
                self.background_missing = [value for value in self.background_missing if value not in arguments]
        else:
            pytest.fail(f"Unexpected setup subprocess: {arguments!r}")
        return ""

    def launch(self, phase="core", *, cancel_file: Path | None = None) -> int:
        arguments = ["--project-root", str(self.root), "--phase", phase]
        if cancel_file is not None:
            arguments.extend(["--cancel-file", str(cancel_file)])
        return startup.main(arguments)

    def modules(self) -> list[str]:
        return [arguments[arguments.index("-m") + 1] for arguments in self.calls if "-m" in arguments]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "Studio application with spaces"
    root.mkdir()
    fixture = SetupFixture(root)
    monkeypatch.setattr(startup, "_is_windows", lambda: True)
    monkeypatch.setattr(startup, "_current_runtime_info", lambda: fixture.current)
    monkeypatch.setattr(startup, "_run_child", fixture.run_child)
    return fixture


def events(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_prepared_core_opens_without_downloads_and_keeps_completion_highlighting(setup, capsys):
    setup.create_environment()
    original = setup.python.read_bytes()
    setup.background_missing = ["streamlit>=1.40", "requests>=2.32"]

    assert setup.launch() == 0

    assert setup.modules() == []
    assert setup.python.read_bytes() == original
    probe = next(call for call in setup.calls if startup._PACKAGE_PROBE in call)
    assert json.loads(probe[-1]) == list(startup.CORE_REQUIREMENTS)
    assert any(startup._IMPORT_PROBE in call and call[-1] == "core" for call in setup.calls)
    assert not setup.marker.exists()
    ready = [event for event in events(capsys) if event["event"] == "ready"]
    assert ready == [{"event": "ready", "phase": "core", "message": "Studio's editor is ready.", "executable": str(setup.python)}]


def test_first_launch_creates_environment_from_local_python315_and_installs_only_core(setup):
    setup.core_missing = list(startup.CORE_REQUIREMENTS)
    setup.background_missing = ["streamlit>=1.40", "requests>=2.32"]

    assert setup.launch() == 0

    assert setup.modules() == ["venv", "pip"]
    creation = next(call for call in setup.calls if "venv" in call)
    assert creation == [str(setup.base), "-I", "-X", "utf8", "-m", "venv", str(setup.environment_root)]
    install = next(call for call in setup.calls if "pip" in call)
    assert install[-3:] == list(startup.CORE_REQUIREMENTS)
    assert "--upgrade" not in install
    assert "streamlit>=1.40" not in install and "requests>=2.32" not in install
    assert setup.base.read_bytes() == b"existing Python"
    assert not setup.marker.exists()


def test_background_installs_missing_features_with_gui_and_transitive_versions_protected(setup, capsys):
    setup.create_environment()
    setup.background_missing = ["streamlit>=1.40", "requests>=2.32"]

    assert setup.launch("background") == 0

    install = next(call for call in setup.calls if "pip" in call)
    assert install[-2:] == ["streamlit>=1.40", "requests>=2.32"]
    assert "--upgrade" not in install
    assert setup.constraints == setup.pins
    assert not list(setup.root.glob(".hoyo-setup-constraints-*.txt"))
    assert json.loads(setup.marker.read_text(encoding="utf-8"))["ready"] is True
    assert [event["phase"] for event in events(capsys) if event["event"] == "ready"] == ["background"]


def test_background_can_update_feature_entry_points_but_not_their_shared_dependencies(setup):
    setup.create_environment()
    setup.background_missing = ["streamlit>=1.40", "requests>=2.32"]
    setup.pins.extend(["streamlit==1.20.0", "requests==2.20.0", "urllib3==2.5.0", "typing_extensions==4.15.0"])

    assert setup.launch("background") == 0

    assert "streamlit==1.20.0" not in setup.constraints
    assert "requests==2.20.0" not in setup.constraints
    assert "urllib3==2.5.0" in setup.constraints
    assert "typing_extensions==4.15.0" in setup.constraints
    assert "PySide6==6.12.0" in setup.constraints
    assert "parso==0.8.6" in setup.constraints


def test_background_refuses_to_repair_core_while_the_gui_is_running(setup, capsys):
    setup.create_environment()
    setup.core_missing = ["PySide6>=6.8,<7"]
    setup.background_missing = ["streamlit>=1.40"]

    assert setup.launch("background") == 1

    assert setup.modules() == []
    assert not setup.marker.exists()
    assert "Close Studio" in events(capsys)[-1]["message"]


def test_ready_background_rechecks_imports_without_pip_or_downloads(setup):
    setup.create_environment()
    setup.pip_available = False

    assert setup.launch("background") == 0

    assert setup.modules() == []
    assert any(startup._IMPORT_PROBE in call and call[-1] == "background" for call in setup.calls)
    assert setup.marker.exists()


def test_missing_pip_is_prepared_using_local_bundled_files_without_upgrades(setup):
    setup.create_environment()
    setup.parser_missing = True
    setup.pip_available = False
    setup.core_missing = ["jedi>=0.19.2,<0.21"]

    assert setup.launch() == 0

    assert setup.modules() == ["ensurepip", "pip"]
    assert not any("--upgrade" in call or any("get-pip" in value for value in call) for call in setup.calls)


def test_broken_environment_is_preserved_without_installing_anything(setup, capsys):
    setup.environment_root.mkdir()
    note = setup.environment_root / "keep my troubleshooting note.txt"
    note.write_text("Retain this.", encoding="utf-8")

    assert setup.launch() == 1

    assert setup.calls == []
    assert note.read_text(encoding="utf-8") == "Retain this."
    assert "left unchanged" in events(capsys)[-1]["message"]


@pytest.mark.parametrize("change", [{"bits": 32}, {"version": [3, 10, 14]}, {"implementation": "PyPy"}, {"platform": "linux"}])
def test_incompatible_installed_interpreter_is_rejected_before_any_setup(setup, change):
    setup.current.update(change)

    assert setup.launch() == 1
    assert setup.calls == []
    assert not setup.environment_root.exists()


def test_environment_must_belong_to_this_application(setup, capsys):
    setup.create_environment()
    setup.runtime["prefix"] = str(setup.base.parent)

    assert setup.launch() == 1

    assert setup.modules() == []
    assert "does not belong" in events(capsys)[-1]["message"]


@pytest.mark.parametrize("phase", ["core", "background"])
def test_metadata_is_not_enough_actual_import_failure_prevents_ready(setup, capsys, phase):
    setup.create_environment()
    setup.marker.write_text("stale ready marker", encoding="utf-8")
    setup.import_error = "Simulated QtWebEngine import failure"

    assert setup.launch(phase) == 13

    assert not setup.marker.exists()
    output = events(capsys)
    assert not any(event["event"] == "ready" for event in output)
    assert "QtWebEngine import failure" in output[-1]["message"]


def test_download_failure_clears_stale_readiness_and_preserves_environment(setup, capsys):
    setup.create_environment()
    setup.marker.write_text("stale ready marker", encoding="utf-8")
    setup.background_missing = ["streamlit>=1.40"]
    setup.install_exit = 24
    before = setup.python.read_bytes()

    assert setup.launch("background") == 24

    assert not setup.marker.exists()
    assert setup.python.read_bytes() == before
    assert not list(setup.root.glob(".hoyo-setup-constraints-*.txt"))
    output = events(capsys)
    assert output[-1]["event"] == "error"
    assert ".venv\\Scripts\\python.exe -m pip install -r requirements.txt" in output[-1]["message"]
    assert not any(event["event"] == "ready" for event in output)


def test_installer_success_does_not_fake_readiness_if_packages_are_still_missing(setup):
    setup.create_environment()
    setup.background_missing = ["streamlit>=1.40"]
    setup.keep_missing = True

    assert setup.launch("background") == 1
    assert not setup.marker.exists()


def test_cancellation_before_start_does_not_touch_the_environment(setup, capsys):
    cancel_file = setup.root / "setup cancellation.txt"
    cancel_file.touch()

    assert setup.launch(cancel_file=cancel_file) == 130

    assert setup.calls == []
    assert not setup.environment_root.exists()
    assert cancel_file.exists()
    assert events(capsys)[-1]["event"] == "error"


def test_cancellation_during_install_removes_constraints_and_releases_shared_lock(setup):
    setup.create_environment()
    setup.background_missing = ["streamlit>=1.40"]
    setup.cancel_during_install = True

    assert setup.launch("background") == 130

    assert not setup.marker.exists()
    assert not list(setup.root.glob(".hoyo-setup-constraints-*.txt"))
    with startup.installation_lock(setup.root):
        pass


def test_overlap_is_reported_without_starting_another_installer(setup, capsys):
    setup.create_environment()
    with startup.installation_lock(setup.root):
        assert setup.launch() == 1

    assert setup.calls == []
    assert "already running" in events(capsys)[-1]["message"]


def test_environment_blocks_python_auto_install_flags_and_global_pip_targets(monkeypatch):
    removed = {
        "pylauncher_allow_install", "pylauncher_always_install", "pythonhome", "pythonpath", "pip_target", "pip_prefix", "pip_user",
        "pip_ignore_installed", "pip_force_reinstall", "pip_upgrade", "pip_upgrade_strategy", "pip_dry_run",
    }
    for name in ("PYLAUNCHER_ALLOW_INSTALL", "PyLauncher_Always_Install", "PYTHONHOME", "PYTHONPATH", "PIP_TARGET", "PIP_PREFIX", "PIP_USER", "PIP_FORCE_REINSTALL", "PIP_IGNORE_INSTALLED", "PIP_UPGRADE", "PIP_UPGRADE_STRATEGY", "PIP_DRY_RUN"):
        monkeypatch.setenv(name, "enabled")

    child = startup.child_environment()

    assert child["PYTHON_MANAGER_AUTOMATIC_INSTALL"] == "false"
    assert child["PIP_REQUIRE_VIRTUALENV"] == "true"
    assert child["PIP_CONFIG_FILE"] == os.devnull
    assert not any(name.casefold() in removed for name in child)
    assert os.environ["PYLAUNCHER_ALLOW_INSTALL"] == "enabled"


def test_real_package_probe_reports_missing_ranges_and_exact_transitive_pins(tmp_path):
    prelude = """
import importlib.metadata as metadata
import json
import sys
versions = {"PySide6": "6.12.0", "jedi": "0.20.1", "Pygments": "2.21.0", "streamlit": "1.20.0", "parso": "0.8.6"}
def fake_version(name):
    if name in versions:
        return versions[name]
    raise metadata.PackageNotFoundError(name)
class FakeDistribution:
    def __init__(self, name, version):
        self.metadata = {"Name": name}
        self.version = version
metadata.version = fake_version
metadata.distributions = lambda: [FakeDistribution(name, version) for name, version in versions.items()]
"""
    requirements = [*startup.CORE_REQUIREMENTS, "streamlit>=1.40", "requests>=2.32"]
    result = subprocess.run(
        [sys.executable, "-I", "-c", prelude + startup._PACKAGE_PROBE, json.dumps(requirements)],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )

    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["needs_parser"] is False
    assert status["missing"] == ["streamlit>=1.40", "requests>=2.32"]
    assert status["core_missing"] == []
    assert "parso==0.8.6" in status["pins"]
    assert "PySide6==6.12.0" in status["pins"]


def test_real_cross_process_lock_prevents_concurrent_setup_and_releases_after_exit(tmp_path):
    source_root = Path(startup.__file__).resolve().parents[1]
    code = (
        "from pathlib import Path\n"
        "from launcher.startup import installation_lock, StartupFailure\n"
        "import sys\n"
        "try:\n"
        "    with installation_lock(Path(sys.argv[1])): print('acquired')\n"
        "except StartupFailure as error:\n"
        "    print(error); raise SystemExit(19)\n"
    )
    command = [sys.executable, "-c", code, str(tmp_path)]
    with startup.installation_lock(tmp_path):
        blocked = subprocess.run(command, cwd=source_root, capture_output=True, text=True, timeout=15)
    allowed = subprocess.run(command, cwd=source_root, capture_output=True, text=True, timeout=15)

    assert blocked.returncode == 19
    assert "already running" in blocked.stdout
    assert allowed.returncode == 0
    assert allowed.stdout.strip() == "acquired"
    assert (tmp_path / startup.LOCK_NAME).is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-tree boundary; Windows uses owned PID taskkill /T.")
def test_real_cancellation_stops_owned_worker_and_descendant_but_not_unrelated_process(tmp_path):
    cancel_path = tmp_path / "cancel requested"
    descendant_path = tmp_path / "descendant.pid"
    stdout = io.StringIO()
    log = startup.EventLog(tmp_path / startup.LOG_NAME, "background", stdout)
    cancellation = startup.Cancellation(cancel_path)
    code = (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        "print('download started', flush=True)\n"
        "time.sleep(60)\n"
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def request_cancel() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not descendant_path.exists():
            time.sleep(0.02)
        cancel_path.touch()

    thread = threading.Thread(target=request_cancel, daemon=True)
    thread.start()
    try:
        with pytest.raises(startup.SetupCancelled):
            startup._run_child(
                [sys.executable, "-u", "-c", code, str(descendant_path)],
                tmp_path, startup.child_environment(), log, cancellation, description="Downloading features",
            )
        assert unrelated.poll() is None
        descendant = int(descendant_path.read_text())
        state_file = Path(f"/proc/{descendant}/stat")
        deadline = time.monotonic() + 2
        while state_file.exists() and time.monotonic() < deadline:
            if state_file.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        assert not state_file.exists() or state_file.read_text().split()[2] == "Z"
        assert any(json.loads(line)["message"] == "download started" for line in stdout.getvalue().splitlines())
    finally:
        thread.join(timeout=1)
        unrelated.terminate()
        unrelated.wait(timeout=5)
        log.close()


def test_real_child_streams_json_log_events_and_reports_nonzero_exit(tmp_path):
    output = io.StringIO()
    log = startup.EventLog(tmp_path / startup.LOG_NAME, "core", output)
    try:
        with pytest.raises(startup.StartupFailure) as error:
            startup._run_child(
                [sys.executable, "-u", "-c", "import sys; print('package download failed', file=sys.stderr); sys.exit(23)"],
                tmp_path, startup.child_environment(), log, startup.Cancellation(), description="Preparing editor",
            )
        assert error.value.exit_code == 23
        assert any(json.loads(line)["message"] == "package download failed" for line in output.getvalue().splitlines())
        assert "package download failed" in (tmp_path / startup.LOG_NAME).read_text(encoding="utf-8")
    finally:
        log.close()
