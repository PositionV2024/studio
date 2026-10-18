"""Exercise real Qt update controls with deterministic, offline services."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel, QLineEdit, QMainWindow

import studio.update_dialog as ui
from studio.update_install import PreparedUpdate, UpdateInstallError
from studio.update_service import UpdateCheck, UpdateError, UpdateRelease
from studio.github_connection import GitHubError


class Session:
    storage_available = False
    storage_warning = ""
    remembered = False

    def __init__(self, *, connected=False):
        self.has_token = connected
        self.account = {"login": "example-owner", "id": 12} if connected else None

    def connect(self, token, *, remember=False, cancelled=None):
        self.has_token = True
        self.account = {"login": "example-owner", "id": 12}
        self.remembered = remember
        return self.account


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_until(app, condition, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.003)
    raise AssertionError("The update operation did not finish")


@pytest.fixture
def release():
    return UpdateRelease(
        repository="PositionV2024/studio", version="0.4.20", tag="v0.4.20",
        release_url="https://github.com/PositionV2024/studio/releases/tag/v0.4.20",
        notes="A faster editor and new controls.", published_at="2026-10-18T10:30:00Z",
        asset_id=34, asset_name="HoYoProjectStudio-0.4.20-windows-x64.zip",
        asset_size=120, sha256="a" * 64,
    )


@pytest.fixture
def make_controller(app, tmp_path, monkeypatch):
    controllers = []
    parents = []
    monkeypatch.setattr(ui, "installation_support", lambda _root: (False, "Automatic installation is available in the Windows desktop package."))
    monkeypatch.setattr(ui, "validate_download_archive", lambda *_args, **_kwargs: None)

    def create(*, repository=None, startup=True, use_account=False, session=None, parent=None, old_check=None):
        settings = QSettings(str(tmp_path / f"settings-{len(controllers)}.ini"), QSettings.Format.IniFormat)
        if repository is not None:
            settings.setValue("updates/repository", repository)
        settings.setValue("updates/check_on_startup", startup)
        settings.setValue("updates/use_account", use_account)
        if old_check:
            settings.setValue("updates/last_checked_repository", old_check[0])
            settings.setValue("updates/last_checked", old_check[1])
        controller = ui.UpdateController(tmp_path / "app", settings, parent=parent, session=session or Session())
        controllers.append(controller)
        if parent is not None:
            parents.append(parent)
        return controller

    yield create
    for controller in controllers:
        controller.begin_shutdown()
        if controller.is_active:
            wait_until(app, lambda: not controller.is_active)
        if controller._dialog is not None:
            controller._dialog.close()
            controller._dialog.deleteLater()
        controller.deleteLater()
    for parent in parents:
        parent.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def checked(controller, release, app, monkeypatch):
    monkeypatch.setattr(ui, "check_for_update", lambda _repo, current, **_kwargs: UpdateCheck(current, release))
    assert controller.check_for_updates()
    wait_until(app, lambda: not controller.is_active)
    assert controller.release == release


@pytest.mark.parametrize("saved_source", [None, "", " \t "])
def test_initial_update_dialog_prefills_default_without_connecting_on_open(make_controller, monkeypatch, saved_source):
    calls = []
    monkeypatch.setattr(ui, "check_for_update", lambda *_args, **_kwargs: calls.append(True))
    controller = make_controller(repository=saved_source)
    controller.open_dialog()
    assert calls == []
    dialog = controller._dialog
    assert controller.repository == "PositionV2024/studio"
    assert controller.settings.value("updates/repository") == "PositionV2024/studio"
    assert dialog.repository_label.text() == "PositionV2024/studio"
    assert dialog.check_button.isEnabled()
    assert not dialog.download_button.isEnabled()
    assert not dialog.restart_button.isVisible()
    assert not dialog.account_checkbox.isChecked()
    assert dialog.current_version_label.text() == controller.current_version
    assert "Check for updates" in controller.status_message
    assert controller.button_text == "Update"


def test_open_reuses_the_same_modeless_dialog(make_controller):
    controller = make_controller()
    controller.open_dialog()
    dialog = controller._dialog
    assert not dialog.isModal()
    dialog.reject()
    controller.open_dialog()
    assert controller._dialog is dialog
    assert dialog.isVisible()


@pytest.mark.parametrize("saved_source", [
    "https://github.com/custom-owner/custom-studio.git",
    "different-owner/other-project",
    "https://wrong.example/owner/project",
    "https://user:test-secret-value@github.com/example-owner/studio",
    "https://github.com/example-owner/studio?token=test-secret-value",
])
def test_repository_is_fixed_and_old_custom_sources_are_replaced(make_controller, saved_source):
    controller = make_controller(repository=saved_source)
    controller.open_dialog()
    label = controller._dialog.repository_label
    assert isinstance(label, QLabel)
    assert controller._dialog.findChild(QLineEdit, "UpdateRepository") is None
    assert label.text() == "PositionV2024/studio"
    assert label.textInteractionFlags() == Qt.TextInteractionFlag.TextSelectableByMouse
    label.setFocus()
    QTest.keyClicks(label, "different-owner/other-project")
    assert label.text() == "PositionV2024/studio"
    with pytest.raises(AttributeError):
        controller.repository = "different-owner/other-project"
    assert controller.repository == "PositionV2024/studio"
    assert controller.settings.value("updates/repository") == "PositionV2024/studio"
    controller.settings.sync()
    assert "test-secret-value" not in Path(controller.settings.fileName()).read_text(encoding="utf-8")
    assert "test-secret-value" not in controller.status_message
    assert controller._dialog.check_button.isEnabled()


def test_public_check_is_off_thread_responsive_and_never_downloads(app, make_controller, release, monkeypatch):
    controller = make_controller(repository=release.repository, session=Session(connected=True))
    controller.open_dialog()
    started = threading.Event()
    finish = threading.Event()
    calls = []
    forbidden = []

    def check(repository, current, *, session, cancelled):
        calls.append((repository, session, threading.current_thread()))
        started.set()
        while not finish.wait(0.005):
            if cancelled.is_set():
                raise UpdateError("Cancelled", cancelled=True)
        return UpdateCheck(current, release)

    monkeypatch.setattr(ui, "check_for_update", check)
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: forbidden.append("download"))
    monkeypatch.setattr(ui, "prepare_install", lambda *_args, **_kwargs: forbidden.append("prepare"))
    monkeypatch.setattr(ui, "launch_prepared_update", lambda *_args, **_kwargs: forbidden.append("launch"))
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    try:
        controller._dialog.check_button.click()
        wait_until(app, lambda: started.is_set() and len(ticks) >= 4)
        assert controller.state == "checking"
        assert not controller._dialog.check_button.isEnabled()
        assert controller._dialog.cancel_button.isVisible()
        finish.set()
        wait_until(app, lambda: not controller.is_active)
    finally:
        finish.set()
        timer.stop()
    assert calls[0][0] == release.repository
    assert calls[0][1] is None
    assert calls[0][2] is not threading.current_thread()
    assert forbidden == []
    assert controller.state == "available"
    assert controller.button_text == "Update available"
    assert controller._dialog.download_button.isEnabled()
    assert controller.pending_update is None
    assert controller.last_checked


@pytest.mark.parametrize("which,expected_state", [("same", "current"), ("older", "ahead")])
def test_current_or_older_release_cannot_download_or_restart(app, make_controller, release, monkeypatch, which, expected_state):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    release = replace(release, version=controller.current_version if which == "same" else "0.3.0")
    checked(controller, release, app, monkeypatch)
    assert controller.state == expected_state
    assert not controller._dialog.download_button.isEnabled()
    assert not controller.download()
    assert not controller.request_restart()
    assert controller._dialog.latest_version_label.text() == release.version


@pytest.mark.parametrize("result_kind", ["none", "unavailable", "current", "ahead", "error"])
def test_recheck_immediately_disables_previous_download_and_keeps_it_disabled_without_new_update(
    app, make_controller, release, monkeypatch, result_kind,
):
    controller = make_controller()
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)
    assert controller._dialog.download_button.isEnabled()
    started = threading.Event()
    finish = threading.Event()

    def recheck(_repo, current, **_kwargs):
        started.set()
        finish.wait(2)
        if result_kind == "error":
            raise UpdateError("GitHub is temporarily unavailable.")
        if result_kind == "none":
            return UpdateCheck(current, None, "No published update is available yet.")
        if result_kind == "unavailable":
            return UpdateCheck(current, replace(release, asset_id=0, asset_name="", asset_size=0, sha256=""),
                               "This version is still being prepared. Check again later.")
        return UpdateCheck(current, replace(release, version=current if result_kind == "current" else "0.1.0"))

    monkeypatch.setattr(ui, "check_for_update", recheck)
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: pytest.fail("No download is available"))
    assert controller.check_for_updates()
    wait_until(app, started.is_set)
    assert controller.release is None
    assert not controller._dialog.download_button.isEnabled()
    assert not controller.check_for_updates()
    assert not controller.download()
    assert not controller.request_restart()
    finish.set()
    wait_until(app, lambda: not controller.is_active)
    assert controller.state == result_kind
    assert controller._dialog.download_button.isVisible()
    assert not controller._dialog.download_button.isEnabled()
    assert not controller.download_available
    controller._dialog.download_button.click()
    assert not controller.download()
    assert not controller.request_restart()
    assert controller._dialog.check_button.isEnabled()


def test_disabled_download_is_visibly_grey_and_does_not_activate_on_hover_or_click(
    app, make_controller, release, monkeypatch,
):
    from studio.styles import APP_STYLE
    previous_style = app.styleSheet()
    app.setStyleSheet(APP_STYLE)
    try:
        controller = make_controller()
        controller.open_dialog()
        button = controller._dialog.download_button
        app.processEvents()
        before = button.grab().toImage().pixelColor(8, button.height() // 2)
        assert max(before.red(), before.green(), before.blue()) - min(before.red(), before.green(), before.blue()) < 20
        QTest.mouseMove(button, button.rect().center())
        QTest.qWait(20)
        after_hover = button.grab().toImage().pixelColor(8, button.height() // 2)
        assert after_hover == before
        monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: pytest.fail("Disabled button initiated a download"))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert not controller.is_active
        checked(controller, release, app, monkeypatch)
        enabled = button.grab().toImage().pixelColor(8, button.height() // 2)
        assert enabled.blue() - enabled.red() > 35
        assert button.isEnabled()
        assert button.cursor().shape() == Qt.CursorShape.PointingHandCursor
    finally:
        app.setStyleSheet(previous_style)


def test_release_from_a_different_source_cannot_enable_download(app, make_controller, release, monkeypatch):
    controller = make_controller()
    controller.open_dialog()
    other_release = replace(release, repository="different-owner/unrelated-app")
    monkeypatch.setattr(ui, "check_for_update", lambda _repo, current, **_kwargs: UpdateCheck(current, other_release))
    assert controller.check_for_updates()
    wait_until(app, lambda: not controller.is_active)
    assert controller.state == "error"
    assert controller.release is None
    assert not controller.last_checked
    assert not controller._dialog.download_button.isEnabled()
    assert not controller.download()


@pytest.mark.parametrize("action", ["check_for_updates", "download"])
def test_expired_private_connection_discards_previous_actionable_release(app, make_controller, release, monkeypatch, action):
    session = Session(connected=True)
    controller = make_controller(use_account=True, session=session)
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)
    assert controller._dialog.download_button.isEnabled()
    session.has_token = False
    session.account = None
    assert not getattr(controller, action)()
    assert controller.state == "error"
    assert controller.release is None
    assert not controller._dialog.download_button.isEnabled()
    assert not controller.download_available


@pytest.mark.parametrize("old_startup", [True, False])
def test_updates_require_check_click_regardless_of_old_startup_preference(app, make_controller, release, monkeypatch, old_startup):
    calls = []

    def check(repo, current, **_kwargs):
        calls.append(repo)
        return UpdateCheck(current, replace(release, repository=repo))

    monkeypatch.setattr(ui, "check_for_update", check)
    controller = make_controller(repository="example-owner/studio", startup=old_startup)
    assert not controller.settings.contains("updates/check_on_startup")
    assert controller._dialog is None
    controller.open_dialog()
    QTest.qWait(30)
    assert not controller.is_active
    assert controller._connect_dialog is None
    assert not any("starts" in checkbox.text() for checkbox in controller._dialog.findChildren(QCheckBox))
    assert calls == []
    controller._dialog.check_button.click()
    wait_until(app, lambda: not controller.is_active)
    assert calls == ["PositionV2024/studio"]


def test_private_open_never_checks_or_prompts_for_credentials(make_controller, monkeypatch):
    monkeypatch.setattr(ui, "check_for_update", lambda *_args, **_kwargs: pytest.fail("Opening Updates must not make requests"))
    controller = make_controller(repository="example-owner/studio", use_account=True)
    controller.open_dialog()
    QTest.qWait(30)
    assert controller._connect_dialog is None
    assert not controller.is_active
    assert controller._dialog.connect_button.isVisible()
    assert not controller._dialog.download_button.isEnabled()


def test_saved_source_changes_cannot_redirect_an_update_check(app, make_controller, release, monkeypatch):
    controller = make_controller(repository=release.repository)
    started = threading.Event()
    finish = threading.Event()

    calls = []

    def check(repository, current, **_kwargs):
        calls.append(repository)
        started.set()
        finish.wait(2)
        return UpdateCheck(current, release)

    monkeypatch.setattr(ui, "check_for_update", check)
    assert controller.check_for_updates()
    wait_until(app, started.is_set)
    controller.settings.setValue("updates/repository", "different-owner/new-studio")
    finish.set()
    wait_until(app, lambda: not controller.is_active)
    assert controller.repository == "PositionV2024/studio"
    assert controller.release == release
    assert calls == ["PositionV2024/studio"]
    assert controller.pending_update is None
    assert controller.state == "available"
    assert controller.download_available


def test_changing_account_preference_discards_in_flight_result(app, make_controller, release, monkeypatch):
    controller = make_controller(repository=release.repository, session=Session(connected=True))
    started = threading.Event()
    finish = threading.Event()

    def check(_repository, current, **_kwargs):
        started.set()
        finish.wait(2)
        return UpdateCheck(current, release)

    monkeypatch.setattr(ui, "check_for_update", check)
    assert controller.check_for_updates()
    wait_until(app, started.is_set)
    controller.set_use_account(True)
    finish.set()
    wait_until(app, lambda: not controller.is_active)
    assert controller.release is None
    assert controller.use_account
    assert controller.settings.value("updates/use_account", type=bool)


def test_external_account_change_discards_authenticated_result(app, make_controller, release, monkeypatch):
    session = Session(connected=True)
    controller = make_controller(repository=release.repository, use_account=True, session=session)
    started = threading.Event()
    finish = threading.Event()
    calls = []

    def check(_repo, current, **kwargs):
        calls.append(kwargs["session"])
        started.set()
        finish.wait(2)
        return UpdateCheck(current, release)

    monkeypatch.setattr(ui, "check_for_update", check)
    assert controller.check_for_updates()
    wait_until(app, started.is_set)
    session.has_token = False
    session.account = None
    finish.set()
    wait_until(app, lambda: not controller.is_active)
    assert calls == [session]
    assert controller.release is None
    assert "connection changed" in controller.status_message


def test_private_connection_reuses_token_form_with_read_only_permissions(make_controller):
    controller = make_controller(repository="example-owner/studio", use_account=True)
    controller.open_dialog()
    controller._dialog.connect_button.click()
    dialog = controller._connect_dialog
    assert dialog is not None
    assert dialog._session is controller.session
    query = parse_qs(urlsplit(dialog._setup_url).query)
    assert query["contents"] == ["read"]
    assert "administration" not in query
    assert "workflows" not in query
    assert "Read-only" in dialog.instructions_label.text()
    assert controller.is_active
    dialog.reject()
    assert not controller.is_active


def test_failed_check_retries_and_never_exposes_unexpected_exception_text(app, make_controller, release, monkeypatch):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()

    def fail(*_args, **_kwargs):
        raise RuntimeError("Authorization: Bearer test-secret-value")

    monkeypatch.setattr(ui, "check_for_update", fail)
    assert controller.check_for_updates()
    wait_until(app, lambda: not controller.is_active)
    assert controller.state == "error"
    assert "test-secret-value" not in controller.status_message
    assert controller._dialog.check_button.isEnabled()
    assert not controller.last_checked
    checked(controller, release, app, monkeypatch)
    assert controller.state == "available"


def test_closing_dialog_cancels_without_destroying_active_worker(app, make_controller, release, monkeypatch):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    started = threading.Event()
    stopped = threading.Event()
    idle = []
    controller.idle.connect(lambda: idle.append(not controller.is_active))

    def check(*_args, cancelled, **_kwargs):
        started.set()
        while not cancelled.wait(0.005):
            pass
        stopped.set()
        raise UpdateError("Cancelled", cancelled=True)

    monkeypatch.setattr(ui, "check_for_update", check)
    assert controller.check_for_updates()
    wait_until(app, started.is_set)
    controller._dialog.close()
    assert not controller._dialog.isVisible()
    wait_until(app, lambda: not controller.is_active)
    assert stopped.is_set()
    assert idle == [True]
    assert "was cancelled" in controller.status_message
    controller.open_dialog()
    assert controller._dialog.isVisible()
    assert controller._dialog.check_button.isEnabled()


def test_begin_shutdown_blocks_new_checks_and_dialogs(make_controller, monkeypatch):
    calls = []
    monkeypatch.setattr(ui, "check_for_update", lambda *_args, **_kwargs: calls.append(True))
    controller = make_controller(repository="example-owner/studio")
    assert not controller.begin_shutdown()
    assert not controller.check_for_updates()
    controller.open_dialog()
    assert controller._dialog is None
    assert calls == []


def test_shutdown_drains_private_sign_in_worker_before_idle(app, make_controller):
    session = Session()
    started = threading.Event()

    def connect(_token, *, remember, cancelled):
        started.set()
        while not cancelled.wait(0.005):
            pass
        raise GitHubError("The connection was cancelled.", cancelled=True)

    session.connect = connect
    controller = make_controller(repository="example-owner/studio", use_account=True, session=session)
    controller.open_dialog()
    controller.connect_github()
    dialog = controller._connect_dialog
    dialog.token_input.setText("test-private-token")
    dialog.connect_button.click()
    wait_until(app, started.is_set)
    idle = []
    controller.idle.connect(lambda: idle.append(not controller.is_active))
    assert controller.begin_shutdown()
    assert not controller._dialog.check_button.isEnabled()
    assert not controller._dialog.account_checkbox.isEnabled()
    assert not controller._dialog.isVisible()
    assert not dialog.isVisible()
    wait_until(app, lambda: not controller.is_active)
    assert idle == [True]
    assert not session.has_token
    assert not controller.check_for_updates()


def test_approved_parent_close_leaves_no_stranded_update_window(app, make_controller):
    class Host(QMainWindow):
        def closeEvent(self, event):
            self.controller.begin_shutdown()
            super().closeEvent(event)

    host = Host()
    controller = make_controller(parent=host)
    host.controller = controller
    host.show()
    controller.open_dialog()
    app.processEvents()
    assert host.isVisible() and controller._dialog.isVisible()
    assert host.close()
    app.processEvents()
    assert not host.isVisible()
    assert not controller._dialog.isVisible()
    assert not controller.is_active


def test_last_window_close_quits_application_with_updater_open(tmp_path):
    # A fresh Qt process verifies quitOnLastWindowClosed, which is shared global
    # state and cannot be reliably tested alongside other module QApplication
    # fixtures. No network or installed GitHub credential is used.
    code = r'''
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow
from studio.update_dialog import UpdateController

app = QApplication([])
events = []
class Host(QMainWindow):
    def closeEvent(self, event):
        self.controller.begin_shutdown()
        super().closeEvent(event)
host = Host()
session = SimpleNamespace(account=None, has_token=False, storage_warning='')
host.controller = UpdateController(Path.cwd(), QSettings(sys.argv[1], QSettings.Format.IniFormat), parent=host, session=session)
host.show()
host.controller.open_dialog()
app.lastWindowClosed.connect(lambda: events.append('last-window-closed'))
app.aboutToQuit.connect(lambda: events.append('quit'))
QTimer.singleShot(25, host.close)
def fallback():
    events.append('timeout')
    app.quit()
QTimer.singleShot(1500, fallback)
app.exec()
print(json.dumps({'events': events, 'host_visible': host.isVisible(), 'update_visible': host.controller._dialog.isVisible()}))
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "quit-test.ini")],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["events"] == ["last-window-closed", "quit"]
    assert not observed["host_visible"]
    assert not observed["update_visible"]


def test_manual_download_provides_zip_without_installation(app, make_controller, release, monkeypatch, tmp_path):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "downloaded.zip"
    archive.write_bytes(b"verified by fake download service")
    calls = []
    validations = []

    def download(item, destination, **kwargs):
        calls.append((item, kwargs["session"]))
        kwargs["progress"](60, 120)
        kwargs["progress"](120, 120)
        return archive

    monkeypatch.setattr(ui, "download_update", download)
    monkeypatch.setattr(ui, "validate_download_archive", lambda *args, **_kwargs: validations.append((args, threading.current_thread())))
    monkeypatch.setattr(ui, "prepare_install", lambda *_args, **_kwargs: pytest.fail("Manual installation cannot prepare or replace the app"))
    monkeypatch.setattr(ui, "launch_prepared_update", lambda *_args, **_kwargs: pytest.fail("Downloading cannot apply an update"))
    controller._dialog.download_button.click()
    wait_until(app, lambda: not controller.is_active)
    assert calls == [(release, None)]
    assert validations[0][0] == (archive, release.version)
    assert validations[0][1] is not threading.current_thread()
    assert controller.state == "ready"
    assert controller.downloaded_archive == archive
    assert controller.pending_update is None
    assert not controller.request_restart()
    assert controller._dialog.folder_button.isVisible()
    assert not controller._dialog.restart_button.isVisible()
    assert "extract the ZIP into a new folder" in controller.status_message


@pytest.mark.parametrize("failure", [
    "The manual update ZIP contains an unsafe path.",
    "The manual update ZIP has an invalid file checksum.",
    "The manual update ZIP has an unexpected package version.",
])
def test_manual_download_validation_failure_never_recommends_extraction(app, make_controller, release, monkeypatch, tmp_path, failure):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "rejected-manual-download.zip"
    archive.write_bytes(b"rejected by inner archive verification")
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: archive)

    def validate(*_args, **_kwargs):
        raise UpdateInstallError(failure)

    monkeypatch.setattr(ui, "validate_download_archive", validate)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    assert controller.state == "error"
    assert failure in controller.status_message
    assert controller.downloaded_archive is None
    assert controller.pending_update is None
    assert not controller._dialog.folder_button.isVisible()
    assert not controller._dialog.restart_button.isVisible()
    assert not controller._dialog.download_button.isEnabled()
    assert "extract" not in controller.status_message.casefold()


def test_prepared_download_needs_explicit_restart_and_save_cancel_can_resume(app, make_controller, release, monkeypatch, tmp_path):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "download.zip"
    archive.write_bytes(b"test package")
    prepared = PreparedUpdate(tmp_path / "job", tmp_path / "job.json", release.version, release.repository, archive, ())
    requests = []
    launches = []
    calls = []
    controller.restart_requested.connect(requests.append)
    monkeypatch.setattr(ui, "installation_support", lambda _root: (True, ""))
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: archive)

    def prepare(*args, **kwargs):
        calls.append((args, threading.current_thread()))
        assert callable(kwargs["cancelled"])
        kwargs["progress"]("Verified the update files.")
        return prepared

    monkeypatch.setattr(ui, "prepare_install", prepare)
    monkeypatch.setattr(ui, "launch_prepared_update", launches.append)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    assert controller.pending_update is prepared
    assert controller.state == "ready"
    assert requests == [] and launches == []
    assert calls[0][0][:4] == (archive, controller.app_root, release.version, release.repository)
    assert calls[0][1] is not threading.current_thread()
    assert controller._dialog.restart_button.isVisible()
    with pytest.raises(UpdateInstallError):
        controller.launch_pending_update()
    controller._dialog.restart_button.click()
    assert requests == [prepared]
    assert launches == []
    assert not controller.request_restart()
    controller.cancel_restart()
    assert controller.pending_update is prepared
    assert controller._dialog.restart_button.isEnabled()
    assert controller.request_restart()
    controller.begin_shutdown()
    controller.launch_pending_update()
    assert launches == [prepared]


def test_failed_download_requires_a_fresh_check_before_retry_and_cannot_offer_restart(app, make_controller, release, monkeypatch, tmp_path):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)

    def download(*_args, **_kwargs):
        raise UpdateError("The download was interrupted. Try again.")

    monkeypatch.setattr(ui, "download_update", download)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    assert controller.state == "error"
    assert not controller._dialog.download_button.isEnabled()
    assert controller.release is None
    assert controller.pending_update is None
    assert not controller.request_restart()
    assert not controller.download()
    assert "Check for updates" in controller.status_message
    archive = tmp_path / "retry.zip"
    archive.write_bytes(b"verified on retry")
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: archive)
    checked(controller, release, app, monkeypatch)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    assert controller.downloaded_archive == archive
    assert controller.state == "ready"


def test_failed_helper_launch_requires_fresh_preparation(app, make_controller, release, monkeypatch, tmp_path):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "download.zip"
    archive.write_bytes(b"package")
    prepared = PreparedUpdate(tmp_path / "job", tmp_path / "job.json", release.version, release.repository, archive, ())
    controller._download_ready(ui._DownloadedUpdate(archive, prepared))
    assert controller.request_restart()
    controller.begin_shutdown()
    assert not controller._dialog.isVisible()
    # MainWindow can call begin_shutdown again after another worker drains.
    # This must not forget that the original update dialog was visible.
    controller.begin_shutdown()
    controller.update_launch_failed("The helper did not become ready.")
    assert controller.pending_update is None
    assert not controller.request_restart()
    assert not controller._shutting_down
    assert controller._dialog.download_button.isVisible()
    assert controller._dialog.download_button.isEnabled()
    assert not controller._dialog.restart_button.isVisible()
    assert controller._dialog.isVisible()
    assert "prepare a new restart" in controller.status_message
    assert archive.is_file()


@pytest.mark.parametrize("failure", [
    "The update archive contains an unsafe path.",
    "A file in the update archive has an invalid checksum.",
    "The package version does not match the requested update.",
    "A managed application file has local changes.",
])
def test_prepare_failures_never_offer_manual_extraction(app, make_controller, release, monkeypatch, tmp_path, failure):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "download.zip"
    archive.write_bytes(b"package")
    monkeypatch.setattr(ui, "installation_support", lambda _root: (True, ""))
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: archive)

    def prepare(*_args, **_kwargs):
        raise UpdateInstallError(failure)

    monkeypatch.setattr(ui, "prepare_install", prepare)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    assert controller.state == "error"
    assert controller.downloaded_archive is None
    assert controller.pending_update is None
    assert failure in controller.status_message
    assert "extract" not in controller.status_message.casefold()
    assert not controller._dialog.folder_button.isVisible()
    assert not controller._dialog.restart_button.isVisible()
    assert not controller._dialog.download_button.isEnabled()
    assert controller.release is None
    assert not controller.request_restart()


def test_cancellation_during_preparation_cannot_offer_restart(app, make_controller, release, monkeypatch, tmp_path):
    controller = make_controller(repository=release.repository)
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "download.zip"
    archive.write_bytes(b"package")
    started = threading.Event()
    monkeypatch.setattr(ui, "installation_support", lambda _root: (True, ""))
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: archive)

    def prepare(*_args, cancelled, **_kwargs):
        started.set()
        while not cancelled():
            time.sleep(0.005)
        raise UpdateInstallError("Cancelled", cancelled=True)

    monkeypatch.setattr(ui, "prepare_install", prepare)
    assert controller.download()
    wait_until(app, started.is_set)
    controller.cancel()
    wait_until(app, lambda: not controller.is_active)
    assert controller.pending_update is None
    assert controller.downloaded_archive is None
    assert not controller.request_restart()
    assert controller.update_is_newer


def test_last_successful_check_is_persisted_and_cleared_when_access_preference_changes(app, make_controller, release, monkeypatch):
    controller = make_controller(repository=release.repository)
    checked(controller, release, app, monkeypatch)
    assert controller.settings.value("updates/last_checked") == controller.last_checked
    assert controller.settings.value("updates/last_checked_repository") == release.repository
    controller.set_use_account(True)
    assert not controller.last_checked
    assert not controller.settings.contains("updates/last_checked")
    assert controller.release is None


def test_old_check_timestamp_is_not_shown_for_a_replaced_custom_source(make_controller):
    controller = make_controller(repository="example-owner/other-studio", old_check=("example-owner/other-studio", "2026-10-17T10:30:00+00:00"))
    controller.open_dialog()
    assert not controller.last_checked
    assert controller._dialog.last_checked_label.text() == "Not checked yet"


def test_release_notes_are_plain_text_and_urls_require_explicit_click(app, make_controller, release, monkeypatch):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    release = replace(release, notes='<img src="https://untrusted.example/tracker"><script>download()</script>')
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    checked(controller, release, app, monkeypatch)
    assert controller._dialog.notes.toPlainText() == release.notes
    assert controller._dialog.status_label.textFormat() == Qt.TextFormat.PlainText
    assert opened == []
    controller._dialog.release_button.click()
    assert opened == [release.release_url]
    controller.release = replace(release, release_url="https://github.com@untrusted.example/release")
    assert not controller.open_release()
    assert opened == [release.release_url]


def test_download_folder_opens_only_the_returned_local_archive(app, make_controller, release, monkeypatch, tmp_path):
    controller = make_controller(repository=release.repository)
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "download.zip"
    archive.write_bytes(b"package")
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: archive)
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    assert opened == []
    assert controller.open_download_folder()
    assert opened[0].isLocalFile()
    assert Path(opened[0].toLocalFile()) == archive.parent


def test_removed_manual_download_can_be_downloaded_again(app, make_controller, release, monkeypatch, tmp_path):
    controller = make_controller(repository=release.repository)
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "download.zip"
    archive.write_bytes(b"package")
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: archive)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    archive.unlink()
    assert not controller.open_download_folder()
    assert controller.downloaded_archive is None
    assert controller._dialog.download_button.isVisible()
    assert controller._dialog.download_button.isEnabled()


def test_preparation_uses_durable_installer_default_directory(app, make_controller, release, monkeypatch, tmp_path):
    controller = make_controller(repository=release.repository)
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "download.zip"
    archive.write_bytes(b"package")
    monkeypatch.setattr(ui, "installation_support", lambda _root: (True, ""))
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: archive)
    called = []

    def prepare(*_args, **kwargs):
        called.append(kwargs)
        return PreparedUpdate(tmp_path / "durable-job", tmp_path / "job.json", release.version, release.repository, archive, ())

    monkeypatch.setattr(ui, "prepare_install", prepare)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    assert len(called) == 1
    assert "cache_root" not in called[0]
