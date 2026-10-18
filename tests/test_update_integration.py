"""Exercise the real toolbar and save-before-update close path without a server."""

from __future__ import annotations

from email.message import Message
import hashlib
import http.client
import io
import json
from pathlib import Path
import ssl
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QWidget

from studio.main_window import MainWindow
from studio.update_install import UpdateInstallError
from launcher import build_update_package, update_apply
from studio import update_dialog as update_ui
from studio import update_install
from studio.github_connection import GitHubSession


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
    assert not dialog.check_button.isEnabled()
    assert dialog.connect_button.isVisible()
    assert not hasattr(dialog, "account_checkbox")
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


# These regressions deliberately exercise the complete distribution, the real
# session and HTTPS request/response bytes, and actual Qt controls. Only the
# network boundary and Windows availability are substituted; archive parsing,
# downloaded-file validation, file comparison and installation planning run.
FLOW_CURRENT = "0.4.17"
FLOW_TARGET = "0.4.18"
FLOW_TOKEN = "offline_connected_studio_integration_token"
FLOW_REPOSITORY = "PositionV2024/studio"
FLOW_CHANGED = "app/studio/integration_changed.py"
FLOW_ADDED = "app/studio/integration_added.py"
FLOW_REMOVED = "app/studio/integration_removed.py"


def _flow_manifest(files, version):
    return (json.dumps({
        "schema_version": 1, "application": update_apply.APPLICATION,
        "version": version, "platform": "windows-x64", "layout": "compact-v1",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }, indent=2) + "\n").encode()


def _flow_version(files, version):
    result = dict(files)
    result["app/studio/__init__.py"] = f'__version__ = "{version}"\n'.encode()
    result["app/pyproject.toml"] = (
        f'[project]\nname = "hoyo-project-studio"\nversion = "{version}"\n'
    ).encode()
    return result


def _flow_zip(files, version, *, prefix="Clarence Desktop/", extra=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(prefix + name, data)
        archive.writestr(prefix + update_apply.MANIFEST_NAME, _flow_manifest(files, version))
        for name, data in extra:
            archive.writestr(name, data)
    return stream.getvalue()


def _flow_snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def _flow_wait(app, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.003)
    raise AssertionError("The real update worker did not complete")


class _FlowResponse:
    def __init__(self, data=b"", *, status=200, headers=None):
        self.status = status
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.headers = Message()
        self.headers["Content-Length"] = str(len(self.body))
        for name, value in (headers or {}).items():
            self.headers[name] = str(value)
        self.offset = 0

    def read(self, size):
        result = self.body[self.offset:self.offset + size]
        self.offset += len(result)
        return result

    def close(self):
        pass


class _FlowHTTP:
    """Route HTTP bytes for one immutable release without mocking services."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.binary_transfers = 0
        self.asset = None
        self.release = None
        self.raw_zip = b""
        router = self

        class Connection:
            def __init__(self, host, **kwargs):
                assert host in {"api.github.com", "release-assets.githubusercontent.com"}
                assert kwargs["port"] == 443
                assert kwargs["context"].check_hostname
                assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
                self.host = host
                self.sock = None
                self.response = None

            def set_debuglevel(self, value):
                assert value == 0

            def connect(self):
                self.sock = SimpleNamespace(shutdown=lambda _how: None)

            def request(self, method, path, *, body, headers):
                assert self.sock is not None
                assert method == "GET" and body is None, "Update operations must never write to GitHub"
                if self.host == "api.github.com":
                    assert headers["Authorization"] == "Bearer " + FLOW_TOKEN
                else:
                    assert "Authorization" not in headers
                router.calls.append((self.host, method, path, dict(headers), threading.get_ident()))
                self.response = router.route(self.host, path, headers)

            def getresponse(self):
                return self.response

            def close(self):
                self.sock = None

        monkeypatch.setattr(http.client, "HTTPSConnection", Connection)

    def publish_fixture(self, raw_zip, *, version=FLOW_TARGET, name="Clarence Studio Desktop.zip"):
        self.raw_zip = raw_zip
        self.asset = {
            "id": 8642, "name": name, "size": len(raw_zip), "state": "uploaded",
            "digest": "sha256:" + hashlib.sha256(raw_zip).hexdigest(),
        }
        self.release = {
            "id": 7531, "draft": False, "prerelease": False, "tag_name": "v" + version,
            "published_at": "2026-10-18T09:30:00Z", "body": "Desktop release file comparison.",
            "assets": [self.asset],
        }

    def route(self, host, path, headers):
        if host == "release-assets.githubusercontent.com":
            assert path == "/offline-studio.zip?fixture=1"
            self.binary_transfers += 1
            return _FlowResponse(self.raw_zip)
        if path == "/user":
            return _FlowResponse({"id": 9124, "login": "PositionV2024", "type": "User"})
        if path in {
            f"/repos/{FLOW_REPOSITORY}/releases/latest",
            f"/repos/{FLOW_REPOSITORY}/releases/tags/{self.release['tag_name']}",
        }:
            return _FlowResponse(self.release)
        assert path == f"/repos/{FLOW_REPOSITORY}/releases/assets/{self.asset['id']}"
        if headers["Accept"] == "application/octet-stream":
            return _FlowResponse(status=302, headers={
                "Location": "https://release-assets.githubusercontent.com/offline-studio.zip?fixture=1",
            })
        return _FlowResponse(self.asset)


@pytest.fixture
def release_flow(app, tmp_path, monkeypatch):
    # collect_files is also used to build the distributed application. Thus the
    # regression contains the complete launcher/application, not a pretend ZIP.
    files = build_update_package.collect_files(Path(__file__).resolve().parents[1])
    old = _flow_version(files, FLOW_CURRENT)
    old[FLOW_CHANGED] = b"def greeting():\n    return 'Installed baseline'\n"
    old[FLOW_REMOVED] = b"# This old application file is retired\n"
    new = _flow_version(old, FLOW_TARGET)
    new[FLOW_CHANGED] = b"def greeting():\n    return 'Online release'\n"
    del new[FLOW_REMOVED]
    new[FLOW_ADDED] = b"# This application helper is new\n"
    root = tmp_path / "My Studio 雪"
    for name, data in old.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / update_apply.MANIFEST_NAME).write_bytes(_flow_manifest(old, FLOW_CURRENT))
    for name in ("app/projects/user_app/main.py", "app/.venv/private_dependency.py", "app/personal_notes.txt"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"User-owned content is preserved and excluded from the comparison\n")
    http = _FlowHTTP(monkeypatch)
    session = GitHubSession(store=SimpleNamespace(available=False, load=lambda: None, delete=lambda: None))
    session.connect(FLOW_TOKEN)
    assert session.account["login"] == "PositionV2024"
    monkeypatch.setattr(update_ui, "__version__", FLOW_CURRENT)
    monkeypatch.setattr(update_install, "__version__", FLOW_CURRENT)
    monkeypatch.setattr(update_install, "_is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local App Data"))
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    controller = update_ui.UpdateController(root / "app", settings, session=session)
    controller._download_cache = tmp_path / "Downloaded releases"
    assert controller.install_supported
    controller.open_dialog()
    app.processEvents()
    yield SimpleNamespace(
        root=root, app_root=root / "app", old=old, new=new, http=http, session=session,
        controller=controller, dialog=controller._dialog, temporary=tmp_path,
    )
    controller.begin_shutdown()
    _flow_wait(app, lambda: not controller.is_active)
    if controller._dialog is not None:
        controller._dialog.close()
        controller._dialog.deleteLater()
    controller.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def _flow_check(flow, app):
    assert flow.controller.check_for_updates()
    _flow_wait(app, lambda: not flow.controller.is_active)
    assert flow.controller.release is not None, flow.controller.status_message
    return flow.controller.comparison


def _flow_select_file(flow, path):
    for index in range(flow.dialog.change_list.topLevelItemCount()):
        item = flow.dialog.change_list.topLevelItem(index)
        if item.text(1) == path:
            flow.dialog.change_list.setCurrentItem(item)
            return item
    raise AssertionError(f"The actual File changes list has no row for {path}")


@pytest.mark.parametrize("prefix,name", [
    ("", "Desktop.zip"),
    ("Studio renamed by Clarence/", "Clarence Studio Desktop.zip"),
    ("Downloads/Studio October/", "Studio-October-2026.zip"),
])
def test_complete_renamed_release_populates_actual_file_changes(release_flow, app, prefix, name):
    flow = release_flow
    (flow.root / FLOW_CHANGED).write_bytes(b"def greeting():\n    return 'My saved desktop edit'\n")
    before = _flow_snapshot(flow.root)
    flow.http.publish_fixture(_flow_zip(flow.new, FLOW_TARGET, prefix=prefix), name=name)
    result = _flow_check(flow, app)
    assert result is not None, flow.controller.comparison_error
    rows = {change.path: change for change in result.changes}
    assert rows[FLOW_ADDED].status == "added"
    assert rows[FLOW_REMOVED].status == "removed"
    assert rows[FLOW_CHANGED].status == "modified" and rows[FLOW_CHANGED].locally_modified
    assert flow.dialog.tabs.tabText(flow.dialog.tabs.currentIndex()) == "File changes"
    assert flow.dialog.change_list.isVisible() and flow.dialog.diff.isVisible()
    assert flow.dialog.latest_version_label.text() == FLOW_TARGET
    assert flow.dialog.change_list.topLevelItemCount() == len(result.changes)
    _flow_select_file(flow, FLOW_CHANGED)
    assert "-    return 'My saved desktop edit'" in flow.dialog.diff.toPlainText()
    assert "+    return 'Online release'" in flow.dialog.diff.toPlainText()
    assert "-    return 'Installed baseline'" not in flow.dialog.diff.toPlainText()
    assert "added" in flow.dialog.comparison_summary.text()
    assert "removed" in flow.dialog.comparison_summary.text()
    assert not any("projects/" in name or ".venv/" in name or "personal_notes" in name for name in rows)
    assert flow.controller.pending_update is None
    assert flow.dialog.download_button.isEnabled()
    assert flow.http.binary_transfers == 1
    assert all(call[-1] != threading.get_ident() for call in flow.http.calls if call[2] != "/user")
    assert _flow_snapshot(flow.root) == before


def test_download_preparation_reuses_checked_arbitrary_package_without_installing(release_flow, app):
    flow = release_flow
    flow.http.publish_fixture(_flow_zip(flow.new, FLOW_TARGET, prefix="My downloads/Current Studio/"))
    before = _flow_snapshot(flow.root)
    result = _flow_check(flow, app)
    assert result is not None, flow.controller.comparison_error
    cached = flow.controller._comparison_archive
    assert cached is not None and cached.is_file()
    requests_before_download = list(flow.http.calls)
    assert flow.controller.download()
    _flow_wait(app, lambda: not flow.controller.is_active)
    prepared = flow.controller.pending_update
    assert prepared is not None, flow.controller.status_message
    assert flow.controller.state == "ready"
    assert prepared.archive == cached == flow.controller.downloaded_archive
    assert flow.http.calls == requests_before_download
    assert flow.http.binary_transfers == 1
    assert prepared.job_directory.is_relative_to(flow.temporary / "Local App Data")
    assert not prepared.job_directory.is_relative_to(flow.root)
    job, directory, _root = update_apply.load_job(prepared.job_file)
    assert job["current_version"] == FLOW_CURRENT and job["target_version"] == FLOW_TARGET
    assert job["repository"] == FLOW_REPOSITORY
    assert (directory / "staged" / FLOW_CHANGED).read_bytes() == flow.new[FLOW_CHANGED]
    assert (directory / "staged" / FLOW_ADDED).is_file()
    assert not (directory / "staged" / FLOW_REMOVED).exists()
    assert flow.controller.comparison == result
    assert flow.dialog.restart_button.isVisible() and flow.dialog.restart_button.isEnabled()
    assert _flow_snapshot(flow.root) == before


@pytest.mark.parametrize("version,state", [(FLOW_CURRENT, "current"), ("0.4.16", "ahead")])
def test_same_or_older_renamed_release_still_shows_diffs_without_download(release_flow, app, version, state):
    flow = release_flow
    release_files = _flow_version(flow.new, version)
    flow.http.publish_fixture(_flow_zip(release_files, version, prefix=""), version=version)
    before = _flow_snapshot(flow.root)
    result = _flow_check(flow, app)
    assert result is not None, flow.controller.comparison_error
    assert flow.controller.state == state
    assert flow.dialog.latest_version_label.text() == version
    assert FLOW_CHANGED in {change.path for change in result.changes}
    _flow_select_file(flow, FLOW_CHANGED)
    assert "+    return 'Online release'" in flow.dialog.diff.toPlainText()
    assert not flow.dialog.download_button.isEnabled()
    requests_before_download = list(flow.http.calls)
    assert not flow.controller.download()
    assert flow.http.calls == requests_before_download
    assert _flow_snapshot(flow.root) == before


@pytest.mark.parametrize("invalid", ["missing-manifest", "sibling-file", "wrong-version"])
def test_invalid_complete_release_keeps_version_and_explains_missing_diff(release_flow, app, invalid):
    flow = release_flow
    if invalid == "missing-manifest":
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in flow.new.items():
                archive.writestr("My Studio/" + name, data)
        package = raw.getvalue()
    else:
        version = "0.4.19" if invalid == "wrong-version" else FLOW_TARGET
        files = _flow_version(flow.new, version)
        package = _flow_zip(files, version, extra=(("Read this unrelated file.txt", b"outside app"),)
                            if invalid == "sibling-file" else ())
    flow.http.publish_fixture(package)
    before = _flow_snapshot(flow.root)
    assert _flow_check(flow, app) is None
    assert flow.dialog.latest_version_label.text() == FLOW_TARGET
    assert flow.dialog.notes.toPlainText() == "Desktop release file comparison."
    assert flow.dialog.comparison_summary.text() == "File comparison unavailable"
    assert flow.dialog.comparison_context.toPlainText().strip()
    assert flow.controller.comparison_error in flow.dialog.comparison_context.toPlainText()
    assert not flow.dialog.download_button.isEnabled()
    assert not flow.controller.download()
    assert flow.controller.pending_update is None and flow.controller.downloaded_archive is None
    assert flow.dialog.change_list.topLevelItemCount() == 0
    assert flow.controller._comparison_archive is None
    assert not list(flow.controller._download_cache.rglob("*.zip"))
    assert _flow_snapshot(flow.root) == before


def test_local_installation_problem_preserves_valid_release_and_visible_diff(release_flow, app):
    flow = release_flow
    (flow.root / FLOW_CHANGED).write_bytes(b"def greeting():\n    return 'Saved custom application code'\n")
    flow.http.publish_fixture(_flow_zip(flow.new, FLOW_TARGET, prefix=""))
    before = _flow_snapshot(flow.root)
    assert _flow_check(flow, app) is not None
    assert flow.controller.download()
    _flow_wait(app, lambda: not flow.controller.is_active)
    # Real build_plan refuses to replace a locally edited owned file. Preserve
    # the already fetched version and explain that concrete failure in the UI.
    assert flow.controller.pending_update is None
    assert flow.controller.state == "error"
    assert flow.dialog.latest_version_label.text() == FLOW_TARGET
    assert flow.dialog.notes.toPlainText() == "Desktop release file comparison."
    assert not flow.dialog.download_button.isEnabled()
    assert "local changes" in flow.dialog.comparison_context.toPlainText().lower()
    assert flow.dialog.comparison_summary.text() != "Check for updates to review added, modified and removed application files."
    assert _flow_snapshot(flow.root) == before


def test_source_checkout_compares_renamed_package_with_explicit_baseline_explanation(release_flow, app):
    flow = release_flow
    source = flow.temporary / "Source checkout"
    for name, data in flow.old.items():
        path = source / name.removeprefix("app/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (source / "studio/integration_changed.py").write_bytes(b"def greeting():\n    return 'Saved source checkout'\n")
    flow.controller.app_root = source
    flow.http.publish_fixture(_flow_zip(flow.new, FLOW_TARGET, prefix="Clarence App/"))
    before = _flow_snapshot(source)
    result = _flow_check(flow, app)
    assert result is not None, flow.controller.comparison_error
    _flow_select_file(flow, FLOW_CHANGED)
    assert "-    return 'Saved source checkout'" in flow.dialog.diff.toPlainText()
    assert "+    return 'Online release'" in flow.dialog.diff.toPlainText()
    assert "no installed package baseline" in flow.dialog.comparison_context.toPlainText()
    assert not any(change.locally_modified for change in result.changes)
    assert _flow_snapshot(source) == before
