"""Exercise the real toolbar and save-before-update close path without a server."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QWidget

from studio.main_window import MainWindow
from studio.update_install import UpdateInstallError


class UpdateWindow(MainWindow):
    def __init__(self, root, settings):
        QMainWindow.__init__(self)
        self.app_root = root
        self.ui_settings = QSettings(str(settings), QSettings.Format.IniFormat)
        self.current_project = None
        self.project_explorer = SimpleNamespace(**{
            name: Mock() for name in ("new_file", "new_folder", "add_files", "delete_selected")
        })
        self._update_project_actions = Mock()
        self.setCentralWidget(QWidget())
        self._create_toolbar()
        self._commit_visual_fields = Mock(return_value=True)
        self._can_close_all_editors = Mock(return_value=True)
        self._confirm_visual_exit = Mock(return_value=True)
        self._save_workspace_layout = Mock()
        self.open_editors = {}
        self.preview = SimpleNamespace(stop_preview=Mock())
        self.runner = SimpleNamespace(shutdown=Mock())
        self.console = SimpleNamespace(shutdown=Mock())
        self._closing_approved = False
        self._update_restart_requested = None
        self._waiting_for_update_shutdown = False
        self.resize(1350, 800)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    value = UpdateWindow(tmp_path, tmp_path / "settings.ini")
    value.ui_settings.setValue("updates/check_on_startup", True)
    monkeypatch.setattr("studio.main_window.shutdown_python_assist", lambda: None)
    yield value
    controller = getattr(value, "update_controller", None)
    if controller is not None and hasattr(controller, "_dialog") and controller._dialog:
        controller._dialog.hide()
    value.update_controller = None
    value._update_restart_requested = None
    value._can_close_all_editors.return_value = True
    value._confirm_visual_exit.return_value = True
    value._commit_visual_fields.return_value = True
    value.background_setup = None
    value.close()
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def controller_double(prepared):
    return SimpleNamespace(
        pending_update=prepared, is_active=False, launch_pending_update=Mock(),
        cancel_restart=Mock(), update_launch_failed=Mock(), begin_shutdown=Mock(),
    )


def test_update_control_is_right_aligned_and_works_without_a_project(window, app):
    window.show()
    app.processEvents()
    toolbar = window.main_toolbar
    assert toolbar.actions()[-1] is window.update_action
    assert window.update_button.objectName() == "UpdateToolbarButton"
    assert window.update_action.isEnabled()
    assert window.update_button.geometry().right() >= toolbar.width() - 18
    assert window.update_button.geometry().left() > window.view_button.geometry().right() + 200
    window.open_update_dialog()
    dialog = window.update_controller._dialog
    assert dialog.isVisible()
    assert window.update_controller.repository == "PositionV2024/studio"
    assert dialog.repository_label.text() == "PositionV2024/studio"
    assert dialog.check_button.isEnabled()
    assert not window.ui_settings.contains("updates/check_on_startup")
    assert not dialog.download_button.isEnabled()
    assert not window.update_controller.is_active
    window.open_update_dialog()
    assert window.update_controller._dialog is dialog


def test_opening_studio_does_not_schedule_an_update_check(window, app, monkeypatch):
    requests = []
    monkeypatch.setattr("studio.update_dialog.check_for_update", lambda *_args, **_kwargs: requests.append(True))
    window.ui_settings.setValue("updates/repository", "other-owner/old-project")
    window._initialize_updates()
    window.show()
    # The previous version used a 1.5-second single-shot timer on this path.
    # Process actual Qt events beyond that point to catch a leftover callback.
    QTest.qWait(1700)
    assert requests == []
    assert window.update_controller._dialog is None
    assert not window.update_controller.is_active
    assert window.update_controller.repository == "PositionV2024/studio"
    assert not window.ui_settings.contains("updates/check_on_startup")
    window.open_update_dialog()
    app.processEvents()
    assert requests == []
    assert not window.update_controller._dialog.download_button.isEnabled()


@pytest.mark.parametrize("prompt", ["_commit_visual_fields", "_can_close_all_editors", "_confirm_visual_exit"])
def test_cancelled_save_prompt_keeps_prepared_update_and_app_open(window, app, prompt, monkeypatch):
    prepared = object()
    controller = controller_double(prepared)
    window.update_controller = controller
    getattr(window, prompt).return_value = False
    monkeypatch.setattr(window, "_show_inline_commit_error", Mock())
    window.show()
    window._restart_for_update(prepared)
    assert window.isVisible()
    assert not window._closing_approved
    assert window._update_restart_requested is None
    assert controller.pending_update is prepared
    controller.launch_pending_update.assert_not_called()
    controller.begin_shutdown.assert_not_called()
    controller.cancel_restart.assert_called_once()
    window.runner.shutdown.assert_not_called()


def test_update_launch_happens_after_save_prompts_and_before_app_exit(window, app):
    order = []
    prepared = object()
    controller = controller_double(prepared)
    window.update_controller = controller
    window._can_close_all_editors.side_effect = lambda: order.append("code-saved") or True
    window._confirm_visual_exit.side_effect = lambda: order.append("ui-saved") or True
    controller.launch_pending_update.side_effect = lambda: order.append("helper-started")
    window.runner.shutdown.side_effect = lambda: order.append("runner-stopped")
    window.show()
    window._restart_for_update(prepared)
    assert order == ["code-saved", "ui-saved", "helper-started", "runner-stopped"]
    assert not window.isVisible()
    controller.launch_pending_update.assert_called_once()
    controller.cancel_restart.assert_not_called()


def test_normal_close_does_not_install_a_downloaded_update(window):
    controller = controller_double(object())
    window.update_controller = controller
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    controller.begin_shutdown.assert_called_once()
    controller.launch_pending_update.assert_not_called()


def test_stale_restart_request_cannot_launch_a_different_prepared_update(window):
    controller = controller_double(object())
    window.update_controller = controller
    window._restart_for_update(object())
    controller.launch_pending_update.assert_not_called()
    assert not window._closing_approved


def test_update_worker_drains_before_shutdown_without_repeating_save_prompts(window):
    controller = controller_double(None)
    controller.is_active = True
    window.update_controller = controller
    first = QCloseEvent()
    window.closeEvent(first)
    assert not first.isAccepted()
    assert window._closing_approved and window._waiting_for_update_shutdown
    assert not window.centralWidget().isEnabled()
    window.runner.shutdown.assert_not_called()
    controller.is_active = False
    second = QCloseEvent()
    window.closeEvent(second)
    assert second.isAccepted()
    window._can_close_all_editors.assert_called_once()
    window._confirm_visual_exit.assert_called_once()
    window.runner.shutdown.assert_called_once()


def test_background_setup_drains_before_update_helper_launch(window):
    prepared = object()
    controller = controller_double(prepared)
    window.update_controller = controller
    window._update_restart_requested = prepared
    setup = SimpleNamespace(is_active=True, cancel=Mock())
    window.background_setup = setup
    first = QCloseEvent()
    window.closeEvent(first)
    assert not first.isAccepted()
    setup.cancel.assert_called_once()
    controller.launch_pending_update.assert_not_called()
    setup.is_active = False
    second = QCloseEvent()
    window.closeEvent(second)
    assert second.isAccepted()
    controller.launch_pending_update.assert_called_once()
    window._confirm_visual_exit.assert_called_once()


def test_failed_helper_start_restores_editing_and_does_not_close_app(window, app, monkeypatch):
    prepared = object()
    controller = controller_double(prepared)
    controller.launch_pending_update.side_effect = UpdateInstallError("The update helper could not start.")
    window.update_controller = controller
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, title, text: messages.append((title, text)))
    window.show()
    window._restart_for_update(prepared)
    assert window.isVisible()
    assert not window._closing_approved
    assert window.centralWidget().isEnabled()
    assert window.update_action.isEnabled()
    assert window._update_restart_requested is None
    window.runner.shutdown.assert_not_called()
    assert messages == [("Update could not start", "The update helper could not start.")]
    controller.update_launch_failed.assert_called_once()
    controller.cancel_restart.assert_not_called()
