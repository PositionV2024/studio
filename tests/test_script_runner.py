"""Run small, generated scripts through the real asynchronous Qt process API."""

import json
import os
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from studio.project_manifest import ProjectManifest, save_manifest
from studio.script_runner import ScriptRunner, discover_script_python


@pytest.fixture(scope="module")
def script_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def script_runner(script_app):
    runner = ScriptRunner()
    runner.output = []
    runner.states = []
    runner.starts = []
    runner.results = []
    runner.errors = []
    runner.log_received.connect(runner.output.append)
    runner.state_changed.connect(runner.states.append)
    runner.started.connect(lambda path, pid: runner.starts.append((path, pid)))
    runner.finished.connect(lambda path, code, stopped: runner.results.append((path, code, stopped)))
    runner.failed.connect(runner.errors.append)
    yield runner
    runner.shutdown()
    runner.deleteLater()
    script_app.processEvents()


def wait_for(app, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "Timed out waiting for the script process"


def write_script(root, source, name="sample.py"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def output(runner):
    return "".join(runner.output)


def test_success_streams_stdout_stderr_and_split_utf8(script_app, script_runner, tmp_path):
    path = write_script(tmp_path, """
import os
import sys
import time
print('running before completion')
data = '€你好'.encode('utf-8')
os.write(1, data[:1])
time.sleep(0.15)
os.write(1, data[1:] + b'\\n')
print('standard error text', file=sys.stderr)
""")
    assert script_runner.start(path, tmp_path, sys.executable)
    assert script_runner.is_running
    wait_for(script_app, lambda: "running before completion" in output(script_runner))
    assert script_runner.is_running, "Output must be delivered while the child is still running"
    wait_for(script_app, lambda: bool(script_runner.results))
    text = output(script_runner)
    assert "€你好\nstandard error text\n" in text
    assert "�" not in text
    assert script_runner.results == [(str(path), 0, False)]
    assert script_runner.states == ["starting", "running", "finished"]
    assert not script_runner.errors
    assert not script_runner.is_running and script_runner.process_id == 0


def test_original_project_cwd_script_imports_and_unicode_paths(script_app, script_runner, tmp_path):
    project = tmp_path / "project space 中文"
    project.mkdir()
    (project / "data.txt").write_text("project data", encoding="utf-8")
    script = write_script(project, """
import json
import os
import sys
from pathlib import Path
import local_helper
print('RESULT:' + json.dumps({
    'cwd': os.getcwd(),
    'file': __file__,
    'python': sys.executable,
    'data': Path('data.txt').read_text(encoding='utf-8'),
    'helper': local_helper.VALUE,
}, ensure_ascii=False))
""", "sub folder/测试 $ script.py")
    write_script(project, "VALUE = 'imported nearby helper'\n", "sub folder/local_helper.py")
    assert script_runner.start(script, project, sys.executable)
    wait_for(script_app, lambda: bool(script_runner.results))
    assert script_runner.results == [(str(script), 0, False)]
    result = json.loads(next(line[7:] for line in output(script_runner).splitlines() if line.startswith("RESULT:")))
    assert result == {
        "cwd": str(project), "file": str(script), "python": sys.executable,
        "data": "project data", "helper": "imported nearby helper",
    }
    assert script_runner.script_path == script
    assert script_runner.project_path == project
    assert script_runner.starts[0][0] == str(script) and script_runner.starts[0][1] > 0


def test_runtime_exception_reports_traceback_and_nonzero_exit(script_app, script_runner, tmp_path):
    script = write_script(tmp_path, "print('before error')\nraise ValueError('generated test exception')\n")
    assert script_runner.start(script, tmp_path, sys.executable)
    wait_for(script_app, lambda: bool(script_runner.results))
    assert script_runner.results == [(str(script), 1, False)]
    assert script_runner.state == "failed"
    assert len(script_runner.errors) == 1
    assert "Traceback (most recent call last)" in output(script_runner)
    assert "ValueError: generated test exception" in output(script_runner)
    assert str(script) in output(script_runner)


def test_prompt_line_input_and_explicit_eof(script_app, script_runner, tmp_path):
    script = write_script(tmp_path, """
import sys
name = input('Your name: ')
print('Hello ' + name)
print('Remaining: ' + sys.stdin.read())
""")
    assert not script_runner.write_input("too early\n")
    assert not script_runner.close_input()
    assert script_runner.start(script, tmp_path, sys.executable)
    wait_for(script_app, lambda: "Your name: " in output(script_runner))
    assert script_runner.write_input("Clarence 你好\n")
    wait_for(script_app, lambda: "Hello Clarence 你好" in output(script_runner))
    assert script_runner.write_input("last input")
    assert script_runner.close_input()
    assert not script_runner.close_input()
    assert not script_runner.write_input("after eof\n")
    wait_for(script_app, lambda: bool(script_runner.results))
    assert "Remaining: last input" in output(script_runner)
    assert script_runner.results == [(str(script), 0, False)]
    assert not script_runner.errors


def test_second_start_does_not_replace_active_script(script_app, script_runner, tmp_path):
    first = write_script(tmp_path, "input('waiting: ')\n", "first.py")
    second = write_script(tmp_path, "print('must not run')\n", "second.py")
    assert script_runner.start(first, tmp_path, sys.executable)
    wait_for(script_app, lambda: "waiting: " in output(script_runner))
    pid = script_runner.process_id
    assert not script_runner.start(second, tmp_path, sys.executable)
    assert script_runner.script_path == first
    assert script_runner.process_id == pid and script_runner.is_running
    assert script_runner.state == "running" and not script_runner.results
    assert script_runner.write_input("finish\n")
    wait_for(script_app, lambda: bool(script_runner.results))
    assert script_runner.results == [(str(first), 0, False)]
    assert "must not run" not in output(script_runner)


def test_stop_is_asynchronous_and_intentional(script_app, script_runner, tmp_path):
    script = write_script(tmp_path, "import time\nprint('ready')\ntime.sleep(30)\n")
    assert script_runner.start(script, tmp_path, sys.executable)
    wait_for(script_app, lambda: "ready\n" in output(script_runner))
    before = time.monotonic()
    script_runner.stop()
    assert time.monotonic() - before < 0.25
    assert script_runner.state == "stopping"
    wait_for(script_app, lambda: bool(script_runner.results))
    assert script_runner.results[0][0] == str(script)
    assert script_runner.results[0][2] is True
    assert script_runner.state == "stopped"
    assert not script_runner.errors and not script_runner.is_running


def test_stop_immediately_after_accepted_start(script_app, script_runner, tmp_path):
    script = write_script(tmp_path, "import time\ntime.sleep(30)\n")
    assert script_runner.start(script, tmp_path, sys.executable)
    # Do not pump Qt events first: the child is still starting on most systems.
    script_runner.stop()
    script_runner.stop()
    wait_for(script_app, lambda: bool(script_runner.results))
    assert len(script_runner.results) == 1
    assert script_runner.results[0][0] == str(script) and script_runner.results[0][2]
    assert script_runner.state == "stopped"
    assert not script_runner.errors


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal handling used to simulate ignored terminate")
def test_stop_escalates_if_script_ignores_terminate(script_app, script_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(ScriptRunner, "STOP_TIMEOUT_MS", 80)
    script = write_script(tmp_path, """
import signal
import time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('signal handler ready')
while True:
    time.sleep(1)
""")
    assert script_runner.start(script, tmp_path, sys.executable)
    wait_for(script_app, lambda: "signal handler ready" in output(script_runner))
    script_runner.stop()
    wait_for(script_app, lambda: bool(script_runner.results))
    assert "did not stop in time" in output(script_runner)
    assert script_runner.results[0][2]
    assert not script_runner.errors


def test_previous_stop_timer_cannot_kill_next_run(script_app, script_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(ScriptRunner, "STOP_TIMEOUT_MS", 100)
    first = write_script(tmp_path, "import time\nprint('first ready')\ntime.sleep(30)\n", "first.py")
    second = write_script(tmp_path, "import time\ntime.sleep(0.3)\nprint('second survived')\n", "second.py")
    assert script_runner.start(first, tmp_path, sys.executable)
    wait_for(script_app, lambda: "first ready" in output(script_runner))
    script_runner.stop()
    wait_for(script_app, lambda: len(script_runner.results) == 1)
    assert script_runner.start(second, tmp_path, sys.executable)
    wait_for(script_app, lambda: len(script_runner.results) == 2)
    assert script_runner.results[0][0] == str(first) and script_runner.results[0][2]
    assert script_runner.results[1] == (str(second), 0, False)
    assert "second survived" in output(script_runner)
    assert not script_runner.errors


def test_shutdown_closes_active_child_and_prevents_relaunch(script_app, script_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(ScriptRunner, "SHUTDOWN_GRACE_MS", 80)
    script = write_script(tmp_path, """
import os
import signal
import time
if os.name != 'nt':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('shutdown ready')
while True:
    time.sleep(1)
""")
    assert script_runner.start(script, tmp_path, sys.executable)
    wait_for(script_app, lambda: "shutdown ready" in output(script_runner))
    pid = script_runner.process_id
    before = time.monotonic()
    script_runner.shutdown()
    assert time.monotonic() - before < 2
    assert not script_runner.is_running and script_runner.process_id == 0
    assert len(script_runner.results) == 1 and script_runner.results[0][2]
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not script_runner.start(script, tmp_path, sys.executable)
    assert len(script_runner.results) == 1


@pytest.mark.parametrize("missing", ["script", "project", "interpreter"])
def test_missing_inputs_fail_without_launch(script_runner, tmp_path, missing):
    script = write_script(tmp_path, "raise AssertionError('must not execute')\n")
    project = tmp_path
    interpreter = sys.executable
    if missing == "script":
        script = tmp_path / "missing.py"
    elif missing == "project":
        project = tmp_path / "missing project"
    else:
        interpreter = str(tmp_path / "missing python.exe")
    assert not script_runner.start(script, project, interpreter)
    assert len(script_runner.errors) == 1
    assert "not found" in script_runner.errors[0] or "no longer exists" in script_runner.errors[0]
    assert script_runner.errors[0] in output(script_runner)
    assert script_runner.state == "failed"
    assert not script_runner.starts and not script_runner.results


def test_invalid_manifest_interpreter_never_falls_back(script_runner, tmp_path):
    script = write_script(tmp_path, "raise AssertionError('must not execute')\n")
    save_manifest(tmp_path, ProjectManifest("test", python_executable="missing-venv/python.exe"))
    assert not script_runner.start(script, tmp_path)
    assert "missing-venv" in script_runner.errors[0]
    assert not script_runner.starts


def test_explicit_interpreter_takes_precedence_over_manifest(script_app, script_runner, tmp_path):
    script = write_script(tmp_path, "print('explicit environment')\n")
    save_manifest(tmp_path, ProjectManifest("test", python_executable="missing/python.exe"))
    assert script_runner.start(script, tmp_path, sys.executable)
    wait_for(script_app, lambda: bool(script_runner.results))
    assert script_runner.results == [(str(script), 0, False)]
    assert "explicit environment" in output(script_runner)


def test_os_launch_failure_finishes_once_without_fallback(script_app, script_runner, tmp_path):
    script = write_script(tmp_path, "raise AssertionError('must not execute')\n")
    invalid_python = tmp_path / "broken python.exe"
    invalid_python.write_bytes(b"not an executable\n")
    invalid_python.chmod(0o755)
    assert script_runner.start(script, tmp_path, invalid_python)
    wait_for(script_app, lambda: bool(script_runner.results))
    assert script_runner.results == [(str(script), -1, False)]
    assert len(script_runner.errors) == 1
    assert "Could not start Python" in script_runner.errors[0]
    assert str(invalid_python) in script_runner.errors[0]
    assert not script_runner.starts and not script_runner.is_running
    # A later valid run must not receive the failed process's terminal signal.
    assert script_runner.start(write_script(tmp_path, "print('recovered')\n"), tmp_path, sys.executable)
    wait_for(script_app, lambda: len(script_runner.results) == 2)
    assert script_runner.results[1] == (str(script), 0, False)


def test_pythonw_uses_console_twin_in_same_environment(tmp_path):
    scripts = tmp_path / "env with spaces" / "Scripts"
    scripts.mkdir(parents=True)
    windowed = scripts / "PYTHONW.EXE"
    console = scripts / "python.exe"
    windowed.touch()
    console.touch()
    windowed.chmod(0o755)
    console.chmod(0o755)
    assert discover_script_python(tmp_path, windowed) == console
    assert discover_script_python(tmp_path, windowed.relative_to(tmp_path)) == console
    console.unlink()
    with pytest.raises(RuntimeError, match="console executable was not found"):
        discover_script_python(tmp_path, windowed)


@pytest.mark.skipif(os.name == "nt", reason="The venv discovery path here models POSIX symlinks")
def test_virtual_environment_executable_symlink_is_preserved(tmp_path):
    executable = tmp_path / ".venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    assert discover_script_python(tmp_path) == executable
    assert discover_script_python(tmp_path, ".venv/bin/python") == executable
    assert executable.resolve() != executable


def test_frozen_application_is_never_used_as_python(script_runner, tmp_path, monkeypatch):
    script = write_script(tmp_path, "raise AssertionError('must not execute')\n")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("studio.script_runner.shutil.which", lambda _command: sys.executable)
    assert not script_runner.start(script, tmp_path, sys.executable)
    assert "HoYo Project Studio" in script_runner.errors[-1]
    assert not script_runner.start(script, tmp_path)
    assert "No Python interpreter" in script_runner.errors[-1]
    assert not script_runner.starts and not script_runner.results
