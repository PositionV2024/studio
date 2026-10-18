"""Run the selected Python file in a separate, responsive child process.

The editor decides when to save the document. This module only runs the saved
file, using its own project's working directory and Python interpreter. It does
not share process state with Streamlit previews or execute code inside Studio.
"""

from __future__ import annotations

import codecs
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .project_manifest import load_manifest


def _is_frozen_studio(executable: Path) -> bool:
    if not getattr(sys, "frozen", False):
        return False
    try:
        return executable.resolve() == Path(sys.executable).resolve()
    except (OSError, RuntimeError):
        return str(executable) == sys.executable


def _checked_python(executable: str | Path, project_path: Path) -> Path:
    """Resolve an interpreter without resolving away a virtual environment."""

    path = Path(executable).expanduser()
    if not path.is_absolute():
        candidate = project_path / path
        # A configured bare command such as "python" may name a PATH entry;
        # paths with directories always remain relative to the original project.
        if path.name == str(path) and not candidate.is_file():
            command = shutil.which(str(path))
            path = Path(command) if command else candidate
        else:
            path = candidate
    path = Path(os.path.abspath(path))

    if _is_frozen_studio(path):
        raise RuntimeError(
            "The selected executable is HoYo Project Studio, not Python. "
            "Select a Python interpreter or create a project .venv."
        )

    console_name = {"pythonw.exe": "python.exe", "pyw.exe": "py.exe"}.get(path.name.lower())
    if console_name:
        console = path.with_name(console_name)
        if not console.is_file():
            raise RuntimeError(
                f"Python's console executable was not found: {console}\n"
                "Choose python.exe from the same environment so Script Test "
                "can display output and accept input."
            )
        path = console

    if _is_frozen_studio(path):
        raise RuntimeError(
            "HoYo Project Studio cannot be used as a Python interpreter. "
            "Select an installed Python interpreter or create a project .venv."
        )
    if not path.is_file():
        raise RuntimeError(
            f"Python interpreter was not found: {path}\n"
            "Check python_executable in this project's .studio/project.json "
            "or restore the selected Python environment."
        )
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise RuntimeError(f"Python interpreter cannot be executed: {path}\nCheck its file permissions.")
    return path


def discover_script_python(
    project_path: str | Path,
    python_executable: str | Path | None = None,
) -> Path:
    """Find Python, while treating an explicit configuration as authoritative.

    An invalid configured interpreter is an error. Falling back to a different
    environment would run the user's script with different packages/settings.
    Keep a venv's executable symlink intact; resolving it changes sys.prefix.
    """

    project_path = Path(project_path).expanduser().resolve()
    if python_executable and str(python_executable).strip():
        return _checked_python(python_executable, project_path)

    manifest = load_manifest(project_path)
    if manifest is not None and manifest.python_executable.strip():
        return _checked_python(manifest.python_executable, project_path)

    for folder in (".venv", "venv"):
        for relative in ("Scripts/python.exe", "Scripts/pythonw.exe", "bin/python"):
            candidate = project_path / folder / relative
            if candidate.is_file():
                return _checked_python(candidate, project_path)

    if not getattr(sys, "frozen", False) and sys.executable:
        return _checked_python(sys.executable, project_path)

    for command in ("py.exe", "py", "python.exe", "python", "python3"):
        discovered = shutil.which(command)
        if discovered and not _is_frozen_studio(Path(discovered)):
            return _checked_python(discovered, project_path)

    raise RuntimeError(
        "No Python interpreter was found for this project. Create a project "
        ".venv, set python_executable in .studio/project.json, or install Python."
    )


class ScriptRunner(QObject):
    """Own one asynchronous script run, independent of the preview runner.

    ``start`` returns whether a launch was accepted, not whether the script
    succeeded. Each accepted launch emits ``finished`` exactly once, including
    a launch failure (exit code -1). Rejected requests emit ``failed`` without
    emitting ``finished`` or replacing an active run's context.

    ``log_received`` preserves merged stdout/stderr text, including prompts
    without newlines. ``write_input`` writes raw text; callers append a newline
    when sending a line. The UI supplies the final Finished/Stopped footer.
    """

    log_received = Signal(str)
    state_changed = Signal(str)
    started = Signal(str, int)  # script path, PID
    finished = Signal(str, int, bool)  # script path, exit code, intentional stop
    failed = Signal(str)

    STOP_TIMEOUT_MS = 1500
    SHUTDOWN_GRACE_MS = 1000
    SHUTDOWN_KILL_MS = 1000

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self._kill_timer: QTimer | None = None
        self._run_id = 0
        self._active = False
        self._closed = False
        self._state = "idle"
        self._script_path: Path | None = None
        self._project_path: Path | None = None
        self._python_executable: Path | None = None
        self._intentional_stop = False
        self._input_open = False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._process_error = ""

    @property
    def is_running(self) -> bool:
        # Include accepted launches still in QProcess.Starting and terminal
        # events waiting to be handled, so a second run cannot slip between them.
        return self._active

    @property
    def state(self) -> str:
        return self._state

    @property
    def script_path(self) -> Path | None:
        return self._script_path

    @property
    def project_path(self) -> Path | None:
        return self._project_path

    @property
    def python_executable(self) -> Path | None:
        return self._python_executable

    @property
    def process_id(self) -> int:
        return int(self._process.processId()) if self._active and self._process is not None else 0

    def _set_state(self, state: str) -> None:
        if self._state != state:
            self._state = state
            self.state_changed.emit(state)

    def _report_error(self, message: str) -> None:
        self.log_received.emit(f"\nError: {message}\n")
        self.failed.emit(message)

    def start(
        self,
        script_path: str | Path,
        project_path: str | Path,
        python_executable: str | Path | None = None,
    ) -> bool:
        if self._closed:
            self._report_error("Script Test is shutting down. Reopen Project Studio to run another script.")
            return False
        if self._active:
            self._report_error("A script test is already running. Stop it before testing another script.")
            return False

        try:
            project = Path(project_path).expanduser().resolve()
            if not project.is_dir():
                raise RuntimeError(f"The script's project folder no longer exists: {project}")
            script = Path(script_path).expanduser()
            if not script.is_absolute():
                script = project / script
            script = script.resolve()
            if not script.is_file():
                raise RuntimeError(f"The selected script was not found: {script}\nSave or restore the file before testing it.")
            interpreter = discover_script_python(project, python_executable)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self._set_state("failed")
            self._report_error(str(error))
            return False

        # Use a new QProcess per run. Old queued signals and stop timers cannot
        # refer to a process that has since been reused for a different script.
        if self._kill_timer is not None:
            self._kill_timer.stop()
        if self._process is not None:
            self._process.deleteLater()

        self._run_id += 1
        run_id = self._run_id
        process = QProcess(self)
        self._process = process
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setWorkingDirectory(str(project))
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(environment)

        process.readyReadStandardOutput.connect(lambda: self._read_output(process, run_id))
        process.started.connect(lambda: self._on_started(process, run_id))
        process.finished.connect(lambda code, status: self._finish_run(process, run_id, code, status))
        process.errorOccurred.connect(lambda error: self._on_error(process, run_id, error))

        timer = QTimer(process)
        timer.setSingleShot(True)
        timer.setInterval(self.STOP_TIMEOUT_MS)
        timer.timeout.connect(lambda: self._force_kill_if_needed(process, run_id))
        self._kill_timer = timer

        self._script_path = script
        self._project_path = project
        self._python_executable = interpreter
        self._active = True
        self._intentional_stop = False
        self._input_open = True
        self._process_error = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

        self.log_received.emit(
            f"Testing: {script}\nPython: {interpreter}\nWorking directory: {project}\n\n"
        )
        self._set_state("starting")
        # A close handler connected to state_changed may have already cancelled
        # this accepted run before QProcess.start was reached.
        if self._active and not self._closed:
            process.start(str(interpreter), ["-u", str(script)])
        return True

    def _is_current(self, process: QProcess, run_id: int) -> bool:
        return self._active and self._process is process and self._run_id == run_id

    def _on_started(self, process: QProcess, run_id: int) -> None:
        if not self._is_current(process, run_id):
            return
        self.started.emit(str(self._script_path), int(process.processId()))
        if not self._is_current(process, run_id):
            return
        if self._intentional_stop:
            # terminate() may have been requested while QProcess was Starting.
            process.terminate()
        else:
            self._set_state("running")

    def _read_output(self, process: QProcess, run_id: int) -> None:
        if not self._is_current(process, run_id):
            return
        data = bytes(process.readAllStandardOutput())
        if data:
            text = self._decoder.decode(data)
            if text:
                self.log_received.emit(text)

    def _on_error(self, process: QProcess, run_id: int, error: QProcess.ProcessError) -> None:
        if not self._is_current(process, run_id):
            return
        if error == QProcess.ProcessError.FailedToStart:
            message = (
                f"Could not start Python: {self._python_executable}\n"
                f"{process.errorString()}\n"
                "Check that this project's selected Python environment is installed and usable."
            )
            self._finish_run(process, run_id, -1, QProcess.ExitStatus.CrashExit, start_error=message)
        elif not self._intentional_stop:
            # Crashed is followed by finished; defer the terminal report so a
            # traceback/output is drained first and the run finishes only once.
            self._process_error = process.errorString()
            if error in (QProcess.ProcessError.ReadError, QProcess.ProcessError.WriteError):
                self.log_received.emit(f"\nScript input/output error: {self._process_error}\n")

    def _finish_run(
        self,
        process: QProcess,
        run_id: int,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
        *,
        start_error: str = "",
    ) -> None:
        if not self._is_current(process, run_id):
            return
        self._read_output(process, run_id)
        trailing = self._decoder.decode(b"", final=True)
        if trailing:
            self.log_received.emit(trailing)

        path = str(self._script_path)
        stopped = self._intentional_stop
        if self._kill_timer is not None:
            self._kill_timer.stop()
        self._input_open = False
        self._active = False

        error = ""
        if stopped:
            state = "stopped"
        elif start_error:
            state = "failed"
            error = start_error
        elif exit_status == QProcess.ExitStatus.CrashExit:
            state = "failed"
            detail = f" ({self._process_error})" if self._process_error else ""
            error = f"The script process stopped unexpectedly{detail}. Exit code: {exit_code}."
        elif exit_code != 0:
            state = "failed"
            error = f"The script exited with code {exit_code}. Review the Script Test output for details."
        else:
            state = "finished"

        self._set_state(state)
        if error:
            self._report_error(error)
        self.finished.emit(path, int(exit_code), stopped)

    def write_input(self, text: str) -> bool:
        process = self._process
        if (
            not self._active or self._intentional_stop or not self._input_open
            or process is None or process.state() != QProcess.ProcessState.Running
        ):
            return False
        data = text.encode("utf-8")
        return process.write(data) == len(data)

    def close_input(self) -> bool:
        process = self._process
        if (
            not self._active or self._intentional_stop or not self._input_open
            or process is None or process.state() != QProcess.ProcessState.Running
        ):
            return False
        self._input_open = False
        process.closeWriteChannel()
        return True

    def stop(self) -> None:
        if not self._active or self._intentional_stop or self._process is None:
            return
        self._intentional_stop = True
        self._input_open = False
        self.log_received.emit("\nStopping script…\n")
        self._set_state("stopping")
        self._process.terminate()
        if self._kill_timer is not None:
            self._kill_timer.start()

    def _force_kill_if_needed(self, process: QProcess, run_id: int) -> None:
        if self._is_current(process, run_id) and self._intentional_stop:
            self.log_received.emit("The script did not stop in time; closing its process.\n")
            process.kill()

    def shutdown(self) -> None:
        """Bounded final cleanup; normal Stop never blocks the Qt event loop."""

        self._closed = True
        if not self._active or self._process is None:
            if self._kill_timer is not None:
                self._kill_timer.stop()
            return

        process, run_id = self._process, self._run_id
        self.stop()
        if self._kill_timer is not None:
            self._kill_timer.stop()
        if process.state() != QProcess.ProcessState.NotRunning:
            if not process.waitForFinished(self.SHUTDOWN_GRACE_MS):
                process.kill()
                process.waitForFinished(self.SHUTDOWN_KILL_MS)
        if self._is_current(process, run_id) and process.state() == QProcess.ProcessState.NotRunning:
            self._finish_run(process, run_id, process.exitCode(), process.exitStatus())
