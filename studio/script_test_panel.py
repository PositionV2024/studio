from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .script_runner import ScriptRunner


PANEL_STYLE = """
QWidget#ScriptTestPanel { background: #0b1324; color: #dce8fb; }
QLabel#ScriptTestFile { color: #dce8fb; font-weight: 600; }
QLabel#ScriptTestStatus { color: #9db3d2; font-size: 11px; }
QLabel#ScriptTestStatus[result="success"] { color: #86d9a9; }
QLabel#ScriptTestStatus[result="error"] { color: #ff9cae; }
QLabel#ScriptTestStatus[result="stopped"] { color: #e7c48a; }
QPlainTextEdit#ScriptTestOutput {
    color: #d9e7ff; background: #070d18; border: 1px solid #1f3150;
    border-radius: 5px; padding: 6px; font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px; selection-color: #ffffff; selection-background-color: #315f9a;
}
QLineEdit#ScriptTestInput {
    color: #e3efff; background: #091120; border: 1px solid #2d466b;
    border-radius: 5px; padding: 5px 8px; min-height: 20px;
}
QLineEdit#ScriptTestInput:focus { border-color: #6aa8f5; }
QLineEdit#ScriptTestInput:disabled { color: #6c7d97; background: #0c1524; }
QPushButton#ScriptTestButton {
    color: #d7e6fc; background: #14233b; border: 1px solid #344c6b;
    border-radius: 5px; padding: 4px 10px; min-height: 20px;
}
QPushButton#ScriptTestButton:hover { background: #244164; border-color: #628bc0; }
QPushButton#ScriptTestButton:disabled {
    color: #71829e; background: #121d30; border-color: #263b59;
}
QPushButton#ScriptTestStop {
    color: #ffdde5; background: #552535; border: 1px solid #9a4a63;
    border-radius: 5px; padding: 4px 12px; min-height: 20px;
}
QPushButton#ScriptTestStop:hover { background: #713147; border-color: #c76b85; }
QPushButton#ScriptTestStop:disabled {
    color: #71829e; background: #121d30; border-color: #263b59;
}
"""


class ScriptTestPanel(QWidget):
    """Output and input for the one script test, independent of editor tabs."""

    def __init__(self, runner: ScriptRunner, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ScriptTestPanel")
        self.setStyleSheet(PANEL_STYLE)
        self.runner = runner
        self._input_open = True
        self._terminal_message = ""
        self._failure_message = ""
        self._running_state = "idle"

        self.file_label = QLabel("No script tested")
        self.file_label.setObjectName("ScriptTestFile")
        self.file_label.setTextFormat(Qt.TextFormat.PlainText)
        self.file_label.setMinimumWidth(80)
        self.file_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_label = QLabel("Open a Python file and choose Test Script.")
        self.status_label.setObjectName("ScriptTestStatus")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("ScriptTestStop")
        self.stop_button.setToolTip("Stop the running script test (Shift+F7)")
        self.stop_button.clicked.connect(runner.stop)
        self.clear_button = self._button("Clear", "Clear the displayed test output")

        labels = QVBoxLayout()
        labels.setSpacing(1)
        labels.addWidget(self.file_label)
        labels.addWidget(self.status_label)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addLayout(labels, 1)
        header.addWidget(self.stop_button)
        header.addWidget(self.clear_button)

        self.output = QPlainTextEdit()
        self.output.setObjectName("ScriptTestOutput")
        self.output.setReadOnly(True)
        self.output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.output.setMaximumBlockCount(8000)
        self.output.setPlaceholderText(
            "Printed output, errors and the final exit result will appear here."
        )
        self.clear_button.clicked.connect(self.output.clear)

        self.input_edit = QLineEdit()
        self.input_edit.setObjectName("ScriptTestInput")
        self.input_edit.setAccessibleName("Input for the running Python script")
        self.input_edit.setPlaceholderText("Type a response for input(), then press Enter")
        self.input_edit.setToolTip("Send one line of input to the running script")
        self.input_edit.returnPressed.connect(self._send_input)
        self.send_button = self._button("Send", "Send this line to the running script")
        self.send_button.clicked.connect(self._send_input)
        self.eof_button = self._button(
            "End Input", "Close standard input when the script is reading until the end of input",
        )
        self.eof_button.clicked.connect(self._close_input)
        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        input_row.addWidget(self.input_edit, 1)
        input_row.addWidget(self.send_button)
        input_row.addWidget(self.eof_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.output, 1)
        layout.addLayout(input_row)

        runner.log_received.connect(self.append_output)
        runner.state_changed.connect(self._state_changed)
        runner.failed.connect(self._failed)
        runner.finished.connect(self._finished)
        self._update_controls()

    def _button(self, label: str, tooltip: str) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName("ScriptTestButton")
        button.setToolTip(tooltip)
        return button

    def prepare_run(self, path: Path, project_path: Path) -> None:
        """Reset only the script test, keeping project/preview output intact."""

        self.output.clear()
        self.input_edit.clear()
        self._input_open = True
        self._terminal_message = ""
        self._failure_message = ""
        self._running_state = "starting"
        self.file_label.setText(Path(path).name)
        self.file_label.setToolTip(f"Script: {path}\nWorking directory: {project_path}")
        self._set_status("Starting…")
        self._update_controls()

    def append_output(self, text: str) -> None:
        if not text:
            return
        bar = self.output.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 3
        cursor = QTextCursor(self.output.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(str(text))
        if follow:
            bar.setValue(bar.maximum())

    def _set_status(self, message: str, result: str = "") -> None:
        self.status_label.setText(message)
        self.status_label.setProperty("result", result)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _state_changed(self, state: str) -> None:
        self._running_state = state
        if state == "starting":
            self._set_status("Starting…")
        elif state == "running":
            self._set_status("Running")
        elif state == "stopping":
            self._set_status("Stopping…", "stopped")
        elif not self._terminal_message:
            self._set_status(
                {"failed": "Failed", "stopped": "Stopped", "finished": "Finished"}.get(state, state),
                "error" if state == "failed" else "stopped" if state == "stopped" else "",
            )
        self._update_controls()

    def _failed(self, message: str) -> None:
        # The runner sends the full error through log_received as well. Avoid
        # duplicating tracebacks and preserve controls if a start was rejected.
        self._failure_message = message
        if not self.runner.is_running:
            self._set_status("Failed — see output", "error")
        self._update_controls()

    def _finished(self, path: str, exit_code: int, stopped: bool) -> None:
        self.file_label.setText(Path(path).name)
        if stopped:
            message, result = f"Stopped (exit code {exit_code})", "stopped"
        elif exit_code == 0 and self.runner.state != "failed":
            message, result = "Finished successfully (exit code 0)", "success"
        elif exit_code == -1 and self._failure_message:
            message, result = "Could not start script", "error"
        else:
            message, result = f"Failed (exit code {exit_code})", "error"
        self._terminal_message = message
        self._set_status(message, result)
        self.append_output(f"\n\n[Studio] {message}.\n")
        self._update_controls()

    def _update_controls(self) -> None:
        active = self.runner.is_running
        self.stop_button.setEnabled(active and self._running_state != "stopping")
        can_input = active and self._running_state == "running" and self._input_open
        for widget in (self.input_edit, self.send_button, self.eof_button):
            widget.setEnabled(can_input)

    def _send_input(self) -> None:
        if not self.input_edit.isEnabled():
            return
        text = self.input_edit.text()
        if self.runner.write_input(text + "\n"):
            self.append_output(text + "\n")
            self.input_edit.clear()

    def _close_input(self) -> None:
        if self.runner.close_input():
            self._input_open = False
            self.append_output("\n[Studio] Input closed.\n")
            self._update_controls()
