"""The repair check must find real suggestions and expose missing dependencies."""

from pathlib import Path
import sys
import time
import venv

from launcher.check_python_completion import _run_command, check_completion


def test_diagnostic_runs_real_builtin_and_module_completion():
    result = check_completion(sys.executable)
    assert result["ok"], result
    assert len(result["checks"]) == 2
    assert all(check["ok"] for check in result["checks"])


def test_diagnostic_does_not_borrow_jedi_from_the_application_environment(tmp_path):
    # A clean venv models the old Studio installation before the helper's
    # dependencies have been installed. No package download is needed.
    root = tmp_path / "old-studio-environment"
    venv.EnvBuilder(with_pip=False).create(root)
    python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    assert Path(python).is_file()
    result = check_completion(str(python))
    assert not result["ok"]
    assert "ModuleNotFoundError" in result["error"]
    assert "jedi" in result["error"]


def test_diagnostic_subprocess_deadline_stops_a_stalled_check():
    started = time.monotonic()
    result = _run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
    assert not result["ok"]
    assert "Timed out" in result["output"]
    assert time.monotonic() - started < 5
