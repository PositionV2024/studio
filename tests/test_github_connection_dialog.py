import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit

import studio.publish_dialog as publishing_ui
from studio.github_connection import GitHubError
from studio.publish_dialog import GitHubConnectDialog, PublishDialog


class Session:
    storage_available = False
    storage_warning = ""
    remembered = False
    account = None
    has_token = False

    def __init__(self, *, storage_available=False):
        self.storage_available = storage_available
        self.connections = []
        self.on_connect = None

    def connect(self, token, *, remember=False, cancelled=None):
        self.connections.append((token, remember))
        if self.on_connect:
            self.on_connect(cancelled)
        self.has_token = True
        self.account = {"login": "clarence", "id": 123}
        self.remembered = remember
        return self.account

    def disconnect(self):
        self.has_token = False
        self.account = None
        self.remembered = False


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_until(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("GitHub dialog operation did not finish")


@pytest.fixture(autouse=True)
def clean_dialogs(app):
    yield
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, (GitHubConnectDialog, PublishDialog)):
            widget.reject()
            if widget._worker is not None:
                wait_until(app, lambda: widget._worker is None)
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_connection_form_masks_token_and_opens_only_requested_browser(app, monkeypatch):
    opened = []
    monkeypatch.setattr(publishing_ui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    session = Session()
    dialog = GitHubConnectDialog(session, "clarence/project")
    dialog.show()
    assert session.connections == [] and opened == []
    assert dialog.token_input.echoMode() == QLineEdit.EchoMode.Password
    assert not dialog.connect_button.isEnabled()
    assert not dialog.remember_checkbox.isEnabled()
    assert not dialog.remember_checkbox.isChecked()
    dialog.open_github_button.click()
    parsed = urlparse(opened[0])
    assert parsed.scheme == "https" and parsed.netloc == "github.com"
    assert parsed.path == "/settings/personal-access-tokens/new"
    query = parse_qs(parsed.query)
    assert query["contents"] == ["write"]
    assert query["target_name"] == ["clarence"]
    assert "administration" not in query and "workflows" not in query
    assert "Only select repositories" in dialog.instructions_label.text()
    assert session.connections == []


def test_browser_failure_provides_token_creation_address(app, monkeypatch):
    monkeypatch.setattr(publishing_ui.QDesktopServices, "openUrl", lambda _url: False)
    dialog = GitHubConnectDialog(Session(), "clarence/project")
    dialog.open_github_button.click()
    assert "Could not open your browser" in dialog.status_label.text()
    assert "https://github.com/settings/personal-access-tokens/new" in dialog.status_label.text()


def test_new_repository_and_workflow_requirements_match_browser_form(app):
    dialog = GitHubConnectDialog(Session(storage_available=True), "clarence/new", new_repository=True, workflows=True)
    query = parse_qs(urlparse(dialog._setup_url).query)
    assert query["administration"] == ["write"]
    assert query["workflows"] == ["write"]
    assert "All repositories" in dialog.instructions_label.text()
    assert "Administration" in dialog.instructions_label.text()
    assert "Workflows" in dialog.instructions_label.text()
    assert dialog.remember_checkbox.isEnabled() and dialog.remember_checkbox.isChecked()
    assert "Windows Credential Manager" in dialog.storage_label.text()


def test_connect_validates_off_thread_clears_field_and_waits_for_worker(app):
    session = Session(storage_available=True)
    threads = []
    session.on_connect = lambda _cancelled: threads.append(threading.current_thread())
    dialog = GitHubConnectDialog(session, "clarence/project")
    dialog.show()
    dialog.token_input.setText("test-only-sensitive-value")
    assert dialog.connect_button.isEnabled()
    dialog.connect_button.click()
    assert dialog.token_input.text() == ""
    wait_until(app, lambda: dialog._worker is None)
    assert threads and threads[0] is not threading.current_thread()
    assert session.connections == [("test-only-sensitive-value", True)]
    assert session.account["login"] == "clarence"
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert not dialog.isVisible()
    assert "test-only-sensitive-value" not in dialog.status_label.text()


def test_invalid_token_keeps_form_available_for_retry(app):
    session = Session()

    def invalid(_cancelled):
        raise GitHubError("The access token is invalid or expired.", status=401)

    session.on_connect = invalid
    dialog = GitHubConnectDialog(session)
    dialog.show()
    dialog.token_input.setText("test-invalid-value")
    dialog.connect_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert dialog.isVisible()
    assert "invalid or expired" in dialog.status_label.text()
    assert dialog.token_input.text() == ""
    assert not session.has_token
    session.on_connect = None
    dialog.token_input.setText("test-valid-value")
    dialog.connect_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_unexpected_auth_exception_does_not_display_secret(app):
    session = Session()

    def unexpected(_cancelled):
        raise RuntimeError("request header Bearer test-secret-value")

    session.on_connect = unexpected
    dialog = GitHubConnectDialog(session)
    dialog.show()
    dialog.token_input.setText("test-secret-value")
    dialog.connect_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert "Could not connect to GitHub" in dialog.status_label.text()
    assert "test-secret-value" not in dialog.status_label.text()


def test_close_cancels_connection_without_blocking_the_gui(app):
    session = Session()

    def waiting(cancelled):
        while not cancelled.wait(0.01):
            pass
        raise GitHubError("The connection was cancelled.", cancelled=True)

    session.on_connect = waiting
    dialog = GitHubConnectDialog(session)
    dialog.show()
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start()
    dialog.token_input.setText("test-only-value")
    dialog.connect_button.click()
    wait_until(app, lambda: len(ticks) >= 4)
    dialog.close()
    wait_until(app, lambda: dialog._worker is None and not dialog.isVisible())
    timer.stop()
    assert not session.has_token
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.token_input.text() == ""


def test_publishing_has_no_install_links_or_implicit_auth(app, tmp_path, monkeypatch):
    session = Session()
    monkeypatch.setattr(publishing_ui, "get_session", lambda: session)
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    text = "\n".join(label.text() for label in dialog.github_page.findChildren(QLabel))
    assert "Install Git" not in text
    assert "GitHub CLI" not in text
    assert session.connections == []
    assert dialog.github_login_button.text() == "Sign in with GitHub…"
    assert dialog.github_disconnect_button.isHidden()


def test_login_connects_same_session_and_reports_secure_storage_failure(app, tmp_path, monkeypatch):
    session = Session()
    session.storage_warning = "The connection is available for this session only."
    monkeypatch.setattr(publishing_ui, "get_session", lambda: session)
    opened = []

    def connect_dialog(dialog):
        opened.append(dialog._session)
        session.connect("test-only-value")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(GitHubConnectDialog, "exec", connect_dialog)
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog.github_login_button.click()
    assert opened == [session]
    assert "Connected as clarence" in dialog.github_account_label.text()
    assert "session only" in dialog.status_label.text()
    assert not dialog.github_disconnect_button.isHidden()
    assert "test-only-value" not in dialog.operation_log.toPlainText()
    dialog.github_disconnect_button.click()
    assert not session.has_token
    assert dialog.github_disconnect_button.isHidden()
    assert not dialog.publish_button.isEnabled()
    assert "disconnected" in dialog.status_label.text()


def test_saved_account_is_not_claimed_verified_before_remote_review(app, tmp_path, monkeypatch):
    session = Session(storage_available=True)
    session.has_token = True
    session.remembered = True
    monkeypatch.setattr(publishing_ui, "get_session", lambda: session)
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    assert "verified when you review" in dialog.github_account_label.text()
    assert session.connections == []
    assert dialog.github_login_button.text() == "Change connection…"
