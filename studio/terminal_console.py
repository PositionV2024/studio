from __future__ import annotations

import locale
import os
import shlex
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, Signal
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class CommandLineEdit(QLineEdit):
    """Single-line command entry with shell-like history navigation."""

    history_previous = Signal()
    history_next = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Up:
            self.history_previous.emit()
            return
        if event.key() == Qt.Key.Key_Down:
            self.history_next.emit()
            return
        super().keyPressEvent(event)


class TerminalConsole(QWidget):
    """A lightweight command-line console embedded in Project Studio.

    The console runs the user's normal system shell through QProcess. It is not
    a full terminal emulator, but it supports everyday project commands such as
    ``dir``, ``git status``, ``python --version``, ``pip list``, ``tasklist`` and
    Streamlit diagnostics without opening an external Command Prompt window.
    """

    message_emitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TerminalConsole")

        self.output = QPlainTextEdit()
        self.output.setObjectName("ConsoleOutput")
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(8000)
        self.output.setPlaceholderText(
            "The integrated command-line console will appear here."
        )
        self.output.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.output.customContextMenuRequested.connect(self._show_context_menu)

        self.path_label = QLabel("No project selected")
        self.path_label.setObjectName("ConsolePathLabel")
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.processes_button = QPushButton("Processes")
        self.processes_button.setObjectName("ConsoleToolbarButton")
        self.processes_button.setToolTip(
            "List running operating-system processes in the console"
        )
        self.processes_button.clicked.connect(self.show_processes)

        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("ConsoleToolbarButton")
        self.clear_button.clicked.connect(self.output.clear)

        self.restart_button = QPushButton("Restart Shell")
        self.restart_button.setObjectName("ConsoleToolbarButton")
        self.restart_button.clicked.connect(self.restart_shell)

        header = QHBoxLayout()
        header.setContentsMargins(4, 4, 4, 0)
        header.setSpacing(6)
        header.addWidget(self.path_label, 1)
        header.addWidget(self.processes_button)
        header.addWidget(self.clear_button)
        header.addWidget(self.restart_button)

        self.prompt_label = QLabel(">")
        self.prompt_label.setObjectName("ConsolePrompt")

        self.command_input = CommandLineEdit()
        self.command_input.setObjectName("ConsoleCommandInput")
        self.command_input.setPlaceholderText(
            "Enter a command, for example: git status"
        )
        self.command_input.returnPressed.connect(self._submit_command)
        self.command_input.history_previous.connect(
            lambda: self._move_history(-1)
        )
        self.command_input.history_next.connect(
            lambda: self._move_history(1)
        )

        command_row = QHBoxLayout()
        command_row.setContentsMargins(4, 0, 4, 4)
        command_row.setSpacing(7)
        command_row.addWidget(self.prompt_label)
        command_row.addWidget(self.command_input, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(self.output, 1)
        layout.addLayout(command_row)

        self.shell = QProcess(self)
        self.shell.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.shell.readyReadStandardOutput.connect(self._read_output)
        self.shell.started.connect(self._shell_started)
        self.shell.finished.connect(self._shell_finished)
        self.shell.errorOccurred.connect(self._shell_error)

        self._working_directory: Path | None = None
        self._history: list[str] = []
        self._history_index = 0
        self._shell_starting = False
        self.start_shell()

    @property
    def is_running(self) -> bool:
        return self.shell.state() != QProcess.ProcessState.NotRunning

    def start_shell(self) -> None:
        if self.is_running or self._shell_starting:
            return

        self._shell_starting = True
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUNBUFFERED", "1")
        self.shell.setProcessEnvironment(environment)

        if self._working_directory and self._working_directory.exists():
            self.shell.setWorkingDirectory(str(self._working_directory))

        if os.name == "nt":
            program = os.environ.get("COMSPEC", "cmd.exe")
            args = ["/Q", "/K"]
        else:
            program = os.environ.get("SHELL", "/bin/bash")
            args = ["--noprofile", "--norc", "-i"]

        self.append_system_message(f"Starting shell: {program}")
        self.shell.start(program, args)

    def restart_shell(self) -> None:
        if self.is_running:
            self.shell.kill()
            self.shell.waitForFinished(700)
        self._shell_starting = False
        self.start_shell()

    def shutdown(self) -> None:
        if not self.is_running:
            return
        self.shell.write(self._line_ending_command("exit"))
        self.shell.waitForBytesWritten(200)
        if not self.shell.waitForFinished(600):
            self.shell.kill()
            self.shell.waitForFinished(600)

    def set_working_directory(self, path: Path | str | None) -> None:
        if path is None:
            self._working_directory = None
            self.path_label.setText("No project selected")
            return

        target = Path(path).expanduser().resolve()
        self._working_directory = target
        self.path_label.setText(str(target))
        self.path_label.setToolTip(str(target))

        if not self.is_running:
            self.start_shell()
            return

        if os.name == "nt":
            command = f'cd /d "{target}"'
        else:
            command = f"cd -- {shlex.quote(str(target))}"
        self.run_command(command, record_history=False, show_command=False)
        self.append_system_message(f"Working directory: {target}")

    def run_command(
        self,
        command: str,
        *,
        record_history: bool = True,
        show_command: bool = True,
    ) -> None:
        command = str(command).strip()
        if not command:
            return

        if record_history:
            if not self._history or self._history[-1] != command:
                self._history.append(command)
            self._history_index = len(self._history)

        if show_command:
            self._append_text(f"\n> {command}\n")

        if not self.is_running:
            self.start_shell()
            self.append_system_message(
                "The shell is starting. Run the command again when it is ready."
            )
            return

        self.shell.write(self._line_ending_command(command))

    def show_processes(self) -> None:
        command = "tasklist" if os.name == "nt" else "ps -ef"
        self.run_command(command)

    def show_process(self, pid: int) -> None:
        """Display one process in the same command-line console."""

        if int(pid) <= 0:
            return
        if os.name == "nt":
            command = f'tasklist /FI "PID eq {int(pid)}"'
        else:
            command = (
                f"ps -p {int(pid)} -o pid,ppid,stat,etime,command"
            )
        self.run_command(command, record_history=False)

    def append_system_message(self, message: str) -> None:
        self._append_text(f"\n[Studio] {message}\n")

    def focus_command_line(self) -> None:
        self.command_input.setFocus()

    def _submit_command(self) -> None:
        command = self.command_input.text().strip()
        self.command_input.clear()
        self.run_command(command)

    def _move_history(self, delta: int) -> None:
        if not self._history:
            return
        self._history_index = max(
            0,
            min(len(self._history), self._history_index + delta),
        )
        if self._history_index == len(self._history):
            self.command_input.clear()
            return
        self.command_input.setText(self._history[self._history_index])
        self.command_input.end(False)

    def _line_ending_command(self, command: str) -> bytes:
        ending = "\r\n" if os.name == "nt" else "\n"
        return f"{command}{ending}".encode("utf-8", errors="replace")

    def _shell_started(self) -> None:
        self._shell_starting = False
        self.append_system_message(
            f"Console ready (PID {int(self.shell.processId())})."
        )
        if os.name == "nt":
            # UTF-8 keeps Python and Git output readable in the embedded view.
            self.shell.write(self._line_ending_command("chcp 65001 >NUL"))
        if self._working_directory:
            self.set_working_directory(self._working_directory)
        self.command_input.setFocus()

    def _shell_finished(self, exit_code: int, _status) -> None:
        self._shell_starting = False
        self.append_system_message(f"Shell exited with code {exit_code}.")

    def _shell_error(self, _error) -> None:
        self._shell_starting = False
        self.append_system_message(
            f"Shell error: {self.shell.errorString() or 'Unknown error'}"
        )

    def _read_output(self) -> None:
        raw = bytes(self.shell.readAllStandardOutput())
        if not raw:
            return

        encodings = ["utf-8"]
        preferred = locale.getpreferredencoding(False)
        if preferred and preferred.lower() != "utf-8":
            encodings.append(preferred)
        encodings.extend(["cp1252", "latin-1"])

        text = ""
        for encoding in encodings:
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            text = raw.decode("utf-8", errors="replace")
        self._append_text(text)

    def _append_text(self, text: str) -> None:
        self.output.moveCursor(QTextCursor.MoveOperation.End)
        self.output.insertPlainText(str(text))
        self.output.ensureCursorVisible()
        self.message_emitted.emit(str(text))

    def _show_context_menu(self, position) -> None:
        menu: QMenu = self.output.createStandardContextMenu(position)
        menu.addSeparator()
        processes_action = menu.addAction("List Processes")
        processes_action.triggered.connect(self.show_processes)
        restart_action = menu.addAction("Restart Shell")
        restart_action.triggered.connect(self.restart_shell)
        clear_action = menu.addAction("Clear Console")
        clear_action.triggered.connect(self.output.clear)
        menu.exec(self.output.viewport().mapToGlobal(position))
