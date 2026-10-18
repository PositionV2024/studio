import json
import tempfile
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit

import studio.publish_dialog as publishing_ui
from studio.publish_dialog import PublishDialog
from studio.publishing import PreparedPublish, ProjectScan, PublishFile, PublishResult


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def clean_dialogs(app):
    yield
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, PublishDialog):
            widget.reject()
            if widget._worker is not None:
                wait_until(app, lambda: widget._worker is None)
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

    def publish(reviewed, runner):
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

    def fail(*_args):
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
