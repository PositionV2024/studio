"""Exercise asynchronous setup with real, harmless Qt child processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QProcess
from PySide6.QtWidgets import QApplication

from studio.background_setup import BackgroundSetupController, StartupBanner
from studio.main_window import MainWindow


@pytest.fixture(scope="session")
def setup_app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "The background worker did not reach the expected state"


@pytest.fixture
def controller(tmp_path, setup_app):
    value = BackgroundSetupController(tmp_path)
    yield value
    if value.is_active:
        value.cancel()
        wait_for(setup_app, lambda: not value.is_active)
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    setup_app.processEvents()


def command(controller, code, *arguments):
    controller._command = lambda cancel_path: (
        sys.executable, ["-I", "-u", "-c", code, str(cancel_path), *map(str, arguments)],
    )


def event(kind, *, phase="background", message="Fixture progress"):
    return json.dumps({"event": kind, "phase": phase, "message": message})


@pytest.mark.parametrize("receipt,code,expected", [
    (event("ready"), 0, "ready"),
    (event("ready"), 5, "failed"),
    (event("ready", phase="core"), 0, "failed"),
    (event("status"), 0, "failed"),
    (event("error", message="A download failed"), 0, "failed"),
])
def test_readiness_requires_matching_receipt_and_successful_exit(controller, setup_app, receipt, code, expected):
    command(controller, f"import sys; print({receipt!r}, flush=True); sys.exit({code})")
    assert controller.start()
    wait_for(setup_app, lambda: not controller.is_active)
    assert controller.state == expected
    assert controller.blocks_runtime is (expected != "ready")


def test_early_receipt_does_not_unlock_or_accept_second_worker(controller, setup_app, tmp_path):
    release = tmp_path / "release"
    code = (
        "import pathlib,sys,time\n"
        f"print({event('ready')!r}, flush=True)\n"
        "while not pathlib.Path(sys.argv[2]).exists():\n"
        " if pathlib.Path(sys.argv[1]).exists(): raise SystemExit(130)\n"
        " time.sleep(0.01)\n"
    )
    command(controller, code, release)
    assert controller.start()
    wait_for(setup_app, lambda: controller._received_ready)
    original_pid = controller.process.processId()
    assert controller.is_active and controller.blocks_runtime
    assert not controller.start()
    assert controller.process.processId() == original_pid
    release.touch()
    wait_for(setup_app, lambda: not controller.is_active)
    assert controller.state == "ready"


def test_fragmented_utf8_and_final_line_without_newline_are_read(controller, setup_app):
    payload = (event("log", message="Preparing 日本語 support") + "\n" + event("ready")).encode()
    code = f"import os,time; data={payload!r}; os.write(1,data[:24]); time.sleep(0.02); os.write(1,data[24:])"
    command(controller, code)
    controller.start()
    wait_for(setup_app, lambda: not controller.is_active)
    assert controller.state == "ready"
    assert "Preparing 日本語 support" in controller.log_lines


def test_failed_launch_can_retry_without_stale_error_or_receipt(controller, setup_app, tmp_path):
    controller._command = lambda cancel_path: (str(tmp_path / "missing-python"), [])
    controller.start()
    wait_for(setup_app, lambda: not controller.is_active)
    assert controller.state == "failed" and controller.last_error
    command(controller, f"print({event('ready')!r}, flush=True)")
    controller.start()
    wait_for(setup_app, lambda: not controller.is_active)
    assert controller.state == "ready" and not controller.last_error


def test_cancel_waits_for_worker_exit_and_removes_only_its_token(controller, setup_app, tmp_path):
    retained = tmp_path / "my-script.py"
    retained.write_text("print('saved')\n")
    code = (
        "import pathlib,sys,time\n"
        f"print({event('status')!r}, flush=True)\n"
        "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.01)\n"
        "time.sleep(0.05)\n"
        "raise SystemExit(130)\n"
    )
    command(controller, code)
    controller.start()
    wait_for(setup_app, lambda: controller.state == "running")
    token = controller._cancel_path
    assert controller.cancel()
    assert controller.state == "cancelling" and controller.is_active
    wait_for(setup_app, lambda: not controller.is_active)
    assert controller.state == "cancelled"
    assert token is not None and not token.exists()
    assert retained.read_text() == "print('saved')\n"


def test_late_force_stop_result_cannot_kill_a_retried_worker(controller):
    controller._run_id = 2
    original = controller.process
    current = SimpleNamespace(
        processId=lambda: 1234,
        state=lambda: QProcess.ProcessState.Running,
        kill=Mock(),
    )
    controller.process = current
    try:
        controller._kill_finished(1, 1234)
        current.kill.assert_not_called()
        controller._kill_finished(2, 9999)
        current.kill.assert_not_called()
        controller._kill_finished(2, 1234)
        current.kill.assert_called_once()
    finally:
        controller.process = original


def test_banner_exposes_retry_and_logs_while_leaving_runtime_blocked(controller, setup_app):
    banner = StartupBanner(controller)
    banner.show()
    command(controller, f"print({event('error', message='Connection unavailable')!r}, flush=True); raise SystemExit(1)")
    controller.start()
    wait_for(setup_app, lambda: controller.state == "failed")
    assert banner.retry_button.isVisible() and banner.retry_button.isEnabled()
    assert "editor" in banner.message_label.text()
    banner.show_details()
    assert "Connection unavailable" in banner._log_view.toPlainText()
    banner._details.close()
    banner.close()
    banner.deleteLater()


def test_runtime_gating_preserves_independent_project_environments(tmp_path):
    app_root = tmp_path / "Studio"
    project_root = tmp_path / "My Project"
    shell = SimpleNamespace(
        app_root=app_root,
        background_setup=SimpleNamespace(blocks_runtime=True),
    )
    blocked = lambda executable: MainWindow._setup_runtime_blocked(shell, executable, project_path=project_root)
    assert blocked(app_root / ".venv" / "Scripts" / "python.exe")
    assert blocked(sys.executable)
    assert not blocked(project_root / ".venv" / "Scripts" / "python.exe")
    assert not blocked(".venv/Scripts/python.exe")
    shell.background_setup.blocks_runtime = False
    assert not blocked(sys.executable)
