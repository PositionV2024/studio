import json
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit

import studio.publish_dialog as publishing_ui
from studio.publish_dialog import PublishDialog
from studio.github_connection import GitHubError
from studio.github_repositories import GitHubRepository, RepositoryList
from studio.publishing import PreparedPublish, ProjectScan, PublishFile, PublishResult


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def github_session(monkeypatch):
    class Session:
        has_token = True
        account = {"id": 123, "login": "clarence"}
        remembered = False
        storage_warning = ""
        storage_available = False
        _generation = 0
        token = "test-only-publishing-session"

        def get_token(self):
            if not self.has_token:
                raise GitHubError("Connect GitHub.")
            return self.token

        def client(self, **_kwargs):
            return object()

        def disconnect(self):
            self._generation += 1
            self.has_token = False
            self.account = None

    session = Session()
    monkeypatch.setattr(publishing_ui, "get_session", lambda: session)
    monkeypatch.setattr(publishing_ui, "list_owned_repositories", lambda *_args, **_kwargs: repositories())
    return session


def repositories(*items):
    if not items:
        items = (GitHubRepository(12, "clarence/project", True),)
    return RepositoryList(123, "clarence", tuple(items))


@pytest.fixture(autouse=True)
def clean_dialogs(app):
    yield
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, PublishDialog):
            widget.reject()
            if widget._worker is not None or widget._repository_worker is not None:
                wait_until(app, lambda: widget._worker is None and widget._repository_worker is None)
            widget.deleteLater()
    QApplication.clipboard().clear()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def wait_until(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("Qt operation did not finish")


def sample_scan(root):
    return ProjectScan(root, (PublishFile("app.py", 5, "test-hash"),), ((".env", "Credentials"),))


def prepared(root):
    temporary = tempfile.TemporaryDirectory(prefix="studio-dialog-test-")
    return PreparedPublish(
        root, "clarence/project", "clarence", "private", True, "main", "", "a" * 40,
        sample_scan(root).files, "+print('reviewed')", "1 file changed", Path(temporary.name), temporary,
    )


def test_tabs_select_expected_target_without_auth_or_publish(app, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(publishing_ui, "prepare_github_publish", lambda *_args, **_kwargs: called.append("prepare"))
    monkeypatch.setattr(publishing_ui, "publish_to_github", lambda *_args, **_kwargs: called.append("publish"))
    for target, index in (("github", 0), ("vercel", 1), ("streamlit", 2)):
        dialog = PublishDialog(tmp_path, "Example", target, auto_scan=False)
        assert dialog.target_tabs.currentIndex() == index
        assert not dialog.publish_button.isEnabled()
        assert dialog.github_visibility.currentText() == "Private"
        dialog.reject()
    app.processEvents()
    assert called == []


def test_automatic_scan_reads_files_without_github_account_access(app, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(publishing_ui, "scan_project", lambda root, runner: sample_scan(root))
    monkeypatch.setattr(publishing_ui, "prepare_github_publish", lambda *_args, **_kwargs: called.append("account"))
    dialog = PublishDialog(tmp_path, "Example")
    dialog.show()
    wait_until(app, lambda: dialog._scan is not None and dialog._worker is None)
    assert dialog.file_list.count() == 1
    assert "Credentials" in dialog.excluded_files.toPlainText()
    assert dialog.review_button.isEnabled()
    assert not dialog.publish_button.isEnabled()
    dialog.reject()
    assert called == []


def test_review_enables_explicit_publish_and_shows_exact_account_destination_diff(app, tmp_path, monkeypatch):
    plan = prepared(tmp_path)
    publications = []
    monkeypatch.setattr(publishing_ui, "prepare_github_publish", lambda *_args, **_kwargs: plan)

    def publish(reviewed, runner, **_kwargs):
        publications.append(reviewed)
        return PublishResult("clarence/project", "main", "a" * 40,
                             "https://github.com/clarence/project", "https://github.com/clarence/project/tree/main", True)

    monkeypatch.setattr(publishing_ui, "publish_to_github", publish)
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog._files_ready(sample_scan(tmp_path))
    dialog.review_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert dialog.publish_button.isEnabled()
    assert "clarence/project" in dialog.review_summary.text()
    assert "Signed in as: clarence" in dialog.review_summary.text()
    assert "private repository" in dialog.review_summary.text()
    assert "+print('reviewed')" in dialog.review_diff.toPlainText()
    assert publications == []
    dialog.publish_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert publications == [plan]
    assert not dialog.publish_button.isEnabled()
    assert dialog.vercel_repository.text() == "clarence/project"
    assert dialog.streamlit_entry_link.text().startswith("https://github.com/clarence/project/blob/main/")
    assert "Published to clarence/project" in dialog.status_label.text()
    assert not plan.working_path.exists()
    dialog.reject()


def test_changing_destination_or_selection_requires_a_new_review(app, tmp_path):
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog._files_ready(sample_scan(tmp_path))
    plan = prepared(tmp_path)
    dialog._publish_prepared(plan)
    dialog._update_buttons()
    assert dialog.publish_button.isEnabled()
    dialog.github_repository.setText("clarence/different")
    assert not dialog.publish_button.isEnabled()
    assert not plan.working_path.exists()
    another = prepared(tmp_path)
    dialog._publish_prepared(another)
    dialog._update_buttons()
    dialog.file_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert not dialog.publish_button.isEnabled()
    assert not dialog.review_button.isEnabled()
    assert not another.working_path.exists()
    dialog.reject()


def test_network_wait_keeps_gui_responsive_and_close_cancels_worker(app, tmp_path):
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog.show()
    heartbeats = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: heartbeats.append(True))
    timer.start()

    def slow_operation(runner):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            runner.check_cancelled()
            time.sleep(0.01)

    dialog._start_worker(slow_operation, lambda _result: None)
    wait_until(app, lambda: len(heartbeats) >= 5)
    assert dialog.isVisible()
    dialog.reject()
    wait_until(app, lambda: dialog._worker is None and not dialog.isVisible())
    timer.stop()
    assert len(heartbeats) >= 5
    assert "cancelled" in dialog.operation_log.toPlainText().lower()


def test_failed_publish_keeps_error_and_review_available(app, tmp_path, monkeypatch):
    from studio.publishing import PublishError
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog._files_ready(sample_scan(tmp_path))
    plan = prepared(tmp_path)
    dialog._publish_prepared(plan)
    dialog._update_buttons()

    def fail(*_args, **_kwargs):
        raise PublishError("The connection failed; no upload completed.")

    monkeypatch.setattr(publishing_ui, "publish_to_github", fail)
    dialog.publish_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert "connection failed" in dialog.status_label.text()
    assert "connection failed" in dialog.operation_log.toPlainText()
    assert dialog.publish_button.isEnabled()
    assert plan.working_path.exists()
    dialog.reject()
    assert not plan.working_path.exists()


def test_vercel_has_prepare_step_then_real_browser_handoff_without_claiming_deployment(app, tmp_path, monkeypatch):
    studio = tmp_path / ".studio"
    studio.mkdir()
    (studio / "project.json").write_text(json.dumps({"project_type": "streamlit"}))
    opened = []
    monkeypatch.setattr(publishing_ui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    dialog = PublishDialog(tmp_path, "Example", "vercel", auto_scan=False)
    assert dialog.vercel_compatibility_label.text() == "Streamlit app"
    assert not dialog.container_prepare_button.isHidden()
    dialog.vercel_repository.setText("clarence/project")
    assert not dialog.vercel_open_button.isEnabled()
    (tmp_path / "Dockerfile.vercel").write_text("FROM python:3.12-slim\n")
    dialog._update_compatibility()
    assert "Beta" in dialog.vercel_compatibility_label.text()
    assert dialog.vercel_open_button.isEnabled()
    assert opened == []
    dialog.vercel_open_button.click()
    assert opened == ["https://vercel.com/new"]
    assert "import opened" in dialog.status_label.text()
    assert "deployed successfully" not in dialog.status_label.text().lower()
    dialog.vercel_copy_button.click()
    assert "Repository: https://github.com/clarence/project" in QApplication.clipboard().text()
    dialog.reject()


def test_streamlit_handoff_copies_actual_branch_and_entry_and_reports_browser_failure(app, tmp_path, monkeypatch):
    dialog = PublishDialog(tmp_path, "Example", "streamlit", auto_scan=False)
    assert not dialog.streamlit_open_button.isEnabled()
    dialog.streamlit_repository.setText("clarence/project")
    dialog.streamlit_branch.setText("studio/update-one")
    dialog.streamlit_entry.setText("pages/my app.py")
    dialog.streamlit_copy_button.click()
    assert QApplication.clipboard().text() == "https://github.com/clarence/project/blob/studio%2Fupdate-one/pages/my%20app.py"
    monkeypatch.setattr(publishing_ui.QDesktopServices, "openUrl", lambda _url: False)
    dialog.streamlit_open_button.click()
    assert "Could not open your browser" in dialog.status_label.text()
    assert "https://share.streamlit.io/" in dialog.status_label.text()
    dialog.reject()


def test_real_container_dialog_reviews_complete_files_before_adding_them(app, tmp_path, monkeypatch):
    studio = tmp_path / ".studio"
    studio.mkdir()
    (studio / "project.json").write_text(json.dumps({"project_type": "streamlit", "entry_point": "app.py"}))
    (tmp_path / "app.py").write_text("import streamlit as st\nst.title('Example')\n")
    (tmp_path / "requirements.txt").write_text("streamlit\n")
    reviewed = []

    def accept_review(review):
        assert review.windowTitle() == "Review Vercel deployment files"
        assert not (tmp_path / "Dockerfile.vercel").exists()
        text = review.findChild(QPlainTextEdit).toPlainText()
        assert "FILE: Dockerfile.vercel" in text
        assert "FILE: .dockerignore" in text
        assert "FILE: requirements-vercel.txt" in text
        assert "FILE: studio_vercel_start.py" in text
        reviewed.append(text)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(publishing_ui.QDialog, "exec", accept_review)
    dialog = PublishDialog(tmp_path, "Example", "vercel", auto_scan=False)
    dialog.container_prepare_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert reviewed
    assert (tmp_path / "Dockerfile.vercel").exists()
    assert (tmp_path / "studio_vercel_start.py").exists()
    files = {dialog.file_list.item(index).data(Qt.ItemDataRole.UserRole).path for index in range(dialog.file_list.count())}
    assert {"Dockerfile.vercel", ".dockerignore", "requirements-vercel.txt", "studio_vercel_start.py"} <= files
    assert dialog.vercel_compatibility_label.text() == "Container Runtime (Beta)"
    dialog.reject()


def test_cancelled_container_review_and_wrong_root_do_not_write_files(app, tmp_path, monkeypatch):
    studio = tmp_path / ".studio"
    studio.mkdir()
    (studio / "project.json").write_text(json.dumps({"project_type": "streamlit"}))
    (tmp_path / "app.py").write_text("import streamlit\n")
    monkeypatch.setattr(publishing_ui.QDialog, "exec", lambda _review: QDialog.DialogCode.Rejected)
    dialog = PublishDialog(tmp_path, "Example", "vercel", auto_scan=False)
    dialog.container_prepare_button.click()
    assert not (tmp_path / "Dockerfile.vercel").exists()
    nested = tmp_path / "frontend" / ".studio"
    nested.mkdir(parents=True)
    (nested / "project.json").write_text(json.dumps({"project_type": "streamlit"}))
    dialog.vercel_root.setText("frontend")
    assert dialog.container_prepare_button.isHidden()
    assert "use this project's root" in dialog.vercel_compatibility_details.text().lower()
    dialog._prepare_container()
    assert "root (.)" in dialog.status_label.text()
    assert not (tmp_path / "Dockerfile.vercel").exists()
    assert not (tmp_path / "frontend" / "Dockerfile.vercel").exists()
    dialog.reject()


def test_vercel_entrypoint_is_visible_before_preparation_and_synced_with_cloud(app, tmp_path):
    studio = tmp_path / ".studio"
    studio.mkdir()
    (studio / "project.json").write_text(json.dumps({
        "project_type": "streamlit", "entry_point": "pages/start.py",
    }))
    dialog = PublishDialog(tmp_path, "Example", "vercel", auto_scan=False)
    assert not dialog.vercel_entry.isHidden()
    assert dialog.vercel_entry.text() == "pages/start.py"
    assert dialog.streamlit_entry.text() == "pages/start.py"
    dialog.vercel_entry.setText("apps/deploy.py")
    assert dialog.streamlit_entry.text() == "apps/deploy.py"
    dialog.streamlit_entry.setText("app.py")
    assert dialog.vercel_entry.text() == "app.py"
    dialog.reject()


def select_repository(dialog, name):
    combo = dialog.github_existing_repository
    for index in range(combo.count()):
        item = combo.itemData(index)
        if isinstance(item, GitHubRepository) and item.full_name == name:
            combo.setCurrentIndex(index)
            return item
    raise AssertionError(f"Repository is absent: {name}")


def existing_dialog(app, tmp_path):
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog._files_ready(sample_scan(tmp_path))
    dialog.github_mode.setCurrentIndex(1)
    wait_until(app, lambda: dialog._repository_worker is None and not dialog._repository_request_pending)
    return dialog


def test_new_mode_has_name_and_visibility_but_never_lists_remote_repositories(app, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(publishing_ui, "list_owned_repositories", lambda *_args, **_kwargs: calls.append(True))
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog.show()
    dialog.github_repository.setText("example")
    dialog.github_visibility.setCurrentIndex(1)
    app.processEvents()
    assert dialog.github_repository.isVisible()
    assert dialog.github_visibility.isVisible()
    assert dialog.github_visibility_label.isVisible()
    assert dialog.github_existing_repository_row.isHidden()
    assert dialog.github_existing_repository_label.isHidden()
    assert calls == []


def test_existing_mode_hides_entire_new_visibility_row_and_requires_explicit_choice(app, tmp_path):
    dialog = existing_dialog(app, tmp_path)
    dialog.show()
    assert dialog.github_repository.isHidden()
    assert dialog.github_repository_label.isHidden()
    assert dialog.github_visibility.isHidden()
    assert dialog.github_visibility_label.isHidden()
    assert dialog.github_existing_repository.isVisible()
    assert not dialog.github_existing_repository.isEditable()
    assert dialog.github_existing_repository.count() == 2
    assert dialog._existing_repository() is None
    assert not dialog.review_button.isEnabled()
    assert not dialog.publish_button.isEnabled()
    select_repository(dialog, "clarence/project")
    assert dialog.review_button.isEnabled()
    assert "Private repository" in dialog.github_repository_status.text()
    dialog.github_mode.setCurrentIndex(0)
    assert dialog.github_repository.isVisible()
    assert dialog.github_visibility.isVisible()
    assert dialog.github_visibility_label.isVisible()
    assert dialog.github_existing_repository_row.isHidden()
    assert dialog.github_visibility.currentText() == "Private"


def test_loading_repositories_keeps_file_selection_and_gui_responsive(app, tmp_path, monkeypatch):
    release = threading.Event()
    threads = []

    def waiting(_session, *, cancelled, progress):
        threads.append(threading.current_thread())
        progress("Reading another page of repositories…")
        while not release.wait(0.01):
            if cancelled.is_set():
                raise GitHubError("Cancelled.", cancelled=True)
        return repositories()

    monkeypatch.setattr(publishing_ui, "list_owned_repositories", waiting)
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog._files_ready(sample_scan(tmp_path))
    dialog.show()
    dialog.github_mode.setCurrentIndex(1)
    wait_until(app, lambda: bool(threads))
    assert threads[0] is not threading.current_thread()
    assert dialog.file_list.isEnabled()
    assert dialog.select_none_button.isEnabled()
    assert dialog.refresh_files_button.isEnabled()
    assert not dialog.github_existing_repository.isEnabled()
    assert not dialog.review_button.isEnabled()
    assert not dialog.github_refresh_repositories.isEnabled()
    dialog.select_none_button.click()
    assert dialog._selected_files() == ()
    dialog.select_all_button.click()
    assert len(dialog._selected_files()) == 1
    ticks = []
    QTimer.singleShot(1, lambda: ticks.append(True))
    wait_until(app, lambda: bool(ticks))
    release.set()
    wait_until(app, lambda: dialog._repository_worker is None)
    assert dialog.github_existing_repository.isEnabled()
    assert not dialog.progress_bar.isVisible()


def test_archived_disabled_and_read_only_repositories_are_visible_but_unselectable(app, tmp_path, monkeypatch):
    listed = repositories(
        GitHubRepository(1, "clarence/archived", True, archived=True),
        GitHubRepository(2, "clarence/disabled", True, disabled=True),
        GitHubRepository(3, "clarence/readonly", False, can_push=False),
        GitHubRepository(4, "clarence/usable", False, can_push=None),
    )
    monkeypatch.setattr(publishing_ui, "list_owned_repositories", lambda *_args, **_kwargs: listed)
    dialog = existing_dialog(app, tmp_path)
    for index in (1, 2, 3):
        item = dialog.github_existing_repository.model().item(index)
        assert not item.isEnabled()
        # Method guards still reject programmatic selection of a disabled row.
        dialog.github_existing_repository.setCurrentIndex(index)
        assert not dialog.review_button.isEnabled()
        dialog._prepare_publish()
        assert dialog._worker is None
    select_repository(dialog, "clarence/usable")
    assert dialog.review_button.isEnabled()
    assert "Public repository" in dialog.github_repository_status.text()


@pytest.mark.parametrize("private", [False, True])
def test_review_passes_selected_full_name_and_remote_visibility_not_hidden_preference(app, tmp_path, monkeypatch, private):
    listed = repositories(GitHubRepository(99, "clarence/destination", private))
    monkeypatch.setattr(publishing_ui, "list_owned_repositories", lambda *_args, **_kwargs: listed)
    calls = []
    plan = replace(prepared(tmp_path), repository="clarence/destination", visibility="private" if private else "public",
                   new_repository=False, repository_id=99, account_id=123, owner_id=123, owner_type="User")

    def prepare(*args, **kwargs):
        calls.append((args, kwargs))
        return plan

    monkeypatch.setattr(publishing_ui, "prepare_github_publish", prepare)
    monkeypatch.setattr(publishing_ui, "publish_to_github", lambda *_args, **_kwargs: pytest.fail("Selection/review must not publish."))
    dialog = existing_dialog(app, tmp_path)
    dialog.github_visibility.setCurrentText("Public" if private else "Private")
    dialog.github_repository.setText("wrong-owner/wrong-repository")
    select_repository(dialog, "clarence/destination")
    dialog.review_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert calls[0][0][2] == "clarence/destination"
    assert calls[0][1]["new_repository"] is False
    assert calls[0][1]["visibility"] == ("private" if private else "public")
    assert "client" in calls[0][1]
    assert dialog.publish_button.isEnabled()
    assert dialog._plan is plan


@pytest.mark.parametrize("retained_id", [12, 999, None])
def test_refresh_retains_only_exact_selected_repository_identity(app, tmp_path, monkeypatch, retained_id):
    dialog = existing_dialog(app, tmp_path)
    select_repository(dialog, "clarence/project")
    refreshed = [GitHubRepository(3, "clarence/another", False)]
    if retained_id is not None:
        refreshed.append(GitHubRepository(retained_id, "clarence/project", True))
    monkeypatch.setattr(publishing_ui, "list_owned_repositories", lambda *_args, **_kwargs: repositories(*refreshed))
    dialog.github_refresh_repositories.click()
    wait_until(app, lambda: dialog._repository_worker is None)
    if retained_id == 12:
        assert dialog._existing_repository().full_name == "clarence/project"
        assert dialog.review_button.isEnabled()
    else:
        assert dialog._existing_repository() is None
        assert not dialog.review_button.isEnabled()
        assert "previous repository is no longer available" in dialog.github_repository_status.text()


def test_empty_list_has_clear_create_or_access_guidance(app, tmp_path, monkeypatch):
    monkeypatch.setattr(publishing_ui, "list_owned_repositories", lambda *_args, **_kwargs: RepositoryList(123, "clarence", ()))
    dialog = existing_dialog(app, tmp_path)
    assert "No owned repositories" in dialog.github_existing_repository.currentText()
    assert "Check token access or create a new repository" in dialog.github_repository_status.text()
    assert not dialog.review_button.isEnabled()
    assert dialog.github_refresh_repositories.isEnabled()


def test_failed_list_can_be_retried_without_losing_file_selection(app, tmp_path, monkeypatch):
    def failed(*_args, **_kwargs):
        raise GitHubError("The connection timed out. Try again.")

    monkeypatch.setattr(publishing_ui, "list_owned_repositories", failed)
    dialog = existing_dialog(app, tmp_path)
    assert "timed out" in dialog.github_repository_status.text()
    assert dialog.github_refresh_repositories.text() == "Retry"
    assert len(dialog._selected_files()) == 1
    assert not dialog.review_button.isEnabled()
    monkeypatch.setattr(publishing_ui, "list_owned_repositories", lambda *_args, **_kwargs: repositories())
    dialog.github_refresh_repositories.click()
    wait_until(app, lambda: dialog._repository_worker is None)
    assert dialog.github_refresh_repositories.text() == "Refresh list"
    assert dialog.github_existing_repository.count() == 2
    assert dialog._existing_repository() is None


def test_unexpected_list_error_is_safe_and_does_not_expose_transport_details(app, tmp_path, monkeypatch):
    def failed(*_args, **_kwargs):
        raise RuntimeError("Authorization: Bearer test-only-secret-which-must-not-display")

    monkeypatch.setattr(publishing_ui, "list_owned_repositories", failed)
    dialog = existing_dialog(app, tmp_path)
    assert "Could not load your GitHub repositories" in dialog.github_repository_status.text()
    assert "test-only-secret" not in dialog.github_repository_status.text()
    assert "test-only-secret" not in dialog.operation_log.toPlainText()


def test_disconnected_existing_mode_does_not_request_repositories_or_allow_review(app, tmp_path, monkeypatch, github_session):
    github_session.disconnect()
    calls = []
    monkeypatch.setattr(publishing_ui, "list_owned_repositories", lambda *_args, **_kwargs: calls.append(True))
    dialog = existing_dialog(app, tmp_path)
    assert not dialog.github_existing_repository.isEnabled()
    assert not dialog.github_refresh_repositories.isEnabled()
    assert not dialog.review_button.isEnabled()
    dialog._prepare_publish()
    assert calls == [] and dialog._worker is None
    assert "Connect your GitHub account" in dialog.status_label.text()


def test_token_rotation_invalidates_review_and_exact_repository_selection(app, tmp_path, monkeypatch, github_session):
    dialog = existing_dialog(app, tmp_path)
    select_repository(dialog, "clarence/project")
    plan = replace(prepared(tmp_path), new_repository=False, repository_id=12, account_id=123, owner_id=123)
    monkeypatch.setattr(publishing_ui, "prepare_github_publish", lambda *_args, **_kwargs: plan)
    dialog.review_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert dialog.publish_button.isEnabled()
    github_session.token = "test-only-new-token-for-the-same-account"
    dialog._check_connection_changed()
    assert dialog._plan is None
    assert not plan.working_path.exists()
    assert not dialog.publish_button.isEnabled()
    wait_until(app, lambda: dialog._repository_worker is None)
    assert dialog._existing_repository() is None
    assert not dialog.review_button.isEnabled()


def test_late_list_result_from_replaced_connection_is_discarded(app, tmp_path, monkeypatch, github_session):
    first_release = threading.Event()
    started = threading.Event()
    requests = []

    def list_repos(session, **_kwargs):
        requests.append(session.token)
        if len(requests) == 1:
            started.set()
            first_release.wait(2)
            return repositories(GitHubRepository(1, "clarence/old-token-only", True))
        return repositories(GitHubRepository(2, "clarence/new-token-only", True))

    monkeypatch.setattr(publishing_ui, "list_owned_repositories", list_repos)
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog._files_ready(sample_scan(tmp_path))
    dialog.github_mode.setCurrentIndex(1)
    wait_until(app, started.is_set)
    github_session.token = "test-only-rotated-token"
    dialog._check_connection_changed()
    first_release.set()
    wait_until(app, lambda: dialog._repository_worker is None and not dialog._repository_request_pending)
    assert dialog.github_existing_repository.count() == 2
    assert dialog.github_existing_repository.itemData(1).full_name == "clarence/new-token-only"
    assert dialog._existing_repository() is None


def test_switch_to_new_mode_cancels_pending_list_and_does_not_restart_it(app, tmp_path, monkeypatch):
    started = threading.Event()
    calls = []

    def list_repos(_session, *, cancelled, **_kwargs):
        calls.append(True)
        started.set()
        cancelled.wait(2)
        return repositories()

    monkeypatch.setattr(publishing_ui, "list_owned_repositories", list_repos)
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog.github_mode.setCurrentIndex(1)
    wait_until(app, started.is_set)
    dialog.github_mode.setCurrentIndex(0)
    wait_until(app, lambda: dialog._repository_worker is None)
    assert dialog._repository_list is None
    assert not dialog._repository_request_pending
    assert dialog.github_visibility_label.isHidden() is False
    assert calls == [True]


def test_close_waits_for_repository_worker_cancellation_without_destroying_it(app, tmp_path, monkeypatch):
    started = threading.Event()
    cancelled_seen = threading.Event()

    def list_repos(_session, *, cancelled, **_kwargs):
        started.set()
        if cancelled.wait(2):
            cancelled_seen.set()
        raise GitHubError("Cancelled.", cancelled=True)

    monkeypatch.setattr(publishing_ui, "list_owned_repositories", list_repos)
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog.show()
    dialog.github_mode.setCurrentIndex(1)
    wait_until(app, started.is_set)
    dialog.close()
    wait_until(app, lambda: dialog._repository_worker is None and not dialog.isVisible())
    assert cancelled_seen.is_set()
    assert not dialog._connection_timer.isActive()
    assert dialog._repository_list is None


def test_project_origin_load_is_queued_until_local_scan_worker_finishes(app, tmp_path, monkeypatch):
    calls = []
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    scan = replace(sample_scan(tmp_path), github_repository="clarence/project")
    monkeypatch.setattr(publishing_ui, "scan_project", lambda *_args: scan)

    def list_repos(*_args, **_kwargs):
        calls.append(dialog._worker)
        return repositories()

    monkeypatch.setattr(publishing_ui, "list_owned_repositories", list_repos)
    dialog._refresh_files()
    wait_until(app, lambda: dialog._scan is not None and dialog._worker is None and dialog._repository_worker is None)
    assert calls == [None]
    assert dialog.github_mode.currentIndex() == 1
    assert dialog._existing_repository().full_name == "clarence/project"


@pytest.mark.parametrize("changed", ["repository_id", "owner_id", "account_id"])
def test_review_rejects_repository_or_owner_identity_changed_since_listing(app, tmp_path, monkeypatch, changed):
    dialog = existing_dialog(app, tmp_path)
    select_repository(dialog, "clarence/project")
    fields = {"new_repository": False, "repository_id": 12, "account_id": 123, "owner_id": 123}
    fields[changed] = 999
    plan = replace(prepared(tmp_path), **fields)
    monkeypatch.setattr(publishing_ui, "prepare_github_publish", lambda *_args, **_kwargs: plan)
    dialog.review_button.click()
    wait_until(app, lambda: dialog._worker is None)
    assert dialog._plan is None
    assert not plan.working_path.exists()
    assert not dialog.publish_button.isEnabled()
    assert not dialog.review_button.isEnabled()
    assert "destination changed" in dialog.status_label.text()


def test_cancelled_review_result_cannot_become_publishable(app, tmp_path):
    dialog = PublishDialog(tmp_path, "Example", auto_scan=False)
    dialog._files_ready(sample_scan(tmp_path))
    returned = threading.Event()
    release = threading.Event()
    plan = prepared(tmp_path)

    def ignored_cancellation(_runner):
        returned.set()
        release.wait(2)
        return plan

    dialog._start_worker(ignored_cancellation, dialog._publish_prepared, account_bound=True)
    wait_until(app, returned.is_set)
    dialog._cancel_operation()
    release.set()
    wait_until(app, lambda: dialog._worker is None)
    assert dialog._plan is None
    assert not dialog.publish_button.isEnabled()
    assert not plan.working_path.exists()
