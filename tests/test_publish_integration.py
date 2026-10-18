"""Verify the selected project's live edits are saved before publish review.

These cases use production MainWindow methods, real source editors, real Qt
Properties fields, real SettingsStores and the publishing dialog's local scan.
The shared fixture controls only the browser/process endpoints. Dialog.exec is
replaced so no account sign-in or remote action can be selected during the test.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog

from studio.code_editor import CodeEditorDocument
from studio.publish_dialog import PublishDialog
from studio.settings_store import SettingsStore
from studio import github_connection, publish_dialog as publish_ui, publishing
from studio.github_connection import GitHubError, GitHubSession
from github_api_fixture import FakeGitHub
from test_github_connection import FakeConnection, FakeResponse, MemoryStore
from test_direct_edit_integration import begin_inline, direct_window, inline_text
from test_visual_edit_integration import (
    select_text, type_pending, visual_edit_app, visual_window,
)


def editor(window, project_name, filename, *, change=True):
    root = window.records[project_name].path
    path = root / filename
    path.write_text("print('saved original')\n", encoding="utf-8")
    document = CodeEditorDocument(path, window, project_path=root)
    document.set_language_mode("plain")
    window.open_editors[path.resolve()] = document
    window.document_tabs.addTab(document, filename)
    if change:
        document.editor.moveCursor(QTextCursor.MoveOperation.End)
        document.editor.insertPlainText(f"# Unsaved {project_name} {filename}\n")
        assert document.is_modified
    return document


@pytest.fixture
def publishing_window(direct_window):
    window = direct_window
    window.alpha_app = editor(window, "Alpha", "app.py")
    window.alpha_helper = editor(window, "Alpha", "helper.py")
    window.alpha_clean = editor(window, "Alpha", "unchanged.py", change=False)
    window.bravo_app = editor(window, "Bravo", "app.py")
    window.document_tabs.setCurrentWidget(window.preview)
    yield window
    for document in window.open_editors.values():
        document.shutdown()


@pytest.fixture
def publish_capture(monkeypatch, visual_edit_app):
    capture = SimpleNamespace(dialogs=[], before_scan=None)

    def review_locally(dialog):
        # This callback runs on entry to exec, before its queued initial scan.
        assert dialog._scan is None
        assert dialog._worker is None
        capture.dialogs.append(dialog)
        if capture.before_scan is not None:
            capture.before_scan(dialog)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            visual_edit_app.processEvents()
            if dialog._scan is not None and dialog._worker is None:
                break
            time.sleep(0.01)
        assert dialog._scan is not None, dialog.status_label.text()
        assert dialog._worker is None
        assert not dialog.publish_button.isEnabled()
        dialog.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(PublishDialog, "exec", review_locally)
    yield capture
    for dialog in capture.dialogs:
        dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    visual_edit_app.processEvents()


def assert_source_unsaved(document):
    assert document.path.read_text(encoding="utf-8") == "print('saved original')\n"
    assert document.is_modified
    assert "Unsaved" in document.editor.toPlainText()


@pytest.mark.parametrize("target, tab_index", [("github", 0), ("vercel", 1), ("streamlit", 2)])
def test_selected_project_code_and_last_preview_character_are_saved_before_review(
    publishing_window, publish_capture, target, tab_index,
):
    window = publishing_window
    beta_store = window._settings_for_project(window.records["Bravo"].path)
    beta_store.set_element_patch("bravo.title", {"text": "Keep Bravo unsaved"})
    clean_before = window.alpha_clean.path.stat()
    begin_inline(window)
    inline_text(window, "Already received text")
    window.preview.queued_inline_text = "Final preview character before publishing!"
    expected = {"hero.title": {"text": "Final preview character before publishing!"}}
    saved_after_flush = []
    for document in (window.alpha_app, window.alpha_helper):
        document.saved.connect(lambda _path: saved_after_flush.append(window._inline_visual_owner is None))

    def check_before_scan(dialog):
        assert dialog.project_path == window.records["Alpha"].path
        assert dialog.target_tabs.currentIndex() == tab_index
        assert window._inline_visual_owner is None
        assert window.preview.queued_inline_text is None
        assert window.current_settings.load_published() == expected
        assert SettingsStore(dialog.project_path).load_published() == expected
        assert not window.current_settings.is_dirty
        for document in (window.alpha_app, window.alpha_helper):
            assert not document.is_modified
            assert document.path.read_text(encoding="utf-8") == document.editor.toPlainText()
        assert_source_unsaved(window.bravo_app)
        assert beta_store.is_dirty
        assert beta_store.load_published() == {}
        assert SettingsStore(window.records["Bravo"].path).load_draft() == {}

    publish_capture.before_scan = check_before_scan
    window.open_publish_dialog(target)
    assert len(publish_capture.dialogs) == 1
    assert saved_after_flush == [True, True]
    scan = publish_capture.dialogs[0]._scan
    scanned = {item.path: item for item in scan.files}
    assert "app.py" in scanned and ".studio/published.json" in scanned
    assert scanned["app.py"].sha256 == hashlib.sha256(window.alpha_app.path.read_bytes()).hexdigest()
    assert scanned[".studio/published.json"].sha256 == hashlib.sha256(window.current_settings.published_path.read_bytes()).hexdigest()
    assert window.alpha_clean.path.stat().st_mtime_ns == clean_before.st_mtime_ns
    assert window.alpha_clean.path.stat().st_ino == clean_before.st_ino
    window.dialogs.critical.assert_not_called()


def test_code_save_failure_keeps_buffers_and_prevents_dialog_or_local_scan(
    publishing_window, publish_capture, monkeypatch,
):
    window = publishing_window
    begin_inline(window)
    inline_text(window, "UI edit remains available")
    replace = os.replace

    def fail_selected_source(source, destination):
        if Path(destination) == window.alpha_app.path:
            raise PermissionError("Selected source file is read-only")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_selected_source)
    window.open_publish_dialog("github")
    assert publish_capture.dialogs == []
    assert window.findChildren(PublishDialog) == []
    assert_source_unsaved(window.alpha_app)
    assert_source_unsaved(window.alpha_helper)
    assert_source_unsaved(window.bravo_app)
    assert window.current_settings.is_dirty
    assert window.current_settings.load_draft()["hero.title"]["text"] == "UI edit remains available"
    assert window.current_settings.load_published() == {}
    assert SettingsStore(window.current_project.path).load_draft() == {}
    window.dialogs.critical.assert_called_once()


@pytest.mark.parametrize("failed_file", ["draft.json", "published.json"])
def test_ui_save_or_publish_failure_prevents_dialog_and_preserves_available_changes(
    publishing_window, publish_capture, monkeypatch, failed_file,
):
    window = publishing_window
    begin_inline(window)
    inline_text(window, "Publish this UI later")
    expected = {"hero.title": {"text": "Publish this UI later"}}
    replace = os.replace
    failed_path = window.current_project.path / ".studio" / failed_file

    def fail_settings(source, destination):
        if Path(destination) == failed_path:
            raise PermissionError("UI settings could not be replaced")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_settings)
    window.open_publish_dialog("vercel")
    assert publish_capture.dialogs == []
    assert window.findChildren(PublishDialog) == []
    assert window.current_settings.load_draft() == expected
    assert window.current_settings.load_published() == {}
    assert window.current_settings.is_dirty == (failed_file == "draft.json")
    assert SettingsStore(window.current_project.path).load_draft() == ({} if failed_file == "draft.json" else expected)
    assert not window.alpha_app.is_modified
    assert not window.alpha_helper.is_modified
    assert_source_unsaved(window.bravo_app)
    window.dialogs.critical.assert_called_once()
    assert "Could not prepare project for publishing" in window.dialogs.critical.call_args.args


def test_unacknowledged_inline_flush_prevents_any_source_save_or_dialog(
    publishing_window, publish_capture,
):
    window = publishing_window
    begin_inline(window)
    inline_text(window, "Acknowledged edit is still here")
    window.preview.queued_inline_text = "Unacknowledged last character!"
    owner_before = window._inline_visual_owner
    window.preview.commit_inline_edit.side_effect = None
    window.preview.commit_inline_edit.return_value = False
    window.open_publish_dialog("streamlit")
    assert publish_capture.dialogs == []
    assert window.findChildren(PublishDialog) == []
    for document in (window.alpha_app, window.alpha_helper, window.bravo_app):
        assert_source_unsaved(document)
    assert window._inline_visual_owner == owner_before
    assert window.preview.queued_inline_text == "Unacknowledged last character!"
    assert window.current_settings.is_dirty
    assert window.current_settings.load_published() == {}
    assert SettingsStore(window.current_project.path).load_draft() == {}
    window.dialogs.warning.assert_called_once()
    window.dialogs.critical.assert_not_called()


def test_focused_properties_value_is_committed_before_publication_review(
    publishing_window, publish_capture,
):
    window = publishing_window
    select_text(window)
    type_pending(window, "Still typing when Publish is pressed")
    assert window.inspector.has_pending_edits

    def check_before_scan(dialog):
        published = json.loads((dialog.project_path / ".studio" / "published.json").read_text())
        assert published["hero.title"]["text"] == "Still typing when Publish is pressed"
        assert not window.inspector.has_pending_edits
        assert not window.current_settings.is_dirty

    publish_capture.before_scan = check_before_scan
    window.open_publish_dialog("github")
    assert len(publish_capture.dialogs) == 1
    assert_source_unsaved(window.bravo_app)


# These integration cases leave the complete production picker, session,
# authenticated JSON transport, review snapshot and Git-object publisher in
# place. Only the HTTPS socket endpoint is replaced with an in-memory GitHub.
PICKER_TOKEN = "offline_repository_picker_connection"
PICKER_NEXT_TOKEN = "offline_repository_picker_changed_connection"


class RepositoryPickerHTTPS:
    """Translate production HTTP bytes to the existing Git-object fixture."""

    def __init__(self, *, private=True):
        self.api = FakeGitHub(repository="clarence/selected-project", existing=True, private=private)
        self.original_head = self.api.seed({
            "app.py": b"print('old remote')\n",
            "remote-only.txt": b"Retain this unselected remote file.\n",
        })
        self.account = self.api.get_authenticated_user()
        self.api.calls.clear()
        self.rows = [self.row(456, "selected-project", private=private)]
        self.calls = []
        self.connections = []
        self.before_response = None
        self.list_status = 200
        self.forbidden_actions = []

    def row(self, repository_id, name, *, private=False, owner_id=123,
            owner_login="clarence", owner_type="User", **fields):
        return {
            "id": repository_id,
            "name": name,
            "full_name": owner_login + "/" + name,
            "private": private,
            "owner": {"id": owner_id, "login": owner_login, "type": owner_type},
            "archived": False,
            "disabled": False,
            "permissions": {"push": True},
            **fields,
        }

    def __call__(self, host, *, port, timeout, context):
        assert host == "api.github.com" and port == 443 and timeout > 0
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        connection = FakeConnection()
        self.connections.append(connection)

        def response():
            method, path, body, headers = connection.calls[-1]
            assert headers["Authorization"] in {
                "Bearer " + PICKER_TOKEN, "Bearer " + PICKER_NEXT_TOKEN,
            }
            assert not {"Cookie", "Proxy-Authorization"} & headers.keys()
            data = json.loads(body) if body else None
            # Never retain authentication headers in evidence or output.
            self.calls.append((method, path, data))
            if self.before_response is not None:
                self.before_response(connection, method, path)
            parsed = urlsplit(path)
            if method == "GET" and path == "/user":
                return FakeResponse(dict(self.account))
            if method == "GET" and parsed.path == "/user/repos":
                query = parse_qs(parsed.query)
                assert query["affiliation"] == ["owner"]
                assert query["visibility"] == ["all"]
                assert query["per_page"] == ["100"]
                page = int(query["page"][0])
                assert page >= 1
                return FakeResponse(self.rows[(page - 1) * 100:page * 100], status=self.list_status)
            try:
                payload = self.api.request(method, path, data)
            except GitHubError as exc:
                return FakeResponse({"message": "Offline response"}, status=exc.status or 500)
            return FakeResponse(payload)

        connection.response_action = response
        return connection

    def forbidden(self, *_args, **_kwargs):
        self.forbidden_actions.append(True)
        raise AssertionError("Repository selection must not launch a browser, Git or GitHub CLI")

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def picker_wait(app, condition, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.004)
    raise AssertionError("The production publishing dialog did not finish its operation")


def picker_names(dialog):
    return [
        repository.full_name
        for index in range(dialog.github_existing_repository.count())
        if (repository := dialog.github_existing_repository.itemData(index)) is not None
    ]


def picker_select(dialog, full_name):
    combo = dialog.github_existing_repository
    for index in range(combo.count()):
        repository = combo.itemData(index)
        if repository is not None and repository.full_name == full_name:
            combo.setCurrentIndex(index)
            assert dialog._selected_repository_name() == full_name
            return
    raise AssertionError("The expected own repository is absent from the production picker")


@pytest.fixture
def repository_picker(tmp_path, monkeypatch, visual_edit_app):
    instances = []

    def build(*, private=True, remote="", saved=True):
        project = tmp_path / ("picker-project-" + str(len(instances)))
        project.mkdir()
        (project / "app.py").write_bytes(b"print('reviewed local')\n")
        (project / "local-only.txt").write_bytes(b"Include this selected project file.\n")
        (project / ".env").write_bytes(b"EXCLUDED=fixture-secret\n")
        if remote:
            git = project / ".git"
            git.mkdir()
            (git / "config").write_text(
                '[remote "origin"]\n\turl = https://github.com/' + remote + '.git\n',
                encoding="utf-8",
            )
        boundary = RepositoryPickerHTTPS(private=private)
        session = GitHubSession(store=MemoryStore(PICKER_TOKEN if saved else None))
        monkeypatch.setattr(github_connection.http.client, "HTTPSConnection", boundary)
        monkeypatch.setattr(github_connection, "_session", session)
        monkeypatch.setattr(publishing.CommandRunner, "run", boundary.forbidden)
        monkeypatch.setattr(publishing.CommandRunner, "executable", boundary.forbidden)
        monkeypatch.setattr(publish_ui.QDesktopServices, "openUrl", boundary.forbidden)
        dialog = PublishDialog(project, "Repository picker integration")
        instances.append(SimpleNamespace(
            dialog=dialog, session=session, boundary=boundary,
            project=project, app=visual_edit_app,
        ))
        dialog.show()
        picker_wait(visual_edit_app, lambda: dialog._scan is not None and dialog._worker is None)
        return instances[-1]

    yield build
    for instance in instances:
        dialog = instance.dialog
        dialog.reject()
        picker_wait(visual_edit_app, lambda: dialog._worker is None and dialog._repository_worker is None)
        dialog.deleteLater()
        assert not instance.boundary.forbidden_actions
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    visual_edit_app.processEvents()


def picker_load(instance):
    dialog = instance.dialog
    dialog.github_mode.setCurrentIndex(1)
    picker_wait(instance.app, lambda: dialog._repository_list is not None and dialog._repository_worker is None)
    return dialog


def picker_review(instance):
    dialog = picker_load(instance)
    picker_select(dialog, instance.boundary.api.repository)
    assert dialog.review_button.isEnabled()
    dialog.review_button.click()
    picker_wait(instance.app, lambda: dialog._worker is None)
    assert dialog._plan is not None, dialog.status_label.text()
    assert not instance.boundary.writes
    return dialog._plan


def test_repository_picker_create_mode_stays_local_and_existing_visibility_is_hidden(repository_picker):
    instance = repository_picker()
    dialog = instance.dialog
    assert dialog.github_mode.currentIndex() == 0
    assert dialog.github_visibility.isVisible()
    assert dialog.github_visibility_label.isVisible()
    assert not dialog.github_existing_repository.isVisible()
    assert instance.boundary.calls == []
    picker_load(instance)
    assert not dialog.github_visibility.isVisible()
    assert not dialog.github_visibility_label.isVisible()
    assert dialog.github_existing_repository.isVisible()
    assert not dialog.github_existing_repository.isEditable()
    requests = len(instance.boundary.calls)
    dialog.github_mode.setCurrentIndex(0)
    instance.app.processEvents()
    assert dialog.github_visibility.isVisible()
    assert dialog.github_visibility_label.isVisible()
    assert not dialog.github_existing_repository.isVisible()
    assert len(instance.boundary.calls) == requests
    assert not instance.boundary.writes


@pytest.mark.parametrize("private", [False, True], ids=["public", "private"])
def test_picker_owned_pagination_to_review_and_explicit_publish_keeps_exact_target(
    repository_picker, private,
):
    instance = repository_picker(private=private)
    boundary, dialog = instance.boundary, instance.dialog
    boundary.rows = [boundary.row(1000 + index, f"project-{index:03d}", private=bool(index % 2))
                     for index in range(104)] + boundary.rows
    boundary.rows.extend([
        boundary.row(9001, "foreign-project", owner_id=999, owner_login="someone-else"),
        boundary.row(9002, "organization-project", owner_id=991, owner_login="sample-org", owner_type="Organization"),
    ])
    picker_load(instance)
    names = picker_names(dialog)
    assert len(names) == 105 and len(set(names)) == 105
    assert all(name.startswith("clarence/") for name in names)
    assert boundary.api.repository in names
    assert instance.session.account["id"] == 123
    pages = [parse_qs(urlsplit(path).query)["page"] for method, path, _data in boundary.calls
             if method == "GET" and urlsplit(path).path == "/user/repos"]
    assert pages == [["1"], ["2"]]
    assert dialog._existing_repository() is None
    assert not dialog.review_button.isEnabled()
    # The retained create-mode name and visibility must have no influence on
    # the existing destination or its actual privacy.
    dialog.github_repository.setText("someone-else/not-selected")
    dialog.github_visibility.setCurrentIndex(0 if not private else 1)
    picker_select(dialog, boundary.api.repository)
    assert not boundary.writes
    assert not dialog.publish_button.isEnabled()
    dialog.review_button.click()
    picker_wait(instance.app, lambda: dialog._worker is None)
    plan = dialog._plan
    assert plan is not None, dialog.status_label.text()
    assert plan.repository == boundary.api.repository
    assert plan.repository_id == boundary.api.repository_id == 456
    assert plan.account == "clarence" and plan.account_id == plan.owner_id == 123
    assert plan.visibility == ("private" if private else "public")
    assert plan.new_repository is False
    assert plan.base_revision == boundary.original_head
    assert plan.branch.startswith("studio/update-")
    assert "-print('old remote')" in dialog.review_diff.toPlainText()
    assert "+print('reviewed local')" in dialog.review_diff.toPlainText()
    assert {item.path for item in plan.files} == {"app.py", "local-only.txt"}
    assert not boundary.writes
    # Local edits after review cannot change the immutable upload snapshot.
    (instance.project / "app.py").write_bytes(b"print('edit after review')\n")
    dialog.publish_button.click()
    picker_wait(instance.app, lambda: dialog._worker is None)
    result = dialog._result
    assert result is not None, dialog.status_label.text()
    assert result.repository == boundary.api.repository and result.branch == plan.branch
    assert result.new_repository is False
    assert boundary.api.branches["main"] == boundary.original_head
    published = boundary.api.tree_for(result.branch)
    assert published["app.py"][0] == b"print('reviewed local')\n"
    assert published["remote-only.txt"][0] == b"Retain this unselected remote file.\n"
    assert ".env" not in published
    assert len(boundary.api.branches) == 2
    assert all(path.startswith("/repos/clarence/selected-project/") for _method, path, _data in boundary.writes)
    assert not any(method in {"DELETE", "PATCH", "PUT"} for method, _path, _data in boundary.writes)
    assert not plan.working_path.exists()
    assert not dialog.publish_button.isEnabled()


def test_picker_missing_project_origin_and_removed_selection_never_choose_another_repo(repository_picker):
    instance = repository_picker(remote="clarence/no-longer-accessible")
    dialog = picker_load(instance)
    assert dialog._existing_repository() is None
    assert not dialog.review_button.isEnabled()
    assert picker_names(dialog) == [instance.boundary.api.repository]
    picker_select(dialog, instance.boundary.api.repository)
    assert dialog.review_button.isEnabled()
    instance.boundary.rows = [instance.boundary.row(789, "different-project")]
    dialog.github_refresh_repositories.click()
    picker_wait(instance.app, lambda: dialog._repository_worker is None and picker_names(dialog) == ["clarence/different-project"])
    assert dialog._existing_repository() is None
    assert not dialog.review_button.isEnabled()
    assert not dialog.publish_button.isEnabled()
    assert not instance.boundary.writes


def test_picker_empty_and_denied_lists_keep_review_disabled_without_manual_fallback(repository_picker):
    instance = repository_picker()
    instance.boundary.rows = []
    dialog = picker_load(instance)
    assert picker_names(dialog) == [] and dialog._existing_repository() is None
    assert not dialog.review_button.isEnabled()
    assert not dialog.github_repository.isVisible()
    instance.boundary.list_status = 403
    dialog.github_refresh_repositories.click()
    picker_wait(instance.app, lambda: dialog._repository_worker is None)
    assert picker_names(dialog) == [] and dialog._repository_list is None
    assert dialog.github_repository_status.text()
    assert not dialog.review_button.isEnabled()
    assert not instance.boundary.writes


@pytest.mark.parametrize("change", ["token", "disconnect"])
def test_picker_changed_connection_after_review_discards_snapshot_before_publish(repository_picker, change):
    instance = repository_picker()
    plan = picker_review(instance)
    if change == "token":
        instance.session.connect(PICKER_NEXT_TOKEN)
    else:
        instance.session.disconnect()
    # Check the direct slot guard too, without depending on the polling timer.
    instance.dialog._publish()
    picker_wait(instance.app, lambda: instance.dialog._worker is None and instance.dialog._repository_worker is None)
    assert instance.dialog._plan is None
    assert not plan.working_path.exists()
    assert not instance.dialog.publish_button.isEnabled()
    assert instance.dialog._existing_repository() is None
    assert not instance.boundary.writes


@pytest.mark.parametrize("change", ["token", "account"])
def test_picker_connection_change_during_https_list_discards_previous_choices(repository_picker, change):
    instance = repository_picker()
    dialog = picker_load(instance)
    picker_select(dialog, instance.boundary.api.repository)
    entered = threading.Event()

    def delayed_response(connection, _method, path):
        if urlsplit(path).path != "/user/repos":
            return
        entered.set()
        if not connection.socket.stopped.wait(3):
            raise AssertionError("Changing the connection did not cancel the old repository read")
        raise OSError("Offline stale-account read")

    instance.boundary.before_response = delayed_response
    dialog.github_refresh_repositories.click()
    picker_wait(instance.app, entered.is_set)
    if change == "account":
        instance.boundary.account = {
            "id": 987, "login": "different-account", "name": "Other offline account",
            "html_url": "https://github.com/different-account",
        }
        instance.boundary.rows = [instance.boundary.row(
            9871, "different-project", owner_id=987, owner_login="different-account",
        )]
    else:
        instance.boundary.rows = [instance.boundary.row(9871, "different-project")]
    instance.session.connect(PICKER_NEXT_TOKEN)
    instance.boundary.before_response = None
    assert dialog._check_connection_changed()
    assert dialog._existing_repository() is None and dialog._repository_list is None
    picker_wait(instance.app, lambda: dialog._repository_worker is None and dialog._repository_list is not None)
    expected = "different-account/different-project" if change == "account" else "clarence/different-project"
    assert picker_names(dialog) == [expected]
    assert dialog._existing_repository() is None
    assert not dialog.review_button.isEnabled()
    assert not dialog.publish_button.isEnabled()
    assert not instance.boundary.writes


@pytest.mark.parametrize("change", ["recreated", "transferred"])
def test_picker_destination_changed_after_selection_requires_a_fresh_list(repository_picker, change):
    instance = repository_picker()
    dialog = picker_load(instance)
    picker_select(dialog, instance.boundary.api.repository)
    if change == "recreated":
        instance.boundary.api.repository_id = 999
    else:
        instance.boundary.api.owner_id = 998
    dialog.review_button.click()
    picker_wait(instance.app, lambda: dialog._worker is None)
    assert dialog._plan is None
    assert not dialog.publish_button.isEnabled()
    assert not instance.boundary.writes


def test_picker_close_during_https_read_cancels_and_drains_worker_with_responsive_qt(repository_picker):
    instance = repository_picker()
    entered = threading.Event()

    def delayed_response(connection, _method, path):
        if urlsplit(path).path != "/user/repos":
            return
        entered.set()
        # The real client's cancellation watcher closes its owned socket.
        if not connection.socket.stopped.wait(3):
            raise AssertionError("Closing the dialog did not cancel the HTTPS read")
        raise OSError("Offline cancelled read")

    instance.boundary.before_response = delayed_response
    heartbeats = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: heartbeats.append(True))
    timer.start()
    try:
        instance.dialog.github_mode.setCurrentIndex(1)
        picker_wait(instance.app, lambda: entered.is_set() and len(heartbeats) >= 3)
        worker = instance.dialog._repository_worker
        assert worker is not None and worker.isRunning()
        instance.dialog.close()
        picker_wait(instance.app, lambda: instance.dialog._repository_worker is None and not instance.dialog.isVisible())
        assert not instance.boundary.writes
        assert instance.dialog._repository_list is None
        assert len(heartbeats) >= 3
    finally:
        timer.stop()
