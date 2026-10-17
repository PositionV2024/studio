from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .project_manifest import ProjectManifest


class ProjectRunner(QObject):
    """Run one Streamlit preview process without blocking the desktop UI.

    Project selection is intentionally separate from process ownership. Selecting
    another project does not synchronously terminate the current Streamlit
    server. When the user presses Run for a different project, the runner queues
    the new project, stops the old process asynchronously, and starts the queued
    project after the old process has exited.
    """

    log_received = Signal(str)
    state_changed = Signal(str)
    preview_ready = Signal(str, str)  # project path, preview URL
    failed = Signal(str)
    process_started = Signal(int, str, str)  # PID, project name, project path
    process_stopped = Signal(str, int, bool)  # project name, exit code, intentional

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.started.connect(self._on_started)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)

        self._active_project_path: Path | None = None
        self._active_manifest: ProjectManifest | None = None
        self._preview_url: str | None = None
        self._pending_start: tuple[Path, ProjectManifest] | None = None

        self._operation = "idle"
        self._intentional_stop = False
        self._stop_reason = ""
        self._error_reported = False

        self._health_timer = QTimer(self)
        self._health_timer.setInterval(450)
        self._health_timer.timeout.connect(self._check_port)
        self._health_attempts = 0
        self._max_health_attempts = 80

        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.setInterval(2500)
        self._kill_timer.timeout.connect(self._force_kill_if_needed)

        # Probe the actual Streamlit HTML asynchronously. A listening TCP port
        # can become available a moment before Streamlit's frontend is ready,
        # which previously caused QWebEngineView to cache a blank white page.
        self._network_manager = QNetworkAccessManager(self)
        self._probe_reply: QNetworkReply | None = None

    @property
    def is_running(self) -> bool:
        """Return whether any child operation is still active."""

        return self.process.state() != QProcess.ProcessState.NotRunning

    @property
    def is_preview_running(self) -> bool:
        """Return whether the active process belongs to a Streamlit preview."""

        return self.is_running and self._operation != "install"

    @property
    def running_project_path(self) -> Path | None:
        if not self.is_preview_running:
            return None
        return self._active_project_path

    @property
    def running_project_name(self) -> str | None:
        if not self.is_preview_running or self._active_manifest is None:
            return None
        return self._active_manifest.name

    @property
    def preview_url(self) -> str | None:
        return self._preview_url

    @property
    def has_pending_start(self) -> bool:
        return self._pending_start is not None

    @property
    def pending_project_path(self) -> Path | None:
        return self._pending_start[0] if self._pending_start else None

    @property
    def process_id(self) -> int:
        return int(self.process.processId()) if self.is_running else 0

    def is_project_running(self, project_path: Path | str) -> bool:
        active = self.running_project_path
        if active is None:
            return False
        try:
            return active.resolve() == Path(project_path).resolve()
        except OSError:
            return active == Path(project_path)

    @staticmethod
    def _discover_python(project_path: Path, manifest: ProjectManifest) -> Path:
        if manifest.python_executable:
            configured = Path(manifest.python_executable).expanduser()
            if not configured.is_absolute():
                configured = project_path / configured
            if configured.is_file():
                return configured

        candidates = [
            project_path / ".venv" / "Scripts" / "python.exe",
            project_path / ".venv" / "bin" / "python",
            project_path / "venv" / "Scripts" / "python.exe",
            project_path / "venv" / "bin" / "python",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate

        # When Project Studio is launched from source, its own virtual
        # environment can run Streamlit projects as a practical fallback.
        if not getattr(sys, "frozen", False):
            return Path(sys.executable)

        # A frozen Project Studio executable is not a Python interpreter.
        for command in ("py.exe", "py", "python.exe", "python"):
            discovered = shutil.which(command)
            if discovered:
                return Path(discovered)

        raise RuntimeError(
            "No Python interpreter was found for this project. Create a "
            "project .venv, configure python_executable in .studio/project.json, "
            "or install Python with the Windows py launcher."
        )

    def start_project(self, project_path: Path, manifest: ProjectManifest) -> None:
        """Start a project or switch to it without freezing the GUI."""

        project_path = project_path.expanduser().resolve()

        if self.is_preview_running and self.is_project_running(project_path):
            # The requested project is already active. Reuse its preview rather
            # than stopping and starting the same server.
            if self._preview_url:
                self.preview_ready.emit(str(project_path), self._preview_url)
            self.log_received.emit(
                f"\n{manifest.name} is already running"
                f" (PID {self.process_id}).\n"
            )
            return

        if self.is_running:
            if self._operation == "install":
                self.failed.emit(
                    "Dependency installation is still running. Wait for it to "
                    "finish before starting a project."
                )
                return

            self._pending_start = (project_path, manifest)
            active_name = self.running_project_name or "the current project"
            self.log_received.emit(
                f"\nSwitch requested: {active_name} → {manifest.name}\n"
            )
            self.state_changed.emit("switching")
            self.stop_project(reason="switch", preserve_pending=True)
            return

        self._launch_project(project_path, manifest)

    def _launch_project(
        self,
        project_path: Path,
        manifest: ProjectManifest,
    ) -> None:
        validation_errors = manifest.validate(project_path)
        if validation_errors:
            self.failed.emit("\n".join(validation_errors))
            return

        try:
            python_executable = self._discover_python(project_path, manifest)
        except RuntimeError as error:
            self.failed.emit(str(error))
            return

        args = [
            "-m",
            "streamlit",
            "run",
            manifest.entry_point,
            "--server.headless=true",
            "--server.address=127.0.0.1",
            f"--server.port={manifest.preview_port}",
            "--browser.gatherUsageStats=false",
            "--server.runOnSave=true",
        ]

        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")

        self._active_project_path = project_path
        self._active_manifest = manifest
        self._preview_url = None
        self._operation = "run"
        self._intentional_stop = False
        self._stop_reason = ""
        self._error_reported = False

        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(str(project_path))

        self.log_received.emit(
            "\n"
            + "─" * 72
            + "\n"
            + f"Starting {manifest.name}\n"
            + f"Working directory: {project_path}\n"
            + f"Command: {python_executable} {' '.join(args)}\n\n"
        )
        self.state_changed.emit("starting")
        self.process.start(str(python_executable), args)

        self._health_attempts = 0
        self._health_timer.start()

    def install_requirements(
        self,
        project_path: Path,
        manifest: ProjectManifest,
    ) -> None:
        """Install requirements.txt with the project's Python interpreter."""

        if self.is_running:
            self.failed.emit(
                "Stop the running project before installing dependencies."
            )
            return

        requirements = project_path / "requirements.txt"
        if not requirements.exists():
            self.failed.emit(f"No requirements.txt was found in {project_path}")
            return

        try:
            python_executable = self._discover_python(project_path, manifest)
        except RuntimeError as error:
            self.failed.emit(str(error))
            return

        args = ["-m", "pip", "install", "-r", str(requirements)]
        self._active_project_path = project_path
        self._active_manifest = manifest
        self._preview_url = None
        self._operation = "install"
        self._intentional_stop = False
        self._stop_reason = ""
        self._error_reported = False

        self.process.setWorkingDirectory(str(project_path))
        self.process.setProcessEnvironment(QProcessEnvironment.systemEnvironment())
        self.log_received.emit(
            "\n"
            + "─" * 72
            + "\n"
            + f"Installing dependencies for {manifest.name}\n"
            + f"Command: {python_executable} {' '.join(args)}\n\n"
        )
        self.state_changed.emit("installing")
        self.process.start(str(python_executable), args)

    def stop_project(
        self,
        *,
        reason: str = "user",
        preserve_pending: bool = False,
    ) -> None:
        """Request a graceful asynchronous stop.

        The previous implementation called waitForFinished(), which blocked the
        Qt event loop and made project selection feel frozen. This method returns
        immediately and uses a timer only as a fallback if the process ignores
        terminate().
        """

        self._health_timer.stop()
        self._cancel_preview_probe()

        if not preserve_pending:
            self._pending_start = None

        if not self.is_running:
            self.state_changed.emit("stopped")
            if self._pending_start is not None:
                QTimer.singleShot(0, self._start_pending_project)
            return

        self._intentional_stop = True
        self._stop_reason = reason
        name = self.running_project_name or (
            self._active_manifest.name if self._active_manifest else "project"
        )

        if reason == "switch":
            self.log_received.emit(f"Stopping {name} before switching…\n")
            self.state_changed.emit("switching")
        elif reason == "restart":
            self.log_received.emit(f"Restarting {name}…\n")
            self.state_changed.emit("restarting")
        else:
            self.log_received.emit(f"\nStopping {name}…\n")
            self.state_changed.emit("stopping")

        self.process.terminate()
        self._kill_timer.start()

    def restart_project(self) -> None:
        if (
            not self.is_preview_running
            or self._active_project_path is None
            or self._active_manifest is None
        ):
            return

        self._pending_start = (
            self._active_project_path,
            self._active_manifest,
        )
        self.stop_project(reason="restart", preserve_pending=True)

    def shutdown(self) -> None:
        """Stop child processes while the desktop application is closing."""

        self._health_timer.stop()
        self._kill_timer.stop()
        self._cancel_preview_probe()
        self._pending_start = None

        if not self.is_running:
            return

        self._intentional_stop = True
        self._stop_reason = "shutdown"
        self.process.terminate()
        if not self.process.waitForFinished(1000):
            self.process.kill()
            self.process.waitForFinished(1000)

    def _force_kill_if_needed(self) -> None:
        if not self.is_running:
            return
        self.log_received.emit(
            "The process did not exit after terminate(); forcing it to close.\n"
        )
        self.process.kill()

    def _start_pending_project(self) -> None:
        pending = self._pending_start
        self._pending_start = None
        if pending is None or self.is_running:
            return
        project_path, manifest = pending
        self._launch_project(project_path, manifest)

    def _cancel_preview_probe(self) -> None:
        reply = self._probe_reply
        self._probe_reply = None
        if reply is not None:
            reply.setProperty("cancelled", True)
            reply.abort()
            reply.deleteLater()

    def _check_port(self) -> None:
        """Request the real Streamlit document without blocking Qt."""

        if (
            self._active_manifest is None
            or self._active_project_path is None
            or not self.is_preview_running
        ):
            self._health_timer.stop()
            self._cancel_preview_probe()
            return

        if self._probe_reply is not None:
            return

        self._health_attempts += 1
        url = QUrl(
            f"http://127.0.0.1:{self._active_manifest.preview_port}/"
        )
        request = QNetworkRequest(url)
        if hasattr(request, "setTransferTimeout"):
            request.setTransferTimeout(900)
        request.setRawHeader(b"Cache-Control", b"no-cache")
        request.setRawHeader(b"Pragma", b"no-cache")
        reply = self._network_manager.get(request)
        reply.setProperty("project_path", str(self._active_project_path))
        reply.setProperty("preview_port", self._active_manifest.preview_port)
        self._probe_reply = reply
        reply.finished.connect(
            lambda current=reply: self._preview_probe_finished(current)
        )

    def _preview_probe_finished(self, reply: QNetworkReply) -> None:
        if self._probe_reply is reply:
            self._probe_reply = None

        if bool(reply.property("cancelled")):
            reply.deleteLater()
            return

        project_path = str(reply.property("project_path") or "")
        port = int(reply.property("preview_port") or 0)
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        body = bytes(reply.readAll())
        error = reply.error()
        reply.deleteLater()

        if (
            self._active_manifest is None
            or self._active_project_path is None
            or not self.is_preview_running
            or str(self._active_project_path) != project_path
            or self._active_manifest.preview_port != port
        ):
            return

        html_ready = (
            error == QNetworkReply.NetworkError.NoError
            and status is not None
            and 200 <= int(status) < 400
            and bool(body.strip())
            and (
                b"<html" in body.lower()
                or b"<!doctype" in body.lower()
                or b"streamlit" in body.lower()
            )
        )

        if html_ready:
            self._health_timer.stop()
            self._preview_url = f"http://127.0.0.1:{port}"
            self.preview_ready.emit(project_path, self._preview_url)
            self.log_received.emit(
                f"\nPreview ready: {self._preview_url}\n"
            )
            return

        if self._health_attempts >= self._max_health_attempts:
            self._health_timer.stop()
            self.failed.emit(
                "The Streamlit process started, but its web page did not "
                "become ready. Check the Output panel for an import or "
                "dependency error."
            )

    def _read_output(self) -> None:
        data = bytes(self.process.readAllStandardOutput()).decode(
            "utf-8",
            errors="replace",
        )
        if data:
            self.log_received.emit(data)

    def _on_started(self) -> None:
        if self._active_manifest is None or self._active_project_path is None:
            return

        pid = self.process_id
        if self._operation == "install":
            self.state_changed.emit("installing")
            self.log_received.emit(f"Dependency process started (PID {pid}).\n")
            return

        self.state_changed.emit("running")
        self.log_received.emit(
            f"Streamlit process started (PID {pid}). Waiting for preview…\n"
        )
        self.process_started.emit(
            pid,
            self._active_manifest.name,
            str(self._active_project_path),
        )

    def _on_finished(
        self,
        exit_code: int,
        _exit_status: QProcess.ExitStatus,
    ) -> None:
        self._health_timer.stop()
        self._kill_timer.stop()
        self._cancel_preview_probe()

        operation = self._operation
        intentional = self._intentional_stop
        project_name = (
            self._active_manifest.name if self._active_manifest else "Project"
        )

        self._operation = "idle"
        self._intentional_stop = False
        self._stop_reason = ""
        self._preview_url = None
        self._active_project_path = None
        self._active_manifest = None

        if operation == "install":
            if intentional:
                self.log_received.emit("\nDependency installation cancelled.\n")
            elif exit_code == 0:
                self.log_received.emit("\nDependencies installed successfully.\n")
            else:
                message = (
                    f"Dependency installation exited with code {exit_code}. "
                    "Review the Output panel for details."
                )
                self.log_received.emit(f"\nERROR: {message}\n")
                if not self._error_reported:
                    self.failed.emit(message)
        elif intentional:
            self.log_received.emit(f"{project_name} stopped.\n")
        elif exit_code == 0:
            self.log_received.emit(f"\n{project_name} finished normally.\n")
        else:
            message = (
                f"{project_name} exited unexpectedly with code {exit_code}. "
                "Review the Output panel for the traceback."
            )
            self.log_received.emit(f"\nERROR: {message}\n")
            if not self._error_reported:
                self.failed.emit(message)

        if operation != "install":
            self.process_stopped.emit(project_name, int(exit_code), intentional)
        self.state_changed.emit("stopped")

        if self._pending_start is not None:
            QTimer.singleShot(80, self._start_pending_project)

    def _on_error(self, error: QProcess.ProcessError) -> None:
        self._health_timer.stop()
        self._cancel_preview_probe()

        # QProcess can report Crashed, Timedout, or WriteError while an
        # intentionally terminated Windows process is closing. None of those
        # should be presented as an application crash.
        if self._intentional_stop:
            self.log_received.emit("Process closed by Project Studio.\n")
            return

        message = self.process.errorString() or "Unknown process error"
        self.log_received.emit(f"\nERROR: {message}\n")
        self._error_reported = True

        if error == QProcess.ProcessError.FailedToStart:
            self._kill_timer.stop()
            self._operation = "idle"
            self._preview_url = None
            self._active_project_path = None
            self._active_manifest = None
            self.state_changed.emit("stopped")

        self.failed.emit(message)
