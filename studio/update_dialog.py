"""Responsive GitHub update checks and user-initiated desktop installations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import stat
import threading
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import QObject, QSettings, QStandardPaths, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFontDatabase, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from .github_connection import GitHubError, GitHubSession, get_session
from .publish_dialog import GitHubConnectDialog
from .update_compare import ComparisonError, ReleaseArchiveError, ReleaseComparison, compare_release_archive
from .update_install import (
    PreparedUpdate, UpdateInstallError, installation_support,
    launch_prepared_update, prepare_install, validate_download_archive,
)
from .update_service import (
    UpdateCheck, UpdateError, UpdateRelease, check_for_update,
    download_update, resolve_release_package, verify_cached_download,
)


DEFAULT_UPDATE_REPOSITORY = "PositionV2024/studio"


@dataclass(frozen=True)
class _DownloadedUpdate:
    archive: Path
    prepared: PreparedUpdate | None
    manual_reason: str = ""


@dataclass(frozen=True)
class _CheckedUpdate:
    check: UpdateCheck
    comparison: ReleaseComparison | None = None
    archive: Path | None = None
    comparison_error: str = ""
    archive_rejected: bool = False


def _remove_comparison_download(archive: Path, cache_root: Path) -> None:
    """Remove only our single comparison ZIP and its now-empty private folder.

    Never recurse through a cache folder, follow a link, or touch an arbitrary
    path returned by an unexpected service. Explicit user downloads are retained
    by the controller and never passed here.
    """
    try:
        archive, cache_root = Path(archive).absolute(), Path(cache_root).absolute()
        if archive.parent.parent != cache_root or not archive.parent.name.startswith("studio-download-"):
            return
        for candidate in (cache_root, archive.parent, archive):
            information = candidate.lstat()
            if stat.S_ISLNK(information.st_mode) or getattr(information, "st_file_attributes", 0) & 0x400:
                return
        if not stat.S_ISREG(archive.lstat().st_mode):
            return
        archive.unlink()
        archive.parent.rmdir()
    except OSError:
        # A moved cache, an in-use file or another file placed in its directory
        # must not obstruct Studio shutdown or make cleanup broader.
        pass


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
        self.result = None

    def request_cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        try:
            result = self.operation(self.cancelled, self.progress.emit)
            self.result = result
            if self.cancelled.is_set():
                self.failed.emit("", True)
            else:
                self.completed.emit(result)
        except (UpdateError, UpdateInstallError, GitHubError, ComparisonError) as error:
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

    Checking reads the release package into a cache to compare saved files.
    Preparing an installation requires a separate click, and applying it requires ``restart_requested``
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
        settings.remove("updates/use_account")
        self.last_checked = str(settings.value("updates/last_checked", ""))
        if settings.value("updates/last_checked_repository", "") != self.repository:
            self.last_checked = ""
        self.state = "idle"
        self.status_message = (
            "Choose Check for updates to compare your saved application files with the latest release."
            if self._account_snapshot() is not None else
            "Connect your GitHub account to check for updates and compare application files."
        )
        self.release: UpdateRelease | None = None
        self.pending_update: PreparedUpdate | None = None
        self.downloaded_archive: Path | None = None
        self.manual_reason = ""
        self.download_progress: tuple[int, int] | None = None
        self.comparison: ReleaseComparison | None = None
        self.comparison_error = ""
        self._comparison_cancelled = False
        self._archive_rejected = False
        self._comparison_archive: Path | None = None
        self._comparison_release: UpdateRelease | None = None
        self._result_account = None
        self._download_cache = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)) / "updates" / "downloads"
        self._comparison_cleanup_pending: set[Path] = set()
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
        self._observed_account = self._account_snapshot()
        # Publishing and Updates share a thread-safe session without a Qt
        # notification signal. Detect account/token replacement while idle as
        # well as at every action and worker boundary; this never uses network.
        self._account_timer = QTimer(self)
        self._account_timer.setInterval(500)
        self._account_timer.timeout.connect(self._refresh_connection)
        self._account_timer.start()

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
            self.has_connection
            and self._result_account == self._account_snapshot()
            and self.release is not None
            and not self._archive_rejected
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
        try:
            if not self.session.has_token:
                return None
            token = self.session.get_token()
            if not isinstance(token, str) or not token:
                return None
            # Retain only a fingerprint. Tokens must never enter Qt signals,
            # persisted settings, comparison models, status text or logs.
            return hashlib.sha256(token.encode("utf-8")).digest(), getattr(self.session, "_generation", None)
        except Exception:
            return None

    @property
    def has_connection(self) -> bool:
        return self._account_snapshot() is not None

    def _sync_connection(self) -> bool:
        current = self._account_snapshot()
        if current == self._observed_account:
            return False
        self._observed_account = current
        message = (
            "The GitHub connection changed. Check for updates again."
            if current is not None else
            "The GitHub connection has ended. Connect again and check for updates."
        )
        self._invalidate(message)
        return True

    @Slot()
    def _refresh_connection(self) -> None:
        if self._shutting_down:
            return
        if not self._sync_connection() and self._dialog is not None and self._dialog.isVisible():
            self.details_changed.emit()

    def _clear_result(self, *, keep_release: bool = False) -> None:
        self._release_comparison_cache()
        if not keep_release:
            self.release = None
        self.pending_update = None
        self.downloaded_archive = None
        self.manual_reason = ""
        self.download_progress = None
        self.comparison = None
        self.comparison_error = ""
        self._comparison_cancelled = False
        self._archive_rejected = False
        self._comparison_archive = None
        self._comparison_release = None
        self._result_account = None
        self._restart_emitted = False

    def _release_comparison_cache(self) -> None:
        archive = self._comparison_archive
        protected = {self.downloaded_archive, self.pending_update.archive if self.pending_update is not None else None}
        if archive is not None and archive not in protected:
            if self._worker is not None:
                self._comparison_cleanup_pending.add(archive)
            else:
                _remove_comparison_download(archive, self._download_cache)

    def _drain_comparison_cleanup(self) -> None:
        if self._worker is not None:
            return
        protected = {self.downloaded_archive, self.pending_update.archive if self.pending_update is not None else None}
        for archive in self._comparison_cleanup_pending:
            if archive not in protected:
                _remove_comparison_download(archive, self._download_cache)
        self._comparison_cleanup_pending.clear()

    def _invalidate(self, message: str) -> None:
        self._generation += 1
        self._cancel_message_pending = False
        if self._worker is not None:
            self._worker.request_cancel()
        self._clear_result()
        self.comparison_error = message
        self.last_checked = ""
        self.settings.remove("updates/last_checked")
        self.settings.remove("updates/last_checked_repository")
        self._set_status("idle", message)

    def open_dialog(self) -> None:
        if self._shutting_down:
            return
        self._sync_connection()
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
        self._worker_account = self._account_snapshot()
        worker.completed.connect(self._worker_completed)
        worker.failed.connect(self._worker_failed)
        worker.progress.connect(self._worker_progress)
        worker.finished.connect(self._worker_finished)
        self._notify()
        worker.start()

    def check_for_updates(self) -> bool:
        if self.is_active or self._shutting_down:
            return False
        self._sync_connection()
        # A new attempt immediately retires the previous result. In particular,
        # losing authentication must not leave a stale Download button enabled.
        self._generation += 1
        self._clear_result()
        if not self.has_connection:
            self._set_status("error", "Connect your GitHub account before checking for updates.")
            return False
        repository = self.repository
        session = self.session
        current_version = self.current_version
        app_root = self.app_root
        account = self._account_snapshot()
        cache_root = self._download_cache
        self._set_status("checking", "Checking for updates…")

        def check_and_compare(cancelled, progress):
            def require_same_connection():
                if cancelled.is_set():
                    raise UpdateError("The update operation was cancelled.", cancelled=True)
                if account is None or account != self._account_snapshot():
                    raise UpdateError("The GitHub connection changed. Check for updates again.")

            require_same_connection()
            result = check_for_update(repository, current_version, session=session, cancelled=cancelled)
            require_same_connection()
            if result.release is None:
                return _CheckedUpdate(result, comparison_error="No published release is available for file comparison.")
            if result.release.repository.casefold() != repository.casefold():
                raise UpdateError("Studio could not verify the update source. Check for updates again.")
            # Metadata is useful even when the package cannot be compared.
            # This is display-only: _result_account remains unset until the
            # completed check, so no partial result can enable installation.
            progress(("release", result.release))
            archive = None
            keep_archive = False
            try:
                progress(("message", "Finding the release package for file comparison…"))
                resolved = resolve_release_package(result.release, session=session, cancelled=cancelled)
                require_same_connection()
                result = UpdateCheck(current_version, resolved)
                progress(("release", resolved))
                progress(("message", "Reading the release package to compare saved application files…"))
                archive = download_update(
                    resolved, cache_root, session=session, cancelled=cancelled,
                    progress=lambda received, total: progress(("bytes", received, total)),
                )
                require_same_connection()
                comparison = compare_release_archive(
                    archive, app_root, resolved.version, current_version=current_version,
                    cancelled=cancelled.is_set,
                    progress=lambda message: progress(("message", message)),
                )
                require_same_connection()
                keep_archive = True
                return _CheckedUpdate(result, comparison, archive)
            except (UpdateError, UpdateInstallError, ComparisonError, GitHubError) as error:
                require_same_connection()
                if getattr(error, "cancelled", False):
                    raise
                return _CheckedUpdate(
                    result, comparison_error=str(error),
                    archive_rejected=isinstance(error, ReleaseArchiveError),
                )
            except Exception:
                require_same_connection()
                return _CheckedUpdate(
                    result, comparison_error="The files could not be compared. Check your connection and available disk space, then check again.",
                )
            finally:
                if archive is not None and not keep_archive:
                    _remove_comparison_download(archive, cache_root)

        self._start_worker("check", check_and_compare)
        return True

    def download(self) -> bool:
        if self._shutting_down:
            return False
        self._sync_connection()
        if not self.has_connection:
            self._clear_result()
            self._set_status("error", "Connect your GitHub account before downloading an update.")
            return False
        if (
            self.is_active or not self.download_available
            or self.pending_update is not None or self.downloaded_archive is not None
        ):
            return False
        release = self.release
        assert release is not None
        session = self.session
        account = self._account_snapshot()
        cached_archive = self._comparison_archive if self._comparison_release == release else None
        cache_root = self._download_cache
        app_root = self.app_root
        self._generation += 1
        self.download_progress = None
        self.downloaded_archive = None
        self.manual_reason = ""
        self._set_status("downloading", f"Preparing Studio {release.version} from the checked package…" if cached_archive else f"Downloading Studio {release.version}…")

        def download_and_prepare(cancelled, progress):
            def require_same_connection():
                if cancelled.is_set():
                    raise UpdateError("The download was cancelled.", cancelled=True)
                if account is None or account != self._account_snapshot():
                    raise UpdateError("The GitHub connection changed. Check for updates again.")

            require_same_connection()
            archive = None
            if cached_archive is not None:
                progress(("message", "Verifying the release package already read for comparison…"))
                if verify_cached_download(release, cached_archive, session=session, cancelled=cancelled):
                    archive = cached_archive
            if archive is None:
                require_same_connection()
                progress(("message", f"Downloading Studio {release.version}…"))
                archive = download_update(
                    release, cache_root, session=session, cancelled=cancelled,
                    progress=lambda received, total: progress(("bytes", received, total)),
                )
            require_same_connection()
            supported, reason = installation_support(app_root)
            if not supported:
                progress(("message", "Verifying the downloaded files for manual installation…"))
                validate_download_archive(
                    archive, release.version, cancelled=cancelled.is_set,
                    progress=lambda message: progress(("message", message)),
                )
                require_same_connection()
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
            require_same_connection()
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
        if self._worker_account is None or self._worker_account != self._account_snapshot():
            return
        if isinstance(event, tuple) and event:
            if event[0] == "bytes" and len(event) == 3:
                received, total = max(0, int(event[1])), max(0, int(event[2]))
                self.download_progress = (received, total)
                self.status_message = (
                    "Reading the release package for file comparison…"
                    if worker.kind == "check" else "Downloading the update…"
                )
            elif event[0] == "message" and len(event) == 2:
                self.download_progress = None
                self.status_message = str(event[1])
            elif event[0] == "release" and len(event) == 2 and worker.kind == "check":
                release = event[1]
                if isinstance(release, UpdateRelease) and release.repository.casefold() == self.repository.casefold():
                    self.release = release
            self.details_changed.emit()

    @Slot()
    def _worker_finished(self) -> None:
        worker = self.sender()
        if worker is not self._worker:
            return
        outcome = self._worker_outcome
        valid = worker.generation == self._generation and not worker.cancelled.is_set() and not self._shutting_down
        changed_account = self._worker_account is None or self._worker_account != self._account_snapshot()
        self._worker = None
        self._worker_outcome = None
        self._worker_account = None
        worker.deleteLater()
        if (not valid or changed_account) and isinstance(worker.result, _CheckedUpdate) and worker.result.archive is not None:
            _remove_comparison_download(worker.result.archive, self._download_cache)
        if valid and changed_account:
            self._clear_result()
            self.comparison_error = "The GitHub connection changed. Check for updates again."
            self._set_status("idle", self.comparison_error)
        elif valid and outcome is not None:
            kind, value = outcome
            if kind == "error":
                # A rejected ZIP must retire installation eligibility, but
                # must not erase the release we already fetched or replace a
                # real error with the initial File changes instructions.
                self._clear_result(keep_release=True)
                message = str(value)
                if worker.kind == "download":
                    message += " Choose Check for updates to try again."
                self.comparison_error = message
                self._set_status("error", message)
            elif kind == "cancelled":
                if worker.kind == "check":
                    self.comparison_error = "The comparison was cancelled. Choose Check for updates to compare the release files again."
                    self._comparison_cancelled = True
                self._set_status("available" if self.download_available else "idle", "The update operation was cancelled. You can try again.")
            elif isinstance(value, _CheckedUpdate):
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
        self._drain_comparison_cleanup()
        if self._shutting_down:
            self._release_comparison_cache()
        self._notify()
        if not self.is_active:
            self.idle.emit()

    def _check_ready(self, outcome: _CheckedUpdate) -> None:
        result = outcome.check
        if result.release is not None and result.release.repository.casefold() != self.repository.casefold():
            self._clear_result()
            self._set_status("error", "Studio could not verify the update source. Check for updates again.")
            return
        self.release = result.release
        self._result_account = self._account_snapshot()
        self.comparison = outcome.comparison
        self.comparison_error = outcome.comparison_error
        self._comparison_cancelled = False
        self._archive_rejected = outcome.archive_rejected
        self._comparison_archive = outcome.archive if outcome.comparison is not None else None
        self._comparison_release = result.release if self._comparison_archive is not None else None
        self.download_progress = None
        self.last_checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.settings.setValue("updates/last_checked", self.last_checked)
        self.settings.setValue("updates/last_checked_repository", self.repository)
        state = result.status
        if result.status == "available" and outcome.archive_rejected:
            state = "unavailable"
            message = (
                f"Studio {result.release.version} was found, but its package could not be verified. "
                "See File changes for the reason, then check again after the release is corrected."
            )
        elif result.status == "available":
            message = f"Studio {result.release.version} is available. Choose Download update."
            if outcome.comparison_error:
                message += " File comparison is unavailable; see the reason below."
        elif result.status == "ahead":
            message = f"You have Studio {self.current_version}, which is newer than the latest published release ({result.release.version})."
        elif result.status == "none":
            message = result.message or "No updates are available yet. Check again later."
        elif result.status == "unavailable":
            message = result.message or "The new version is still being prepared. Check again later."
        else:
            message = f"You're up to date. You have Studio {self.current_version}."
        self._set_status(state, message)

    def _download_ready(self, result: _DownloadedUpdate) -> None:
        if self._comparison_archive is not None and self._comparison_archive != result.archive:
            self._release_comparison_cache()
            self._comparison_archive = None
            self._comparison_release = None
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
        self._sync_connection()
        if self.pending_update is None or self.is_active or self._shutting_down or self._restart_emitted:
            return False
        if not self.has_connection or self._result_account != self._account_snapshot():
            return False
        self._restart_emitted = True
        self.details_changed.emit()
        self.restart_requested.emit(self.pending_update)
        return True

    def cancel_restart(self, message: str = "") -> None:
        self._shutting_down = False
        self._account_timer.start()
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
        self._account_timer.start()
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
        if not self.has_connection or self._result_account != self._account_snapshot():
            raise UpdateInstallError("The GitHub connection changed. Reconnect and check for updates again.")
        if self.pending_update is None or not self._restart_emitted:
            raise UpdateInstallError("Choose Restart to update after downloading an available release.")
        if self.is_active:
            raise UpdateInstallError("Wait for the update operation to finish before restarting.")
        launch_prepared_update(self.pending_update)

    def cancel(self) -> bool:
        was_active = self.is_active
        if self._worker is not None:
            if self._worker.kind == "check":
                self.comparison_error = "The comparison was cancelled. Choose Check for updates to compare the release files again."
                self._comparison_cancelled = True
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
        self._account_timer.stop()
        connection = self._connect_dialog
        active = self.cancel()
        # A modeless QDialog remains a top-level window even with a parent.
        # Closing only the main window can therefore leave Studio running.
        # Hide both owned windows, while retaining any worker until it drains.
        if connection is not None:
            connection.hide()
        if self._dialog is not None:
            self._dialog.hide()
        if not active:
            self._release_comparison_cache()
        self._notify()
        return active

    def connect_github(self) -> None:
        if self.is_active or self._shutting_down:
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
            self._observed_account = self._account_snapshot()
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


class _DiffHighlighter(QSyntaxHighlighter):
    """Color plain unified text without interpreting repository content as HTML."""

    def highlightBlock(self, text: str) -> None:
        color = None
        if text.startswith(("+++ ", "--- ")):
            color = "#aac6f6"
        elif text.startswith("@@"):
            color = "#c5a7ff"
        elif text.startswith("+"):
            color = "#9bdcaa"
        elif text.startswith("-"):
            color = "#ffb0ae"
        if color:
            style = QTextCharFormat()
            style.setForeground(QColor(color))
            self.setFormat(0, len(text), style)


class UpdateDialog(QDialog):
    def __init__(self, controller: UpdateController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Studio updates")
        self.setObjectName("StudioUpdateDialog")
        self.setMinimumSize(780, 600)
        available = self.screen().availableGeometry()
        self.resize(max(780, min(1000, available.width() - 40)), max(600, min(720, available.height() - 60)))
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
        layout.addWidget(_plain_label("Use your connected GitHub account to check releases and review file changes."))

        form = QFormLayout()
        self.repository_label = _plain_label(controller.repository)
        self.repository_label.setObjectName("UpdateRepository")
        form.addRow("Application repository", self.repository_label)
        layout.addLayout(form)

        account_row = QHBoxLayout()
        self.account_label = _plain_label()
        self.account_label.setObjectName("UpdateConnectedAccount")
        self.account_label.setStyleSheet("color: #a9bbd8;")
        account_row.addWidget(self.account_label, 1)
        account_row.addStretch()
        self.connect_button = QPushButton("Connect GitHub…")
        account_row.addWidget(self.connect_button)
        layout.addLayout(account_row)

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

        self.tabs = QTabWidget()
        self.tabs.setObjectName("UpdateDetailsTabs")
        comparison_page = QWidget()
        comparison_layout = QVBoxLayout(comparison_page)
        comparison_layout.setContentsMargins(12, 12, 12, 12)
        self.comparison_direction = _plain_label()
        self.comparison_direction.setObjectName("UpdateComparisonDirection")
        comparison_layout.addWidget(self.comparison_direction)
        self.comparison_summary = _plain_label()
        self.comparison_summary.setObjectName("UpdateComparisonSummary")
        self.comparison_summary.setStyleSheet("font-weight: 600;")
        comparison_layout.addWidget(self.comparison_summary)
        self.comparison_context = QPlainTextEdit()
        self.comparison_context.setObjectName("UpdateComparisonContext")
        self.comparison_context.setReadOnly(True)
        self.comparison_context.setFixedHeight(64)
        self.comparison_context.setStyleSheet("QPlainTextEdit { color: #a9bbd8; background: transparent; border: none; }")
        comparison_layout.addWidget(self.comparison_context)
        self.change_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.change_list = QTreeWidget()
        self.change_list.setObjectName("UpdateChangedFiles")
        self.change_list.setHeaderLabels(["Change", "Application file"])
        self.change_list.setRootIsDecorated(False)
        self.change_list.setUniformRowHeights(True)
        self.change_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.change_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.change_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.change_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.change_list.setMinimumWidth(250)
        self.change_splitter.addWidget(self.change_list)
        self.diff = QPlainTextEdit()
        self.diff.setObjectName("UpdateFileDiff")
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.diff.setStyleSheet("QPlainTextEdit { background: #18212e; color: #dce5f2; }")
        self.diff.setPlaceholderText("Select a changed file to review its saved desktop contents against the release.")
        self._diff_highlighter = _DiffHighlighter(self.diff.document())
        self.change_splitter.addWidget(self.diff)
        self.change_splitter.setSizes([330, 590])
        self.change_splitter.setStretchFactor(0, 1)
        self.change_splitter.setStretchFactor(1, 2)
        comparison_layout.addWidget(self.change_splitter, 1)
        self.comparison_empty_state = _plain_label()
        self.comparison_empty_state.setObjectName("UpdateComparisonEmptyState")
        self.comparison_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.comparison_empty_state.setStyleSheet("color: #a9bbd8; padding: 16px;")
        comparison_layout.addWidget(self.comparison_empty_state, 1)
        self.tabs.addTab(comparison_page, "File changes")
        self.notes = QPlainTextEdit()
        self.notes.setObjectName("UpdateReleaseNotes")
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText("Release notes will appear after checking for updates.")
        self.notes.setMinimumHeight(120)
        self.tabs.addTab(self.notes, "Release notes")
        self.tabs.setMinimumHeight(220)
        layout.addWidget(self.tabs, 1)
        self._shown_comparison = None
        self.change_list.currentItemChanged.connect(self._show_file_change)
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
        if not controller._shutting_down and controller._sync_connection():
            return
        busy = controller.is_active
        connected = controller.has_connection
        self.status_label.setText(controller.status_message)
        self.status_label.setStyleSheet("color: #ffb5c5;" if controller.state == "error" else "")
        latest = (
            controller.release.version if controller.release else
            "No published update" if controller.state == "none" else
            "Checking…" if controller.state == "checking" else
            "Could not check" if controller.state == "error" else
            "Not checked"
        )
        self.latest_version_label.setText(latest)
        notes = controller.release.notes[:60_000] if controller.release else ""
        if self.notes.toPlainText() != notes:
            self.notes.setPlainText(notes)
        self.connect_button.setVisible(True)
        self.connect_button.setEnabled(not busy and not controller._shutting_down)
        self.connect_button.setText("Change account…" if connected else "Connect GitHub…")
        account = controller.session.account or {}
        if connected and account.get("login"):
            account_text = f"Connected as {account['login']}."
        elif connected:
            account_text = "Using your saved GitHub connection."
        else:
            account_text = "GitHub connection required. Connect your account to continue."
        if controller.session.storage_warning:
            account_text += " " + controller.session.storage_warning
        self.account_label.setText(account_text)
        self.check_button.setEnabled(connected and not busy and not controller._shutting_down)
        self.check_button.setToolTip(
            "Check the latest release and read its package to compare saved application files."
            if connected else "Connect your GitHub account first."
        )
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy and not controller._shutting_down)
        has_ready = controller.pending_update is not None
        self.restart_button.setVisible(has_ready)
        self.restart_button.setEnabled(connected and has_ready and not busy and not controller._restart_emitted and not controller._shutting_down)
        self.download_button.setVisible(not has_ready and controller.downloaded_archive is None)
        can_download = (
            not busy and controller.download_available and not has_ready
            and controller.downloaded_archive is None and not controller._shutting_down
        )
        self.download_button.setEnabled(can_download)
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor if can_download else Qt.CursorShape.ArrowCursor)
        self.download_button.setToolTip(
            "Download this update" if can_download else
            "The release package could not be verified. See File changes for the reason." if controller._archive_rejected else
            "Download is available after a newer update is ready."
        )
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
        self._refresh_comparison()

    def _refresh_comparison(self) -> None:
        controller = self.controller
        comparison = controller.comparison
        release = controller.release
        if release is not None:
            self.comparison_direction.setText(f"Current desktop {controller.current_version} → release {release.version}")
        else:
            self.comparison_direction.setText("Current desktop → latest published release")
        if comparison is not self._shown_comparison:
            self._shown_comparison = comparison
            self.change_list.clear()
            self.diff.clear()
            if comparison is not None:
                colors = {"added": "#9bdcaa", "modified": "#f1d49a", "removed": "#ffb0ae", "unavailable": "#c1c9d7"}
                for index, change in enumerate(comparison.changes):
                    item = QTreeWidgetItem([change.status.capitalize(), change.path])
                    item.setData(0, Qt.ItemDataRole.UserRole, index)
                    item.setForeground(0, QColor(colors.get(change.status, "#c1c9d7")))
                    item.setToolTip(1, change.path + ("\nThis desktop file has local changes." if change.locally_modified else ""))
                    self.change_list.addTopLevelItem(item)
                if self.change_list.topLevelItemCount():
                    self.change_list.setCurrentItem(self.change_list.topLevelItem(0))
        has_changes = comparison is not None and bool(comparison.changes)
        self.change_splitter.setVisible(has_changes)
        self.comparison_empty_state.setVisible(not has_changes)
        attention = False
        if comparison is not None:
            counts = {status: sum(change.status == status for change in comparison.changes) for status in ("added", "modified", "removed", "unavailable")}
            text = f"{counts['added']} added · {counts['modified']} modified · {counts['removed']} removed · {comparison.unchanged_count} unchanged"
            if counts["unavailable"]:
                text += f" · {counts['unavailable']} unavailable"
            if not comparison.changes:
                text = "No application file differences. " + text
            self.comparison_summary.setText(text)
            context = "\n".join(comparison.warnings) or "Comparison uses saved application files. Unsaved editor changes are not included."
            empty = "The saved application files match this release."
        elif controller.comparison_error:
            cancelled = controller._comparison_cancelled
            connection_changed = controller.state == "idle" and not cancelled
            self.comparison_summary.setText(
                "File comparison cancelled" if cancelled else
                "Check again to compare application files." if connection_changed and controller.has_connection else
                "Connect GitHub to review application file changes." if connection_changed else
                "File comparison unavailable"
            )
            context = controller.comparison_error
            attention = not cancelled and not connection_changed
            empty = (
                "Check for updates again when you are ready to review the files." if cancelled else
                "Choose Check for updates to load file changes using this GitHub connection." if connection_changed and controller.has_connection else
                "Connect your GitHub account, then choose Check for updates." if connection_changed else
                "The release package was not accepted. No update has been prepared." if controller._archive_rejected else
                "No file comparison was completed. Review the reason above, then check again."
            )
        elif controller.state == "checking":
            self.comparison_summary.setText("Reading and comparing the latest release…")
            context = controller.status_message
            empty = "The file list and text differences will appear here when the comparison finishes."
        elif controller.state == "error":
            self.comparison_summary.setText("File comparison unavailable")
            context = controller.status_message
            attention = True
            empty = "No file comparison was completed. Review the reason above, then check again."
        elif not controller.has_connection:
            self.comparison_summary.setText("Connect GitHub to review application file changes.")
            context = "Use the same GitHub connection as publishing, then choose Check for updates."
            empty = "Added, modified and removed application files will appear here."
        else:
            self.comparison_summary.setText("Check for updates to review added, modified and removed application files.")
            context = "Comparison uses saved application files. Your projects and installed dependencies are excluded."
            empty = "Choose Check for updates. Select a changed file to see its text differences."
        self.comparison_summary.setStyleSheet("font-weight: 600;" + (" color: #ffb5c5;" if attention else ""))
        self.comparison_empty_state.setText(empty)
        if self.comparison_context.toPlainText() != context:
            self.comparison_context.setPlainText(context)

    def _show_file_change(self, current, _previous=None) -> None:
        comparison = self.controller.comparison
        if current is None or comparison is None:
            self.diff.clear()
            return
        index = current.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(index, int) and 0 <= index < len(comparison.changes):
            self.diff.setPlainText(comparison.changes[index].detail)
            self.diff.verticalScrollBar().setValue(0)

    def reject(self) -> None:
        # Hiding the dialog never destroys a running worker. The controller
        # remains alive and emits idle after the worker has actually finished.
        if self.controller.is_active:
            self.controller.cancel()
        super().reject()

    def closeEvent(self, event) -> None:
        self.reject()
        event.accept()
