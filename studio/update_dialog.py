"""Responsive GitHub update checks and user-initiated desktop installations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import QObject, QSettings, QStandardPaths, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from . import __version__
from .github_connection import GitHubError, GitHubSession, get_session
from .publish_dialog import GitHubConnectDialog
from .update_install import (
    PreparedUpdate, UpdateInstallError, installation_support,
    launch_prepared_update, prepare_install, validate_download_archive,
)
from .update_service import (
    UpdateCheck, UpdateError, UpdateRelease, check_for_update,
    download_update,
)


DEFAULT_UPDATE_REPOSITORY = "PositionV2024/studio"


@dataclass(frozen=True)
class _DownloadedUpdate:
    archive: Path
    prepared: PreparedUpdate | None
    manual_reason: str = ""


class _UpdateWorker(QThread):
    completed = Signal(object)
    failed = Signal(str, bool)
    progress = Signal(object)

    def __init__(self, operation: Callable, generation: int, kind: str, parent=None) -> None:
        super().__init__(parent)
        self.operation = operation
        self.generation = generation
        self.kind = kind
        self.cancelled = threading.Event()

    def request_cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        try:
            result = self.operation(self.cancelled, self.progress.emit)
            if self.cancelled.is_set():
                self.failed.emit("", True)
            else:
                self.completed.emit(result)
        except (UpdateError, UpdateInstallError, GitHubError) as error:
            self.failed.emit(str(error), self.cancelled.is_set() or bool(getattr(error, "cancelled", False)))
        except Exception:
            # Transport exception text may contain credentials, signed download
            # URLs or request headers. Only the services' safe errors reach UI.
            self.failed.emit(
                "The update could not finish. Check your connection and available disk space, then try again.",
                self.cancelled.is_set(),
            )


class _UpdateGitHubConnectDialog(GitHubConnectDialog):
    """Use the existing credential UI with read-only update permissions."""

    def __init__(self, session: GitHubSession, repository: str, parent=None) -> None:
        super().__init__(session, repository, parent=parent)
        self.setWindowTitle("Connect GitHub for updates")
        parts = urlsplit(self._setup_url)
        query = dict(parse_qsl(parts.query))
        query["contents"] = "read"
        self._setup_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        self.instructions_label.setText(
            "On GitHub, choose the repository owner, select Only select repositories, "
            "and choose the repository that publishes Studio's updates. "
            "Keep Contents set to Read-only. Choose an expiry date, then select Generate token."
        )


class UpdateController(QObject):
    """Own update workers independently of the reusable modeless dialog.

    Checking only reads GitHub metadata. Downloading and preparing an update
    requires a separate click, and applying it requires ``restart_requested``
    followed by the main window's normal Save/Discard/Cancel shutdown flow.
    """

    state_changed = Signal(str)
    details_changed = Signal()
    restart_requested = Signal(object)
    idle = Signal()

    def __init__(
        self, app_root: Path, settings: QSettings, parent=None,
        session: GitHubSession | None = None,
    ) -> None:
        super().__init__(parent)
        self.app_root = Path(app_root).absolute()
        self.settings = settings
        self.session = session if session is not None else get_session()
        self.current_version = __version__
        # This is Studio's release source, not a user project's repository.
        # Replace old custom values without redisplaying them (an old URL can
        # contain a credential), and retire the automatic-check preference.
        settings.setValue("updates/repository", self.repository)
        settings.remove("updates/check_on_startup")
        self.use_account = settings.value("updates/use_account", False, type=bool)
        self.last_checked = str(settings.value("updates/last_checked", ""))
        if settings.value("updates/last_checked_repository", "") != self.repository:
            self.last_checked = ""
        self.state = "idle"
        self.status_message = "Choose Check for updates to see whether a newer version is available."
        self.release: UpdateRelease | None = None
        self.pending_update: PreparedUpdate | None = None
        self.downloaded_archive: Path | None = None
        self.manual_reason = ""
        self.download_progress: tuple[int, int] | None = None
        self.install_supported, self.install_support_message = installation_support(self.app_root)
        self._worker: _UpdateWorker | None = None
        self._worker_outcome: tuple[str, object] | None = None
        self._worker_account = None
        self._generation = 0
        self._shutting_down = False
        self._restart_emitted = False
        self._dialog: UpdateDialog | None = None
        self._connect_dialog: _UpdateGitHubConnectDialog | None = None
        self._connect_aborted = False
        self._cancel_message_pending = False
        self._dialog_was_visible_before_shutdown = False

    @property
    def repository(self) -> str:
        """Studio always checks its own application repository."""
        return DEFAULT_UPDATE_REPOSITORY

    @property
    def is_active(self) -> bool:
        # A connection dialog owns its own worker; retain it until its finished
        # signal even while it is waiting for cooperative cancellation.
        return self._worker is not None or self._connect_dialog is not None

    @property
    def button_text(self) -> str:
        if self.state == "checking":
            return "Checking…"
        if self.state == "downloading":
            return "Downloading…"
        if self.state == "available":
            return "Update available"
        if self.state == "ready":
            return "Restart to update" if self.pending_update is not None else "Update downloaded"
        return "Update"

    @property
    def button_tooltip(self) -> str:
        return self.status_message

    @property
    def update_is_newer(self) -> bool:
        return UpdateCheck(self.current_version, self.release).is_newer

    @property
    def download_available(self) -> bool:
        return bool(
            self.release is not None
            and self.release.repository.casefold() == self.repository.casefold()
            and UpdateCheck(self.current_version, self.release).download_available
        )

    def _notify(self) -> None:
        self.details_changed.emit()
        self.state_changed.emit(self.state)

    def _set_status(self, state: str, message: str) -> None:
        self.state = state
        self.status_message = message
        self._notify()

    def _account_snapshot(self):
        account = self.session.account or {}
        return (self.session.has_token, account.get("id"), account.get("login"))

    def _clear_result(self) -> None:
        self.release = None
        self.pending_update = None
        self.downloaded_archive = None
        self.manual_reason = ""
        self.download_progress = None
        self._restart_emitted = False

    def _invalidate(self, message: str) -> None:
        self._generation += 1
        self._cancel_message_pending = False
        if self._worker is not None:
            self._worker.request_cancel()
        self._clear_result()
        self.last_checked = ""
        self.settings.remove("updates/last_checked")
        self.settings.remove("updates/last_checked_repository")
        self._set_status("idle", message)

    def set_use_account(self, checked: bool) -> None:
        if self._shutting_down or bool(checked) == self.use_account:
            return
        self.use_account = bool(checked)
        self.settings.setValue("updates/use_account", self.use_account)
        self._invalidate("GitHub access preference saved. Check for updates again.")

    def open_dialog(self) -> None:
        if self._shutting_down:
            return
        if self._dialog is None:
            parent = self.parent() if isinstance(self.parent(), QWidget) else None
            self._dialog = UpdateDialog(self, parent)
        self._dialog.refresh()
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def _start_worker(self, kind: str, operation: Callable) -> None:
        worker = _UpdateWorker(operation, self._generation, kind, self)
        self._cancel_message_pending = False
        self._worker = worker
        self._worker_outcome = None
        self._worker_account = self._account_snapshot() if self.use_account else None
        worker.completed.connect(self._worker_completed)
        worker.failed.connect(self._worker_failed)
        worker.progress.connect(self._worker_progress)
        worker.finished.connect(self._worker_finished)
        self._notify()
        worker.start()

    def check_for_updates(self) -> bool:
        if self.is_active or self._shutting_down:
            return False
        # A new attempt immediately retires the previous result. In particular,
        # losing authentication must not leave a stale Download button enabled.
        self._generation += 1
        self._clear_result()
        if self.use_account and not self.session.has_token:
            self._set_status("error", "Connect GitHub to check a private repository, or clear the account option for a public repository.")
            return False
        repository = self.repository
        session = self.session if self.use_account else None
        current_version = self.current_version
        self._set_status("checking", "Checking for updates…")
        self._start_worker("check", lambda cancelled, _progress: check_for_update(
            repository, current_version, session=session, cancelled=cancelled,
        ))
        return True

    def download(self) -> bool:
        if (
            self.is_active or self._shutting_down or not self.download_available
            or self.pending_update is not None or self.downloaded_archive is not None
        ):
            return False
        if self.use_account and not self.session.has_token:
            self._clear_result()
            self._set_status("error", "The GitHub connection has ended. Connect again and check for updates.")
            return False
        release = self.release
        assert release is not None
        session = self.session if self.use_account else None
        cache_root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)) / "updates"
        app_root = self.app_root
        self._generation += 1
        self.download_progress = None
        self.downloaded_archive = None
        self.manual_reason = ""
        self._set_status("downloading", f"Downloading Studio {release.version}…")

        def download_and_prepare(cancelled, progress):
            archive = download_update(
                release, cache_root / "downloads", session=session, cancelled=cancelled,
                progress=lambda received, total: progress(("bytes", received, total)),
            )
            if cancelled.is_set():
                raise UpdateError("The download was cancelled.", cancelled=True)
            supported, reason = installation_support(app_root)
            if not supported:
                progress(("message", "Verifying the downloaded files for manual installation…"))
                validate_download_archive(
                    archive, release.version, cancelled=cancelled.is_set,
                    progress=lambda message: progress(("message", message)),
                )
                return _DownloadedUpdate(archive, None, reason)
            progress(("message", "Checking the downloaded files and preparing a safe restart…"))
            # Preparation also verifies the archive's contents. A failure here
            # must remain an error; offering manual extraction would bypass a
            # rejected path, checksum or package version validation.
            prepared = prepare_install(
                archive, app_root, release.version, release.repository,
                cancelled=cancelled.is_set,
                progress=lambda message: progress(("message", message)),
            )
            return _DownloadedUpdate(archive, prepared)

        self._start_worker("download", download_and_prepare)
        return True

    @Slot(object)
    def _worker_completed(self, result: object) -> None:
        worker = self.sender()
        if worker is self._worker and worker.generation == self._generation and not worker.cancelled.is_set():
            self._worker_outcome = ("result", result)

    @Slot(str, bool)
    def _worker_failed(self, message: str, cancelled: bool) -> None:
        worker = self.sender()
        if worker is self._worker and worker.generation == self._generation:
            self._worker_outcome = ("cancelled" if cancelled else "error", message)

    @Slot(object)
    def _worker_progress(self, event: object) -> None:
        worker = self.sender()
        if worker is not self._worker or worker.generation != self._generation or worker.cancelled.is_set():
            return
        if isinstance(event, tuple) and event:
            if event[0] == "bytes" and len(event) == 3:
                received, total = max(0, int(event[1])), max(0, int(event[2]))
                self.download_progress = (received, total)
                self.status_message = "Downloading the update…"
            elif event[0] == "message" and len(event) == 2:
                self.download_progress = None
                self.status_message = str(event[1])
            self.details_changed.emit()

    @Slot()
    def _worker_finished(self) -> None:
        worker = self.sender()
        if worker is not self._worker:
            return
        outcome = self._worker_outcome
        valid = worker.generation == self._generation and not worker.cancelled.is_set() and not self._shutting_down
        changed_account = self.use_account and self._worker_account != self._account_snapshot()
        self._worker = None
        self._worker_outcome = None
        self._worker_account = None
        worker.deleteLater()
        if valid and changed_account:
            self._clear_result()
            self._set_status("idle", "The GitHub connection changed. Check for updates again.")
        elif valid and outcome is not None:
            kind, value = outcome
            if kind == "error":
                self._clear_result()
                message = str(value)
                if worker.kind == "download":
                    message += " Choose Check for updates to try again."
                self._set_status("error", message)
            elif kind == "cancelled":
                self._set_status("available" if self.download_available else "idle", "The update operation was cancelled. You can try again.")
            elif isinstance(value, UpdateCheck):
                self._check_ready(value)
            elif isinstance(value, _DownloadedUpdate):
                self._download_ready(value)
            else:
                self._clear_result()
                self._set_status("error", "Studio received an unexpected update result. Check for updates again.")
        elif self._cancel_message_pending and not self._shutting_down:
            self._set_status(
                "available" if self.download_available else "idle",
                "The update operation was cancelled. You can try again.",
            )
        self._cancel_message_pending = False
        self._notify()
        if not self.is_active:
            self.idle.emit()

    def _check_ready(self, result: UpdateCheck) -> None:
        if result.release is not None and result.release.repository.casefold() != self.repository.casefold():
            self._clear_result()
            self._set_status("error", "Studio could not verify the update source. Check for updates again.")
            return
        self.release = result.release
        self.last_checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.settings.setValue("updates/last_checked", self.last_checked)
        self.settings.setValue("updates/last_checked_repository", self.repository)
        if result.status == "available":
            message = f"Studio {result.release.version} is available. Choose Download update."
        elif result.status == "ahead":
            message = f"You have Studio {self.current_version}, which is newer than the latest published release ({result.release.version})."
        elif result.status == "none":
            message = result.message or "No updates are available yet. Check again later."
        elif result.status == "unavailable":
            message = result.message or "The new version is still being prepared. Check again later."
        else:
            message = f"You're up to date. You have Studio {self.current_version}."
        self._set_status(result.status, message)

    def _download_ready(self, result: _DownloadedUpdate) -> None:
        self.downloaded_archive = result.archive
        self.pending_update = result.prepared
        self.manual_reason = result.manual_reason
        self.download_progress = None
        if result.prepared is not None:
            message = f"Studio {result.prepared.version} is ready. Restart to install it. Studio will ask you to save any unsaved changes first."
            if result.prepared.warnings:
                message += "\n" + "\n".join(result.prepared.warnings)
        else:
            message = (
                "Update downloaded. " + (result.manual_reason + " " if result.manual_reason else "")
                + "Open the download folder, extract the ZIP into a new folder, and open HoYo Project Studio.exe."
            )
        self._set_status("ready", message)

    def request_restart(self) -> bool:
        if self.pending_update is None or self.is_active or self._shutting_down or self._restart_emitted:
            return False
        self._restart_emitted = True
        self.details_changed.emit()
        self.restart_requested.emit(self.pending_update)
        return True

    def cancel_restart(self, message: str = "") -> None:
        self._shutting_down = False
        self._restart_emitted = False
        if self.pending_update is not None:
            self._set_status("ready", message or "The update is ready. You can restart when you have finished your work.")
        else:
            self._notify()
        self._restore_dialog_after_shutdown()

    def _restore_dialog_after_shutdown(self) -> None:
        restore = self._dialog_was_visible_before_shutdown
        self._dialog_was_visible_before_shutdown = False
        if restore and self._dialog is not None:
            self.open_dialog()

    def update_launch_failed(self, message: str = "") -> None:
        """A launched/cancelled helper job is single-use; prepare a new one.

        Save/Discard/Cancel calls ``cancel_restart`` instead because it happens
        before launching the helper and the staged job remains reusable.
        """
        self._shutting_down = False
        self._restart_emitted = False
        self.pending_update = None
        self.downloaded_archive = None
        self.manual_reason = ""
        self.download_progress = None
        self._set_status(
            "error", (message or "Studio could not start the update.")
            + " Download the update again to prepare a new restart.",
        )
        self._restore_dialog_after_shutdown()

    def launch_pending_update(self) -> None:
        if self.pending_update is None or not self._restart_emitted:
            raise UpdateInstallError("Choose Restart to update after downloading an available release.")
        if self.is_active:
            raise UpdateInstallError("Wait for the update operation to finish before restarting.")
        launch_prepared_update(self.pending_update)

    def cancel(self) -> bool:
        was_active = self.is_active
        if self._worker is not None:
            self._generation += 1
            self._cancel_message_pending = True
            self._worker.request_cancel()
            self.download_progress = None
            self._set_status("available" if self.download_available else "idle", "Cancelling the update operation…")
        if self._connect_dialog is not None:
            self._connect_aborted = True
            self._connect_dialog.reject()
        return was_active

    def begin_shutdown(self) -> bool:
        if not self._shutting_down:
            self._dialog_was_visible_before_shutdown = bool(self._dialog is not None and self._dialog.isVisible())
        self._shutting_down = True
        connection = self._connect_dialog
        active = self.cancel()
        # A modeless QDialog remains a top-level window even with a parent.
        # Closing only the main window can therefore leave Studio running.
        # Hide both owned windows, while retaining any worker until it drains.
        if connection is not None:
            connection.hide()
        if self._dialog is not None:
            self._dialog.hide()
        self._notify()
        return active

    def connect_github(self) -> None:
        if self.is_active or self._shutting_down or not self.use_account:
            return
        parent = self._dialog
        dialog = _UpdateGitHubConnectDialog(self.session, self.repository, parent)
        self._connect_dialog = dialog
        self._connect_aborted = False
        dialog.finished.connect(self._connection_finished)
        self._notify()
        dialog.open()

    @Slot(int)
    def _connection_finished(self, result: int) -> None:
        dialog, self._connect_dialog = self._connect_dialog, None
        if dialog is not None:
            dialog.deleteLater()
        if result == QDialog.DialogCode.Accepted and not self._connect_aborted and not self._shutting_down:
            account = self.session.account or {}
            login = account.get("login", "your GitHub account")
            self._invalidate(f"Connected as {login}. Choose Check for updates.")
        else:
            self._notify()
        if not self.is_active:
            self.idle.emit()

    def open_release(self) -> bool:
        if self.release is None:
            return False
        url = self.release.release_url
        try:
            parsed = urlsplit(url)
        except ValueError:
            self._set_status("error", "The release link is invalid. Check for updates again.")
            return False
        prefix = "/" + self.release.repository + "/releases/tag/"
        if (
            parsed.scheme != "https" or parsed.netloc.lower() != "github.com"
            or not parsed.path.lower().startswith(prefix.lower())
            or parsed.query or parsed.fragment
            or "\\" in url or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            self._set_status("error", "The release link is invalid. Check for updates again.")
            return False
        if not QDesktopServices.openUrl(QUrl(url)):
            self._set_status("error", "Could not open your browser. Open this release manually:\n" + url)
            return False
        return True

    def open_download_folder(self) -> bool:
        if self.downloaded_archive is None or not self.downloaded_archive.is_file():
            if self.pending_update is None:
                self.downloaded_archive = None
            self._set_status("error", "The downloaded ZIP is no longer available. Download the update again.")
            return False
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.downloaded_archive.parent))):
            self._set_status("error", "Could not open the download folder:\n" + str(self.downloaded_archive.parent))
            return False
        return True


def _plain_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class UpdateDialog(QDialog):
    def __init__(self, controller: UpdateController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Studio updates")
        self.setObjectName("StudioUpdateDialog")
        self.setMinimumWidth(650)
        self.resize(730, 620)
        # Keep the action state clear even when Studio's shared primary-button
        # stylesheet colors disabled controls blue. Hover applies only when an
        # action is enabled; a disabled Download stays visibly grey.
        self.setStyleSheet("""
            QDialog#StudioUpdateDialog QPushButton#UpdateDownloadButton:enabled,
            QDialog#StudioUpdateDialog QPushButton#UpdateRestartButton:enabled {
                background: #3b6db5;
                color: #ffffff;
                border: 1px solid #5084cb;
                border-radius: 7px;
                padding: 8px 13px;
                font-weight: 600;
            }
            QDialog#StudioUpdateDialog QPushButton#UpdateDownloadButton:enabled:hover,
            QDialog#StudioUpdateDialog QPushButton#UpdateRestartButton:enabled:hover {
                background: #4d80d1;
                border-color: #6d9dea;
            }
            QDialog#StudioUpdateDialog QPushButton#UpdateDownloadButton:enabled:pressed,
            QDialog#StudioUpdateDialog QPushButton#UpdateRestartButton:enabled:pressed {
                background: #2e5590;
            }
            QDialog#StudioUpdateDialog QPushButton#UpdateDownloadButton:disabled,
            QDialog#StudioUpdateDialog QPushButton#UpdateDownloadButton:disabled:hover,
            QDialog#StudioUpdateDialog QPushButton#UpdateRestartButton:disabled,
            QDialog#StudioUpdateDialog QPushButton#UpdateRestartButton:disabled:hover {
                background: #30343b;
                color: #9298a2;
                border: 1px solid #444a54;
                border-radius: 7px;
                padding: 8px 13px;
                font-weight: 600;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        title = _plain_label("Studio updates")
        title.setObjectName("InspectorTitle")
        layout.addWidget(title)
        layout.addWidget(_plain_label("Check for a new version, then download it when you're ready."))

        form = QFormLayout()
        self.repository_label = _plain_label(controller.repository)
        self.repository_label.setObjectName("UpdateRepository")
        form.addRow("Application repository", self.repository_label)
        layout.addLayout(form)

        account_row = QHBoxLayout()
        self.account_checkbox = QCheckBox("Use my connected GitHub account")
        self.account_checkbox.setChecked(controller.use_account)
        account_row.addWidget(self.account_checkbox)
        account_row.addStretch()
        self.connect_button = QPushButton("Connect GitHub…")
        account_row.addWidget(self.connect_button)
        layout.addLayout(account_row)
        self.account_label = _plain_label()
        self.account_label.setStyleSheet("color: #a9bbd8;")
        layout.addWidget(self.account_label)

        versions = QFormLayout()
        self.current_version_label = _plain_label(controller.current_version)
        self.latest_version_label = _plain_label("Not checked")
        versions.addRow("Installed version", self.current_version_label)
        versions.addRow("Latest release", self.latest_version_label)
        layout.addLayout(versions)
        self.status_label = _plain_label()
        self.status_label.setObjectName("UpdateStatus")
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)
        layout.addWidget(self.progress_bar)
        self.progress_label = _plain_label()
        self.progress_label.setStyleSheet("color: #a9bbd8;")
        layout.addWidget(self.progress_label)
        self.last_checked_label = _plain_label()
        self.last_checked_label.setStyleSheet("color: #a9bbd8;")
        layout.addWidget(self.last_checked_label)

        layout.addWidget(_plain_label("Release notes"))
        self.notes = QPlainTextEdit()
        self.notes.setObjectName("UpdateReleaseNotes")
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText("Release notes will appear after checking for updates.")
        self.notes.setMinimumHeight(120)
        layout.addWidget(self.notes, 1)
        links = QHBoxLayout()
        self.release_button = QPushButton("Open release on GitHub")
        self.folder_button = QPushButton("Open download folder")
        links.addWidget(self.release_button)
        links.addWidget(self.folder_button)
        links.addStretch()
        layout.addLayout(links)

        actions = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel operation")
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        self.check_button = QPushButton("Check for updates")
        self.download_button = QPushButton("Download update")
        self.download_button.setObjectName("UpdateDownloadButton")
        self.restart_button = QPushButton("Restart to update")
        self.restart_button.setObjectName("UpdateRestartButton")
        self.close_button = QPushButton("Close")
        actions.addWidget(self.check_button)
        actions.addWidget(self.download_button)
        actions.addWidget(self.restart_button)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

        self.account_checkbox.toggled.connect(controller.set_use_account)
        self.connect_button.clicked.connect(controller.connect_github)
        self.check_button.clicked.connect(controller.check_for_updates)
        self.download_button.clicked.connect(controller.download)
        self.restart_button.clicked.connect(controller.request_restart)
        self.cancel_button.clicked.connect(controller.cancel)
        self.release_button.clicked.connect(controller.open_release)
        self.folder_button.clicked.connect(controller.open_download_folder)
        self.close_button.clicked.connect(self.reject)
        controller.details_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        controller = self.controller
        busy = controller.is_active
        self.status_label.setText(controller.status_message)
        self.status_label.setStyleSheet("color: #ffb5c5;" if controller.state == "error" else "")
        latest = controller.release.version if controller.release else "No published update" if controller.state == "none" else "Not checked"
        self.latest_version_label.setText(latest)
        notes = controller.release.notes[:60_000] if controller.release else ""
        if self.notes.toPlainText() != notes:
            self.notes.setPlainText(notes)
        self.account_checkbox.setEnabled(not controller._shutting_down)
        self.connect_button.setVisible(controller.use_account)
        self.connect_button.setEnabled(not busy and not controller._shutting_down)
        self.connect_button.setText("Change account…" if controller.session.has_token else "Connect GitHub…")
        if controller.use_account:
            account = controller.session.account or {}
            if account.get("login"):
                account_text = f"Connected as {account['login']}."
            elif controller.session.has_token:
                account_text = "A saved GitHub connection is available."
            else:
                account_text = "Connect GitHub if this repository is private."
            if controller.session.storage_warning:
                account_text += " " + controller.session.storage_warning
        else:
            account_text = "Sign-in is only needed if this repository is private."
        self.account_label.setText(account_text)
        self.check_button.setEnabled(not busy and not controller._shutting_down)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy and not controller._shutting_down)
        has_ready = controller.pending_update is not None
        self.restart_button.setVisible(has_ready)
        self.restart_button.setEnabled(has_ready and not busy and not controller._restart_emitted and not controller._shutting_down)
        self.download_button.setVisible(not has_ready and controller.downloaded_archive is None)
        can_download = (
            not busy and controller.download_available and not has_ready
            and controller.downloaded_archive is None and not controller._shutting_down
        )
        self.download_button.setEnabled(can_download)
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor if can_download else Qt.CursorShape.ArrowCursor)
        self.download_button.setToolTip("Download this update" if can_download else "Download is available after a newer update is ready.")
        self.release_button.setEnabled(controller.release is not None)
        self.folder_button.setVisible(controller.downloaded_archive is not None)
        self.folder_button.setEnabled(not busy)
        self.progress_bar.setVisible(busy)
        progress = controller.download_progress
        if progress and progress[1] > 0:
            received, total = progress
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, round(received / total * 1000)))
            self.progress_label.setText(f"{received / 1_048_576:.1f} MB of {total / 1_048_576:.1f} MB")
            self.progress_label.show()
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_label.hide()
        if controller.last_checked:
            try:
                checked = datetime.fromisoformat(controller.last_checked).astimezone().strftime("%d %b %Y, %H:%M")
            except ValueError:
                checked = ""
            self.last_checked_label.setText("Last checked: " + checked if checked else "")
        else:
            self.last_checked_label.setText("Not checked yet")

    def reject(self) -> None:
        # Hiding the dialog never destroys a running worker. The controller
        # remains alive and emits idle after the worker has actually finished.
        if self.controller.is_active:
            self.controller.cancel()
        super().reject()

    def closeEvent(self, event) -> None:
        self.reject()
        event.accept()
