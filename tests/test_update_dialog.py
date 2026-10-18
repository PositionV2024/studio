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
from studio.update_compare import ComparisonError, FileChange, ReleaseArchiveError, ReleaseComparison


class Session:
    storage_available = False
    storage_warning = ""
    remembered = False

    def __init__(self, *, connected=False):
        self.has_token = connected
        self.account = {"login": "example-owner", "id": 12} if connected else None
        self.token = "test_connected_token_for_offline_tests" if connected else ""

    def get_token(self):
        if not self.has_token:
            raise GitHubError("Not connected")
        return self.token

    def connect(self, token, *, remember=False, cancelled=None):
        self.has_token = True
        self.token = token
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

    def resolve(release, **kwargs):
        assert kwargs["session"] is not None
        if not release.is_downloadable:
            raise UpdateError("File comparison is unavailable. No complete application ZIP is attached.")
        return release

    def package(*_args, **_kwargs):
        archive = tmp_path / "comparison.zip"
        archive.write_bytes(b"test package")
        return archive

    def compare(*_args, **_kwargs):
        raise ComparisonError("This fixture has no local application inventory to compare.")

    monkeypatch.setattr(ui, "resolve_release_package", resolve)
    monkeypatch.setattr(ui, "download_update", package)
    monkeypatch.setattr(ui, "compare_release_archive", compare)

    def create(*, repository=None, startup=True, use_account=False, session=None, parent=None, old_check=None):
        settings = QSettings(str(tmp_path / f"settings-{len(controllers)}.ini"), QSettings.Format.IniFormat)
        if repository is not None:
            settings.setValue("updates/repository", repository)
        settings.setValue("updates/check_on_startup", startup)
        settings.setValue("updates/use_account", use_account)
        if old_check:
            settings.setValue("updates/last_checked_repository", old_check[0])
            settings.setValue("updates/last_checked", old_check[1])
        controller = ui.UpdateController(tmp_path / "app", settings, parent=parent, session=session or Session(connected=True))
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
    controller = make_controller(repository=saved_source, session=Session())
    controller.open_dialog()
    assert calls == []
    dialog = controller._dialog
    assert controller.repository == "PositionV2024/studio"
    assert controller.settings.value("updates/repository") == "PositionV2024/studio"
    assert dialog.repository_label.text() == "PositionV2024/studio"
    assert not dialog.check_button.isEnabled()
    assert not dialog.download_button.isEnabled()
    assert not dialog.restart_button.isVisible()
    assert not hasattr(dialog, "account_checkbox")
    assert dialog.connect_button.isVisible()
    assert "required" in dialog.account_label.text().lower()
    assert dialog.current_version_label.text() == controller.current_version
    assert "Connect your GitHub account" in controller.status_message
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


def test_connected_check_is_off_thread_responsive_and_only_reads_package_for_comparison(app, make_controller, release, monkeypatch):
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
    assert calls[0][1] is controller.session
    assert calls[0][2] is not threading.current_thread()
    assert forbidden == ["download"]
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
        with monkeypatch.context() as context:
            context.setattr(ui, "download_update", lambda *_args, **_kwargs: pytest.fail("Disabled button initiated a download"))
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


def test_replacing_token_for_same_account_discards_in_flight_result(app, make_controller, release, monkeypatch):
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
    controller.session.token = "test-replacement-token"
    controller._refresh_connection()
    finish.set()
    wait_until(app, lambda: not controller.is_active)
    assert controller.release is None
    assert controller.has_connection
    assert not controller.settings.contains("updates/use_account")


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
    assert controller._dialog.latest_version_label.text() == "Could not check"
    assert controller._dialog.comparison_summary.text() == "File comparison unavailable"
    assert "test-secret-value" not in controller._dialog.comparison_context.toPlainText()
    assert controller._dialog.comparison_context.toPlainText() == controller.status_message
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
    assert not controller._dialog.connect_button.isEnabled()
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
    assert calls == [(release, controller.session)]
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
    assert controller.release == release
    assert controller._dialog.latest_version_label.text() == release.version
    assert controller._dialog.notes.toPlainText() == release.notes
    assert controller._dialog.comparison_summary.text() == "File comparison unavailable"
    assert "The download was interrupted" in controller._dialog.comparison_context.toPlainText()
    assert controller._dialog.comparison_empty_state.isVisible()
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
    assert controller.release == release
    assert controller._dialog.latest_version_label.text() == release.version
    assert controller._dialog.notes.toPlainText() == release.notes
    assert failure in controller._dialog.comparison_context.toPlainText()
    assert controller._dialog.comparison_summary.text() == "File comparison unavailable"
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


def test_last_successful_check_is_persisted_and_cleared_when_account_changes(app, make_controller, release, monkeypatch):
    controller = make_controller(repository=release.repository)
    checked(controller, release, app, monkeypatch)
    assert controller.settings.value("updates/last_checked") == controller.last_checked
    assert controller.settings.value("updates/last_checked_repository") == release.repository
    controller.session.token = "different-test-token"
    controller._refresh_connection()
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


@pytest.mark.parametrize("legacy_account_setting", [True, False])
def test_account_is_required_and_old_opt_out_cannot_enable_requests(
    app, make_controller, monkeypatch, legacy_account_setting,
):
    requests = []
    monkeypatch.setattr(ui, "check_for_update", lambda *_args, **_kwargs: requests.append("check"))
    monkeypatch.setattr(ui, "download_update", lambda *_args, **_kwargs: requests.append("download"))
    controller = make_controller(session=Session(), use_account=legacy_account_setting)
    controller.open_dialog()
    dialog = controller._dialog
    assert not hasattr(controller, "use_account")
    assert not hasattr(controller, "set_use_account")
    assert not hasattr(dialog, "account_checkbox")
    assert not controller.settings.contains("updates/use_account")
    assert dialog.connect_button.isVisible()
    assert "Connect" in dialog.connect_button.text()
    assert not dialog.check_button.isEnabled()
    assert not dialog.download_button.isEnabled()
    dialog.check_button.click()
    dialog.download_button.click()
    assert not controller.check_for_updates()
    assert not controller.download()
    assert not controller.request_restart()
    assert requests == []


def test_existing_shared_account_is_used_without_an_opt_in(make_controller):
    session = Session(connected=True)
    controller = make_controller(session=session, use_account=False)
    controller.open_dialog()
    assert controller.has_connection
    assert controller._dialog.check_button.isEnabled()
    assert controller._dialog.connect_button.text() == "Change account…"
    assert "example-owner" in controller._dialog.account_label.text()
    assert session.token not in controller._dialog.account_label.text()
    assert session.token not in controller.status_message
    assert session.token not in repr(controller._observed_account)


def comparison_fixture(controller, release):
    return ReleaseComparison(
        current_version=controller.current_version, release_version=release.version,
        changes=(
            FileChange("app/studio/new.py", "added", None, 12,
                       "--- current desktop/app/studio/new.py\n+++ release/app/studio/new.py\n@@ -0,0 +1 @@\n+print('new')\n"),
            FileChange("app/studio/editor.py", "modified", 14, 14,
                       "--- current desktop/app/studio/editor.py\n+++ release/app/studio/editor.py\n@@ -1 +1 @@\n-print('old')\n+print('new')\n", True),
            FileChange("app/studio/old.py", "removed", 8, None,
                       "--- current desktop/app/studio/old.py\n+++ release/app/studio/old.py\n@@ -1 +0,0 @@\n-old = 1\n"),
            FileChange("HoYo Project Studio.exe", "modified", 32, 33,
                       "Binary file changed. A text diff is unavailable."),
            FileChange("app/resources/large.txt", "modified", 1_000_000, 1_000_001,
                       "This file is too large for a text diff."),
            FileChange("app/studio/locked.py", "unavailable", None, 23,
                       "The desktop file could not be read. Its contents were not compared."),
        ),
        unchanged_count=97,
        warnings=("Comparison uses saved application files. Unsaved editor changes are not included.",),
    )


def test_file_changes_tab_lists_counts_and_colored_readonly_text_without_extra_file_reads(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()
    comparison = comparison_fixture(controller, release)
    threads = []

    def compare(*_args, **_kwargs):
        threads.append(threading.current_thread())
        return comparison

    monkeypatch.setattr(ui, "compare_release_archive", compare)
    checked(controller, release, app, monkeypatch)
    dialog = controller._dialog
    assert dialog.tabs.tabText(dialog.tabs.currentIndex()) == "File changes"
    assert dialog.tabs.tabText(1) == "Release notes"
    assert controller.comparison is comparison
    assert "Current desktop" in dialog.comparison_direction.text()
    assert release.version in dialog.comparison_direction.text()
    assert "1 added" in dialog.comparison_summary.text()
    assert "3 modified" in dialog.comparison_summary.text()
    assert "1 removed" in dialog.comparison_summary.text()
    assert "1 unavailable" in dialog.comparison_summary.text()
    assert "97 unchanged" in dialog.comparison_summary.text()
    assert "Unsaved" in dialog.comparison_context.toPlainText()
    assert dialog.change_list.topLevelItemCount() == 6
    assert threads[0] is not threading.current_thread()
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: pytest.fail("Row selection must not read files"))
    for index, change in enumerate(comparison.changes):
        dialog.change_list.setCurrentItem(dialog.change_list.topLevelItem(index))
        assert dialog.diff.toPlainText() == change.detail
    dialog.change_list.setCurrentItem(dialog.change_list.topLevelItem(1))
    dialog.diff.setFocus()
    before = dialog.diff.toPlainText()
    QTest.keyClicks(dialog.diff, "MUST NOT EDIT")
    assert dialog.diff.isReadOnly()
    assert dialog.diff.toPlainText() == before
    assert dialog.notes.isReadOnly()
    dialog._diff_highlighter.rehighlight()
    app.processEvents()
    block = dialog.diff.document().findBlockByNumber(3)
    assert block.text().startswith("-")
    formats = block.layout().formats()
    assert formats and formats[0].format.foreground().color().name() == "#ffb0ae"
    block = block.next()
    assert block.text().startswith("+")
    assert block.layout().formats()[0].format.foreground().color().name() == "#9bdcaa"


@pytest.mark.parametrize("version_case", ["same", "older"])
def test_same_and_older_releases_are_compared_without_enabling_installation(
    app, make_controller, release, monkeypatch, version_case,
):
    controller = make_controller()
    controller.open_dialog()
    version = controller.current_version if version_case == "same" else "0.1.0"
    release = replace(release, version=version, tag="v" + version)
    comparison = comparison_fixture(controller, release)
    resolved = []
    monkeypatch.setattr(ui, "resolve_release_package", lambda item, **_kwargs: resolved.append(item) or item)
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison)
    checked(controller, release, app, monkeypatch)
    assert resolved == [release]
    assert controller.comparison is comparison
    assert controller._comparison_archive is not None
    assert controller.state == ("current" if version_case == "same" else "ahead")
    assert version in controller._dialog.comparison_direction.text()
    assert controller._dialog.change_list.topLevelItemCount() == 6
    assert not controller._dialog.download_button.isEnabled()
    assert not controller.download()
    assert not controller.request_restart()


def test_comparison_cache_is_reverified_and_reused_without_a_second_transfer(
    app, make_controller, release, monkeypatch, tmp_path,
):
    import hashlib
    controller = make_controller()
    controller.open_dialog()
    archive = tmp_path / "verified-cache.zip"
    data = b"verified release package bytes"
    archive.write_bytes(data)
    release = replace(release, asset_size=len(data), sha256=hashlib.sha256(data).hexdigest())
    comparison = comparison_fixture(controller, release)
    calls = []
    verified = []
    prepared = []

    def download(item, _destination, *, session, **_kwargs):
        calls.append((item, session, threading.current_thread()))
        return archive

    actual_verify = ui.verify_cached_download

    def verify(*args, **kwargs):
        verified.append((args, kwargs["session"], threading.current_thread()))
        return actual_verify(*args, **kwargs)

    monkeypatch.setattr(ui, "download_update", download)
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison)
    monkeypatch.setattr(ui, "verify_cached_download", verify)
    monkeypatch.setattr(ui, "prepare_install", lambda *_args, **_kwargs: prepared.append(True))
    checked(controller, release, app, monkeypatch)
    assert len(calls) == 1
    assert prepared == []
    assert controller.downloaded_archive is None and controller.pending_update is None
    assert controller._comparison_archive == archive
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    assert len(calls) == 1
    assert calls[0][1] is controller.session
    assert calls[0][2] is not threading.current_thread()
    assert len(verified) == 1
    assert verified[0][0] == (release, archive)
    assert verified[0][1] is controller.session
    assert verified[0][2] is not threading.current_thread()
    assert controller.downloaded_archive == archive
    assert controller.state == "ready"
    assert prepared == []


@pytest.mark.parametrize("change", ["missing", "corrupt", "replaced"])
def test_missing_or_changed_comparison_cache_is_downloaded_and_verified_again(
    app, make_controller, release, monkeypatch, tmp_path, change,
):
    import hashlib
    controller = make_controller()
    archive = tmp_path / "checked-release.zip"
    replacement = tmp_path / "replacement-release.zip"
    data = b"correct package bytes"
    archive.write_bytes(data)
    replacement.write_bytes(data)
    release = replace(release, asset_size=len(data), sha256=hashlib.sha256(data).hexdigest())
    calls = []

    def download(*_args, **_kwargs):
        calls.append(True)
        return archive if len(calls) == 1 else replacement

    monkeypatch.setattr(ui, "download_update", download)
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison_fixture(controller, release))
    checked(controller, release, app, monkeypatch)
    if change == "missing":
        archive.unlink()
    elif change == "corrupt":
        archive.write_bytes(b"wrong content has different length")
    else:
        archive.write_bytes(b"x" * len(data))
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    assert len(calls) == 2
    assert controller.state == "ready"
    assert controller.downloaded_archive == replacement


def test_idle_token_replacement_for_same_account_disables_actions_and_discards_comparison(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison_fixture(controller, release))
    checked(controller, release, app, monkeypatch)
    assert controller._dialog.download_button.isEnabled()
    controller.session.token = "replacement-token-for-the-same-login"
    # Simulates an account changed by Studio's publishing panel; no updater
    # control was used. The controller watches the shared credential state.
    wait_until(app, lambda: controller.release is None, timeout=2)
    assert not controller._dialog.download_button.isEnabled()
    assert controller._dialog.check_button.isEnabled()
    assert controller.comparison is None and controller._comparison_archive is None
    assert controller._dialog.change_list.topLevelItemCount() == 0
    assert "connection changed" in controller.status_message
    assert "replacement-token-for-the-same-login" not in controller.status_message


def test_signout_disables_check_and_download_without_waiting_for_poll_timer(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()
    checked(controller, release, app, monkeypatch)
    controller.session.has_token = False
    assert not controller.download()
    assert not controller._dialog.check_button.isEnabled()
    assert not controller._dialog.download_button.isEnabled()
    assert not controller.download_available
    assert controller.release is None


@pytest.mark.parametrize("stage", ["transfer", "comparison"])
def test_comparison_failure_keeps_release_version_and_never_claims_no_changes(
    app, make_controller, release, monkeypatch, stage,
):
    controller = make_controller()
    controller.open_dialog()

    def fail(*_args, **_kwargs):
        if stage == "transfer":
            raise UpdateError("The release ZIP could not be downloaded.")
        raise ComparisonError("The installed file inventory could not be read.")

    monkeypatch.setattr(ui, "download_update" if stage == "transfer" else "compare_release_archive", fail)
    checked(controller, release, app, monkeypatch)
    assert controller.state == "available"
    assert controller._dialog.latest_version_label.text() == release.version
    assert "unavailable" in controller._dialog.comparison_summary.text().lower()
    assert "No application file differences" not in controller._dialog.comparison_summary.text()
    assert controller.comparison_error
    assert controller._comparison_archive is None
    assert controller.comparison is None
    assert controller.pending_update is None
    assert controller._dialog.download_button.isEnabled()


def test_fetched_release_is_visible_while_comparison_runs_without_enabling_download(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()
    started = threading.Event()
    finish = threading.Event()
    comparison = comparison_fixture(controller, release)

    def compare(*_args, cancelled, **_kwargs):
        started.set()
        while not finish.wait(0.005):
            if cancelled():
                raise ComparisonError("Cancelled", cancelled=True)
        return comparison

    monkeypatch.setattr(ui, "compare_release_archive", compare)
    monkeypatch.setattr(ui, "check_for_update", lambda _repo, current, **_kwargs: UpdateCheck(current, release))
    try:
        assert controller.check_for_updates()
        wait_until(app, lambda: started.is_set() and controller.release is not None)
        dialog = controller._dialog
        assert dialog.latest_version_label.text() == release.version
        assert dialog.notes.toPlainText() == release.notes
        assert "Reading and comparing" in dialog.comparison_summary.text()
        assert dialog.comparison_empty_state.isVisible()
        assert not dialog.download_button.isEnabled()
        assert not controller.download_available
        assert not controller.download()
        assert not controller.request_restart()
        finish.set()
        wait_until(app, lambda: not controller.is_active)
        assert dialog.change_list.topLevelItemCount() == len(comparison.changes)
        assert dialog.diff.toPlainText() == comparison.changes[0].detail
        assert dialog.download_button.isEnabled()
        assert not dialog.comparison_empty_state.isVisible()
    finally:
        finish.set()


@pytest.mark.parametrize("version_case", ["newer", "same", "older"])
def test_invalid_release_archive_shows_the_actual_reason_and_cannot_be_downloaded(
    app, make_controller, release, monkeypatch, version_case,
):
    controller = make_controller()
    controller.open_dialog()
    if version_case != "newer":
        version = controller.current_version if version_case == "same" else "0.1.0"
        release = replace(release, version=version, tag="v" + version)
    reason = "The release ZIP is missing app/package-manifest.json. Build a complete Studio update package."

    def compare(*_args, **_kwargs):
        raise ReleaseArchiveError(reason)

    monkeypatch.setattr(ui, "compare_release_archive", compare)
    monkeypatch.setattr(ui, "prepare_install", lambda *_args, **_kwargs: pytest.fail("Rejected release was prepared"))
    checked(controller, release, app, monkeypatch)
    dialog = controller._dialog
    assert controller.state == {"newer": "unavailable", "same": "current", "older": "ahead"}[version_case]
    assert dialog.latest_version_label.text() == release.version
    assert dialog.notes.toPlainText() == release.notes
    assert dialog.comparison_summary.text() == "File comparison unavailable"
    assert dialog.comparison_context.toPlainText() == reason
    assert "not accepted" in dialog.comparison_empty_state.text()
    assert dialog.comparison_empty_state.isVisible()
    assert not dialog.download_button.isEnabled()
    assert not controller.download_available
    assert not controller.download()
    assert not controller.request_restart()
    assert controller._comparison_archive is None
    assert controller.pending_update is None
    assert "could not be verified" in dialog.download_button.toolTip()


def test_successful_comparison_after_rejected_archive_restores_only_newer_download(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()

    def reject(*_args, **_kwargs):
        raise ReleaseArchiveError("The release package is incomplete.")

    monkeypatch.setattr(ui, "compare_release_archive", reject)
    checked(controller, release, app, monkeypatch)
    assert not controller.download_available
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison_fixture(controller, release))
    checked(controller, release, app, monkeypatch)
    assert controller.download_available
    assert controller._dialog.download_button.isEnabled()
    assert "3 modified" in controller._dialog.comparison_summary.text()
    assert controller._dialog.change_list.topLevelItemCount() == 6


def test_download_verification_failure_retires_previous_diff_but_preserves_release_details(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison_fixture(controller, release))
    checked(controller, release, app, monkeypatch)
    assert controller._dialog.change_list.topLevelItemCount() == 6
    assert controller._dialog.diff.toPlainText()
    monkeypatch.setattr(ui, "verify_cached_download", lambda *_args, **_kwargs: True)
    reason = "The downloaded package version does not match the reviewed release."

    def reject(*_args, **_kwargs):
        raise UpdateInstallError(reason)

    monkeypatch.setattr(ui, "validate_download_archive", reject)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    dialog = controller._dialog
    assert controller.state == "error"
    assert dialog.latest_version_label.text() == release.version
    assert dialog.notes.toPlainText() == release.notes
    assert reason in dialog.comparison_context.toPlainText()
    assert "Check for updates" in dialog.comparison_context.toPlainText()
    assert dialog.comparison_summary.text() == "File comparison unavailable"
    assert dialog.change_list.topLevelItemCount() == 0
    assert not dialog.diff.toPlainText()
    assert dialog.comparison_empty_state.isVisible()
    assert not dialog.download_button.isEnabled()
    assert not dialog.folder_button.isVisible()
    assert not controller.download_available
    assert not controller.request_restart()


def test_reconnecting_same_token_retires_account_scoped_release_and_diff(
    app, make_controller, release, monkeypatch,
):
    session = Session(connected=True)
    session._generation = 1
    controller = make_controller(session=session)
    controller.open_dialog()
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison_fixture(controller, release))
    checked(controller, release, app, monkeypatch)
    assert controller.download_available
    session._generation += 1
    controller._refresh_connection()
    assert controller.release is None
    assert controller.comparison is None
    assert controller._dialog.change_list.topLevelItemCount() == 0
    assert not controller.download_available
    assert not controller._dialog.download_button.isEnabled()
    assert "connection changed" in controller._dialog.comparison_context.toPlainText()
    assert "No application file differences" not in controller._dialog.comparison_summary.text()


def test_no_changes_is_shown_only_after_a_completed_comparison(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()
    assert "No application file differences" not in controller._dialog.comparison_summary.text()
    comparison = ReleaseComparison(controller.current_version, release.version, (), 125)
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison)
    checked(controller, release, app, monkeypatch)
    dialog = controller._dialog
    assert "No application file differences" in dialog.comparison_summary.text()
    assert "125 unchanged" in dialog.comparison_summary.text()
    assert "files match this release" in dialog.comparison_empty_state.text()
    assert dialog.comparison_empty_state.isVisible()
    assert dialog.download_button.isEnabled()


def test_release_without_a_package_cannot_enable_download_or_fake_file_comparison(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()
    release = replace(release, asset_id=0, asset_name="", asset_size=0, sha256="")
    checked(controller, release, app, monkeypatch)
    assert controller.state == "unavailable"
    assert controller.comparison is None
    assert controller._comparison_archive is None
    assert not controller._dialog.download_button.isEnabled()
    assert "No complete application ZIP" in controller._dialog.comparison_context.toPlainText()
    assert "unavailable" in controller._dialog.comparison_summary.text().lower()


def test_cancel_during_file_comparison_discards_cache_and_never_enables_download(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()
    started = threading.Event()
    stopped = threading.Event()

    def compare(*_args, cancelled, **_kwargs):
        started.set()
        while not cancelled():
            time.sleep(0.003)
        stopped.set()
        raise ComparisonError("Cancelled", cancelled=True)

    monkeypatch.setattr(ui, "compare_release_archive", compare)
    monkeypatch.setattr(ui, "check_for_update", lambda _repo, current, **_kwargs: UpdateCheck(current, release))
    assert controller.check_for_updates()
    wait_until(app, lambda: started.is_set() and controller.release is not None)
    assert controller.state == "checking"
    assert not controller._dialog.download_button.isEnabled()
    controller.cancel()
    wait_until(app, lambda: not controller.is_active)
    assert stopped.is_set()
    assert controller.release == release
    assert controller._dialog.latest_version_label.text() == release.version
    assert controller._dialog.comparison_summary.text() == "File comparison cancelled"
    assert "Choose Check for updates" in controller._dialog.comparison_context.toPlainText()
    assert controller.comparison is None
    assert controller._comparison_archive is None
    assert not controller._dialog.download_button.isEnabled()
    assert not controller.request_restart()


def test_raw_exception_during_comparison_never_displays_tokens_or_signed_urls(
    app, make_controller, release, monkeypatch,
):
    controller = make_controller()
    controller.open_dialog()

    def compare(*_args, **_kwargs):
        raise RuntimeError("Authorization: Bearer test-secret-value https://signed.example/?token=test-secret-value")

    monkeypatch.setattr(ui, "compare_release_archive", compare)
    checked(controller, release, app, monkeypatch)
    assert controller.state == "available"
    assert controller.comparison_error
    assert "test-secret-value" not in controller.comparison_error
    assert "test-secret-value" not in controller.status_message
    assert "test-secret-value" not in controller._dialog.comparison_context.toPlainText()


@pytest.mark.parametrize("connected", [True, False])
def test_account_change_after_restart_request_cannot_launch_helper(
    app, make_controller, release, monkeypatch, tmp_path, connected,
):
    controller = make_controller()
    checked(controller, release, app, monkeypatch)
    archive = tmp_path / "download.zip"
    archive.write_bytes(b"package")
    prepared = PreparedUpdate(tmp_path / "job", tmp_path / "job.json", release.version, release.repository, archive, ())
    controller._download_ready(ui._DownloadedUpdate(archive, prepared))
    assert controller.request_restart()
    controller.begin_shutdown()
    assert not controller._account_timer.isActive()
    controller.session.has_token = connected
    controller.session.token = "a_different_token_while_closing"
    launches = []
    monkeypatch.setattr(ui, "launch_prepared_update", launches.append)
    with pytest.raises(UpdateInstallError, match="GitHub connection changed"):
        controller.launch_pending_update()
    assert launches == []


def owned_package_factory(controller, release, monkeypatch, tmp_path):
    controller._download_cache = tmp_path / "update-cache" / "downloads"
    archives = []

    def download(*_args, **_kwargs):
        directory = controller._download_cache / f"studio-download-test{len(archives)}"
        directory.mkdir(parents=True)
        archive = directory / release.asset_name
        archive.write_bytes(b"package")
        archives.append(archive)
        return archive

    monkeypatch.setattr(ui, "download_update", download)
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison_fixture(controller, release))
    return archives


def test_recheck_and_shutdown_remove_only_their_comparison_cache(
    app, make_controller, release, monkeypatch, tmp_path,
):
    controller = make_controller()
    archives = owned_package_factory(controller, release, monkeypatch, tmp_path)
    checked(controller, release, app, monkeypatch)
    first = archives[0]
    assert first.is_file()
    checked(controller, release, app, monkeypatch)
    assert not first.exists()
    assert not first.parent.exists()
    second = archives[1]
    assert second.is_file()
    controller.begin_shutdown()
    assert not second.exists()
    assert not second.parent.exists()
    assert controller._download_cache.is_dir()


def test_comparison_failure_removes_the_owned_temporary_zip(
    app, make_controller, release, monkeypatch, tmp_path,
):
    controller = make_controller()
    archives = owned_package_factory(controller, release, monkeypatch, tmp_path)

    def compare(*_args, **_kwargs):
        raise ComparisonError("This archive could not be compared.")

    monkeypatch.setattr(ui, "compare_release_archive", compare)
    checked(controller, release, app, monkeypatch)
    assert controller.comparison_error
    assert not archives[0].exists()
    assert controller._comparison_archive is None


def test_cancelled_result_before_qt_delivery_removes_the_owned_comparison_zip(
    app, make_controller, release, monkeypatch, tmp_path,
):
    controller = make_controller()
    archives = owned_package_factory(controller, release, monkeypatch, tmp_path)
    monkeypatch.setattr(ui, "check_for_update", lambda _repo, current, **_kwargs: UpdateCheck(current, release))
    assert controller.check_for_updates()
    assert controller._worker.wait(1500)
    assert archives[0].is_file()
    # The worker has finished, but Qt has not delivered completed/finished yet.
    controller.cancel()
    wait_until(app, lambda: not controller.is_active)
    assert not archives[0].exists()
    assert controller.release is None
    assert controller.comparison is None


def test_cache_used_by_active_preparation_is_cleaned_only_after_worker_drains(
    app, make_controller, release, monkeypatch, tmp_path,
):
    controller = make_controller()
    archives = owned_package_factory(controller, release, monkeypatch, tmp_path)
    checked(controller, release, app, monkeypatch)
    archive = archives[0]
    started = threading.Event()
    finish = threading.Event()
    monkeypatch.setattr(ui, "verify_cached_download", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ui, "installation_support", lambda _root: (True, ""))

    def prepare(*_args, cancelled, **_kwargs):
        started.set()
        finish.wait(2)
        assert archive.is_file()
        raise UpdateInstallError("Cancelled", cancelled=cancelled())

    monkeypatch.setattr(ui, "prepare_install", prepare)
    assert controller.download()
    wait_until(app, started.is_set)
    controller.session.token = "replacement_token_for_offline_tests"
    controller._refresh_connection()
    assert archive.is_file()
    finish.set()
    wait_until(app, lambda: not controller.is_active)
    assert not archive.exists()
    assert controller.pending_update is None


def test_explicitly_downloaded_manual_archive_survives_later_check_and_shutdown(
    app, make_controller, release, monkeypatch, tmp_path,
):
    controller = make_controller()
    archives = owned_package_factory(controller, release, monkeypatch, tmp_path)
    checked(controller, release, app, monkeypatch)
    monkeypatch.setattr(ui, "verify_cached_download", lambda *_args, **_kwargs: True)
    assert controller.download()
    wait_until(app, lambda: not controller.is_active)
    manual = archives[0]
    assert controller.downloaded_archive == manual
    checked(controller, release, app, monkeypatch)
    assert manual.is_file()
    controller.begin_shutdown()
    assert manual.is_file()
    assert not archives[1].exists()


def test_comparison_cleanup_does_not_remove_other_files_or_recurse(tmp_path):
    cache = tmp_path / "downloads"
    private = cache / "studio-download-owned"
    private.mkdir(parents=True)
    archive = private / "release.zip"
    archive.write_bytes(b"temporary comparison")
    unrelated = private / "user-notes.txt"
    unrelated.write_text("keep me")
    outside = tmp_path / "user.zip"
    outside.write_bytes(b"keep me too")
    ui._remove_comparison_download(outside, cache)
    assert outside.is_file()
    ui._remove_comparison_download(archive, cache)
    assert not archive.exists()
    assert unrelated.read_text() == "keep me"
    assert private.is_dir()


def test_many_comparison_warnings_scroll_without_hiding_update_actions(
    app, make_controller, release, monkeypatch,
):
    from types import SimpleNamespace
    from PySide6.QtCore import QRect
    monkeypatch.setattr(ui.UpdateDialog, "screen", lambda _self: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1366, 728)))
    controller = make_controller()
    controller.open_dialog()
    warnings = tuple(f"File {index}: " + "long application path/" * 15 + "could not be read." for index in range(100))
    comparison = replace(comparison_fixture(controller, release), warnings=warnings)
    monkeypatch.setattr(ui, "compare_release_archive", lambda *_args, **_kwargs: comparison)
    checked(controller, release, app, monkeypatch)
    dialog = controller._dialog
    app.processEvents()
    assert dialog.height() <= 688
    assert dialog.comparison_context.height() == 64
    assert dialog.comparison_context.isReadOnly()
    assert "File 99:" in dialog.comparison_context.toPlainText()
    assert dialog.comparison_context.verticalScrollBar().maximum() > 0
    assert dialog.download_button.geometry().bottom() < dialog.height()
    assert dialog.check_button.isVisible() and dialog.download_button.isVisible()
    assert dialog.download_button.isEnabled()
