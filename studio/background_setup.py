"""Finish source-installation packages while the editor remains responsive."""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)


class BackgroundSetupController(QObject):
    """Own one setup worker; readiness requires its receipt and successful exit."""

    state_changed = Signal(str)
    log_received = Signal(str)
    finished = Signal(bool)

    def __init__(self, root: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.root = Path(root).absolute()
        self.state = "idle"
        self.last_error = ""
        self.log_lines: deque[str] = deque(maxlen=2500)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._process_finished)
        self._buffer = b""
        self._received_ready = False
        self._run_id = 0
        self._cancel_directory: tempfile.TemporaryDirectory | None = None
        self._cancel_path: Path | None = None
        self._kill_process: QProcess | None = None
        self._cancel_timer = QTimer(self)
        self._cancel_timer.setSingleShot(True)
        self._cancel_timer.setInterval(10_000)
        self._cancel_timer.timeout.connect(self._force_stop)

    @property
    def is_active(self) -> bool:
        return self.state in {"checking", "running", "cancelling"}

    @property
    def blocks_runtime(self) -> bool:
        return self.state not in {"idle", "ready"}

    def _set_state(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(state)

    def _log(self, text: str) -> None:
        text = text.rstrip("\r\n")
        if text:
            self.log_lines.append(text)
            self.log_received.emit(text)

    def _command(self, cancel_path: Path) -> tuple[str, list[str]]:
        python = self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        return str(python), [
            "-I", "-X", "utf8", str(self.root / "launcher" / "startup.py"),
            "--project-root", str(self.root), "--phase", "background",
            "--cancel-file", str(cancel_path),
        ]

    def start(self) -> bool:
        if self.is_active:
            return False
        self._cleanup_cancel_file()
        self._run_id += 1
        self._buffer = b""
        self._received_ready = False
        self.last_error = ""
        self._set_state("checking")
        try:
            self._cancel_directory = tempfile.TemporaryDirectory(prefix="hoyo-setup-")
            self._cancel_path = Path(self._cancel_directory.name) / "cancel"
            program, arguments = self._command(self._cancel_path)
            environment = QProcessEnvironment.systemEnvironment()
            for key in environment.keys():
                if key.casefold() in {
                    "pythonhome", "pythonpath", "pylauncher_allow_install", "pylauncher_always_install",
                }:
                    environment.remove(key)
            environment.insert("PYTHON_MANAGER_AUTOMATIC_INSTALL", "false")
            environment.insert("PYTHONIOENCODING", "utf-8")
            environment.insert("PIP_NO_INPUT", "1")
            environment.insert("PIP_DISABLE_PIP_VERSION_CHECK", "1")
            self.process.setProcessEnvironment(environment)
            self.process.setWorkingDirectory(str(self.root))
            self.process.setProgram(program)
            self.process.setArguments(arguments)
            self._log("Checking Preview support. File editing remains available.")
            self.process.start()
            self.process.closeWriteChannel()
            return True
        except (OSError, ValueError) as error:
            self.last_error = str(error)
            self._complete(False)
            return False

    def _read_stdout(self) -> None:
        self._buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._consume_line(line)
        if len(self._buffer) > 1_000_000:
            self._log(self._buffer[:4096].decode("utf-8", errors="replace"))
            self._buffer = b""

    def _consume_line(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except ValueError:
            self._log(text)
            return
        if not isinstance(event, dict) or event.get("phase") != "background":
            self._log(text)
            return
        message = event.get("message", "")
        if isinstance(message, str):
            self._log(message)
        kind = event.get("event")
        if kind == "ready":
            self._received_ready = True
        elif kind == "error":
            self.last_error = message if isinstance(message, str) else "Setup could not finish."
        elif kind == "status" and self.state == "checking":
            self._set_state("running")

    def _read_stderr(self) -> None:
        text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        self._log(text)

    def _process_error(self, error: QProcess.ProcessError) -> None:
        self.last_error = self.process.errorString()
        self._log(self.last_error)
        if error == QProcess.ProcessError.FailedToStart and self.is_active:
            self._complete(False)

    def _process_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._read_stdout()
        self._read_stderr()
        if self._buffer:
            self._consume_line(self._buffer)
            self._buffer = b""
        if not self.is_active:
            return
        success = (
            self.state != "cancelling"
            and code == 0 and status == QProcess.ExitStatus.NormalExit
            and self._received_ready and not self.last_error
        )
        if not success and not self.last_error and self.state != "cancelling":
            self.last_error = (
                "Setup exited before confirming that Preview support is ready."
                if code == 0 else f"Setup could not finish (exit code {code})."
            )
        self._complete(success)

    def _complete(self, success: bool) -> None:
        cancelled = self.state == "cancelling"
        self._cancel_timer.stop()
        self._cleanup_cancel_file()
        if success:
            self._log("Preview support is ready.")
            self._set_state("ready")
        elif cancelled:
            self._log("Setup stopped. It can check and continue on the next launch.")
            self._set_state("cancelled")
        else:
            self._log(self.last_error or "Setup could not finish. Choose Retry to try again.")
            self._set_state("failed")
        self.finished.emit(success)

    def cancel(self) -> bool:
        if not self.is_active:
            return False
        if self.state == "cancelling":
            return True
        self._set_state("cancelling")
        self._log("Stopping setup safely...")
        try:
            assert self._cancel_path is not None
            self._cancel_path.write_text("cancel\n", encoding="utf-8")
        except (OSError, AssertionError) as error:
            self._log(f"The cancellation request could not be written: {error}")
            self._force_stop()
        else:
            self._cancel_timer.start()
        return True

    def _force_stop(self) -> None:
        if self.state != "cancelling" or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        if os.name == "nt":
            # The worker normally stops its own child tree cooperatively. This
            # bounded fallback targets only our still-running worker's tree.
            pid = self.process.processId()
            if pid:
                run_id = self._run_id
                self._kill_process = QProcess(self)
                taskkill = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe")
                self._kill_process.finished.connect(lambda *_: self._kill_finished(run_id, pid))
                self._kill_process.errorOccurred.connect(lambda *_: self._kill_finished(run_id, pid))
                self._kill_process.start(taskkill, ["/PID", str(pid), "/T", "/F"])
                return
        self.process.kill()

    def _kill_finished(self, run_id: int, pid: int) -> None:
        if (
            self._run_id == run_id and self.process.processId() == pid
            and self.process.state() != QProcess.ProcessState.NotRunning
        ):
            self.process.kill()

    def _cleanup_cancel_file(self) -> None:
        if self._cancel_directory is not None:
            try:
                self._cancel_directory.cleanup()
            except OSError:
                pass
        self._cancel_directory = None
        self._cancel_path = None


class StartupBanner(QFrame):
    """A compact status area that leaves the entire editor available."""

    def __init__(self, controller: BackgroundSetupController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setObjectName("StartupBanner")
        self.setStyleSheet("""
            QFrame#StartupBanner { background: #142b40; border-bottom: 1px solid #35566f; }
            QLabel#StartupMessage { color: #e1f0ff; background: transparent; border: none; }
            QProgressBar { border: 1px solid #36566e; border-radius: 3px; background: #102031; max-height: 8px; }
            QProgressBar::chunk { background: #65b6ff; border-radius: 2px; }
        """)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 11, 16, 11)
        row.setSpacing(12)
        self.message_label = QLabel()
        self.message_label.setObjectName("StartupMessage")
        self.message_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(110)
        self.progress_bar.setAccessibleName("Preview setup progress")
        self.retry_button = QPushButton("Retry")
        self.retry_button.setAccessibleName("Retry Preview setup")
        self.retry_button.clicked.connect(controller.start)
        self.details_button = QPushButton("Details")
        self.details_button.clicked.connect(self.show_details)
        row.addWidget(self.message_label, 1)
        row.addWidget(self.progress_bar)
        row.addWidget(self.retry_button)
        row.addWidget(self.details_button)
        self._details: QDialog | None = None
        self._log_view: QPlainTextEdit | None = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(6000)
        self._hide_timer.timeout.connect(self.hide)
        controller.state_changed.connect(self._update)
        controller.log_received.connect(self._append_details)
        self._update(controller.state)

    def _update(self, state: str) -> None:
        self._hide_timer.stop()
        messages = {
            "idle": "",
            "checking": "Preparing Preview support. You can continue editing files.",
            "running": "Preparing Preview support. You can continue editing files.",
            "cancelling": "Stopping setup safely...",
            "ready": "Preview support is ready. You can now run your project.",
            "failed": "Preview setup could not finish. Your files and editor are still available.",
            "cancelled": "Preview setup was stopped. Choose Retry when you are ready.",
        }
        self.message_label.setText(messages.get(state, "Preparing Preview support..."))
        self.progress_bar.setVisible(state in {"checking", "running", "cancelling"})
        self.retry_button.setVisible(state in {"failed", "cancelled"})
        self.retry_button.setEnabled(not self.controller.is_active)
        self.setVisible(state != "idle")
        if state == "ready":
            self._hide_timer.start()

    def show_details(self) -> None:
        if self._details is None:
            self._details = QDialog(self)
            self._details.setWindowTitle("Preview setup details")
            self._details.resize(760, 420)
            layout = QVBoxLayout(self._details)
            self._log_view = QPlainTextEdit()
            self._log_view.setReadOnly(True)
            self._log_view.setMaximumBlockCount(2500)
            layout.addWidget(self._log_view)
            close = QPushButton("Close")
            close.clicked.connect(self._details.hide)
            layout.addWidget(close)
        assert self._log_view is not None
        self._log_view.setPlainText("\n".join(self.controller.log_lines))
        self._details.show()
        self._details.raise_()
        self._details.activateWindow()

    def _append_details(self, text: str) -> None:
        if self._log_view is not None:
            self._log_view.appendPlainText(text)
