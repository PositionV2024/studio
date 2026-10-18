from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from .publishing import (
    CommandRunner, PreparedPublish, ProjectScan,
    PublishError, PublishFile, PublishResult, STREAMLIT_DEPLOY_DOCS,
    STREAMLIT_DEPLOY_URL, VERCEL_CONTAINER_DOCS, VERCEL_IMPORT_URL,
    detect_vercel_compatibility, github_entry_url,
    normalize_github_repository, prepare_github_publish, publish_to_github, scan_project,
    deployment_root,
)
from .github_connection import GitHubError, GitHubSession, get_session, token_setup_url
from .github_repositories import GitHubRepository, RepositoryList, list_owned_repositories


class GitHubConnectWorker(QThread):
    connected = Signal(object)
    failed = Signal(str)

    def __init__(self, session: GitHubSession, token: str, remember: bool, parent=None) -> None:
        super().__init__(parent)
        self._session = session
        self._token = token
        self._remember = remember
        self._cancelled = threading.Event()

    def request_cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            account = self._session.connect(
                self._token, remember=self._remember, cancelled=self._cancelled,
            )
            self.connected.emit(account)
        except GitHubError as exc:
            self.failed.emit(str(exc))
        except Exception:
            # Authentication failures must never interpolate a token or a
            # transport exception that could contain request headers.
            self.failed.emit("Could not connect to GitHub. Check your connection and try again.")
        finally:
            self._token = ""


class GitHubConnectDialog(QDialog):
    """Connect directly to GitHub without starting a separate application."""

    def __init__(
        self, session: GitHubSession, repository: str = "", *,
        new_repository: bool = False, workflows: bool = False, parent=None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._worker: GitHubConnectWorker | None = None
        self._connected = False
        self._close_after_work = False
        self._setup_url = token_setup_url(
            repository, new_repository=new_repository, workflows=workflows,
        )
        self.setWindowTitle("Connect to GitHub")
        self.setObjectName("GitHubConnectDialog")
        self.setMinimumWidth(570)
        self.resize(640, 440)
        outer = QVBoxLayout(self)
        title = QLabel("Connect your GitHub account")
        title.setObjectName("InspectorTitle")
        outer.addWidget(title)
        intro = QLabel(
            "Create an access token in your browser, then paste it below. "
            "Studio uses the token to access the repositories you choose."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)
        self.open_github_button = QPushButton("1. Create token on GitHub…")
        self.open_github_button.clicked.connect(self._open_github)
        outer.addWidget(self.open_github_button)
        if new_repository:
            instructions = (
                "On GitHub, choose the account that will own the new repository. "
                "Select All repositories so the token can access the repository after it is created. "
                "Keep Contents and Administration set to Read and write."
            )
        else:
            instructions = (
                "On GitHub, choose the repository owner, select Only select repositories, "
                "and choose the repository you want to update. Keep Contents set to Read and write."
            )
        if workflows:
            instructions += " Your selected files include GitHub Actions workflows; keep Workflows set to Read and write too."
        instructions += " Choose an expiry date, then select Generate token."
        self.instructions_label = QLabel(instructions)
        self.instructions_label.setWordWrap(True)
        outer.addWidget(self.instructions_label)
        form = QFormLayout()
        self.token_input = QLineEdit()
        self.token_input.setObjectName("GitHubAccessToken")
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("Paste the access token from GitHub")
        self.token_input.setMaxLength(4096)
        self.token_input.setClearButtonEnabled(True)
        form.addRow("2. Access token", self.token_input)
        outer.addLayout(form)
        self.remember_checkbox = QCheckBox("Remember on this computer")
        self.remember_checkbox.setChecked(session.storage_available)
        self.remember_checkbox.setEnabled(session.storage_available)
        outer.addWidget(self.remember_checkbox)
        storage_text = (
            "Remembered connections are stored in Windows Credential Manager. "
            "Your token is kept out of project files and upload logs."
            if session.storage_available else
            "This connection lasts until Studio closes. Secure credential storage is available on Windows."
        )
        self.storage_label = QLabel(storage_text)
        self.storage_label.setWordWrap(True)
        outer.addWidget(self.storage_label)
        self.status_label = QLabel("")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setMaximumHeight(7)
        self.progress_bar.hide()
        outer.addWidget(self.progress_bar)
        outer.addStretch()
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.connect_button = QPushButton("Connect GitHub")
        self.connect_button.setObjectName("PrimaryWelcomeButton")
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self._connect)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.connect_button)
        outer.addLayout(buttons)
        self.token_input.textChanged.connect(self._update_buttons)
        self._update_buttons()

    def _update_buttons(self, *_args) -> None:
        busy = self._worker is not None
        self.token_input.setEnabled(not busy)
        self.open_github_button.setEnabled(not busy)
        self.remember_checkbox.setEnabled(not busy and self._session.storage_available)
        self.connect_button.setEnabled(not busy and bool(self.token_input.text().strip()))

    def _open_github(self) -> None:
        if QDesktopServices.openUrl(QUrl(self._setup_url)):
            self.status_label.setStyleSheet("")
            self.status_label.setText("GitHub opened in your browser. Create the token, then return here to connect.")
        else:
            self.status_label.setStyleSheet("color:#ffb5c5")
            self.status_label.setText("Could not open your browser. Open this address manually:\n" + self._setup_url)

    def _connect(self) -> None:
        if self._worker is not None or not self.token_input.text().strip():
            return
        self._connected = False
        self.status_label.setStyleSheet("")
        self.status_label.setText("Checking your GitHub account…")
        worker = GitHubConnectWorker(
            self._session, self.token_input.text().strip(),
            self.remember_checkbox.isChecked(), self,
        )
        self._worker = worker
        self.token_input.clear()
        worker.connected.connect(self._connection_ready)
        worker.failed.connect(self._connection_failed)
        worker.finished.connect(self._worker_finished)
        self.progress_bar.show()
        self._update_buttons()
        worker.start()

    def _connection_ready(self, _account: dict) -> None:
        self._connected = True

    def _connection_failed(self, message: str) -> None:
        self.status_label.setStyleSheet("color:#ffb5c5")
        self.status_label.setText(message)

    def _worker_finished(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self.progress_bar.hide()
        self._update_buttons()
        # Do not destroy a running QThread when the network callback arrives.
        if self._close_after_work:
            super().reject()
        elif self._connected:
            super().accept()

    def reject(self) -> None:
        self.token_input.clear()
        if self._worker is not None:
            self._close_after_work = True
            self._worker.request_cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling the connection…")
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self.reject()
            event.ignore()
            return
        self.token_input.clear()
        super().closeEvent(event)


class PublishWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, work: Callable[[CommandRunner], object], parent=None) -> None:
        super().__init__(parent)
        self._work = work
        self._cancelled = threading.Event()

    def request_cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        runner = CommandRunner(self.progress.emit, self._cancelled)
        try:
            self.completed.emit(self._work(runner))
        except (PublishError, OSError, ValueError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            # Keep a CLI/filesystem failure from tearing down the Qt worker.
            self.failed.emit(f"Publishing could not complete: {exc}")


class RepositoryListWorker(QThread):
    """Read repository choices without locking the local file picker."""

    progress = Signal(str)

    def __init__(self, session, generation, signature, selection, parent=None) -> None:
        super().__init__(parent)
        self._session = session
        self.generation = generation
        self.signature = signature
        self.selection = selection
        self.cancelled = threading.Event()
        self.result: RepositoryList | None = None
        self.error = ""

    def request_cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        try:
            self.result = list_owned_repositories(
                self._session, cancelled=self.cancelled, progress=self.progress.emit,
            )
        except GitHubError as exc:
            self.error = str(exc)
        except Exception:
            self.error = "Could not load your GitHub repositories. Refresh the list to try again."


class PublishDialog(QDialog):
    """An explicit review step precedes any GitHub write.

    Existing-repository mode reads owned repositories from the connected
    account. Only the explicit Publish button writes to GitHub. Browser
    handoffs start only after their named buttons.
    """

    def __init__(
        self, project_path: Path, project_name: str, initial_target: str = "github",
        parent=None, *, auto_scan: bool = True,
    ) -> None:
        super().__init__(parent)
        self.project_path = project_path.expanduser().resolve()
        self.project_name = project_name
        self._github_session = get_session()
        self._worker: PublishWorker | None = None
        self._worker_account_bound = False
        self._repository_worker: RepositoryListWorker | None = None
        self._repository_list: RepositoryList | None = None
        self._repository_signature = None
        self._repository_generation = 0
        self._repository_request_pending = False
        self._repository_pending_selection: tuple[int | None, str] | None = None
        self._repository_origin_candidate = ""
        self._repository_selection_missing = False
        self._destination_initialized = False
        self._review_signature = None
        self._review_repository: GitHubRepository | None = None
        self._review_account_id: int | None = None
        self._plan_signature = None
        self._discard_plan_when_idle = False
        self._observed_connection = self._connection_signature()
        account = self._github_session.account
        self._observed_account_id = account.get("id") if account else None
        self._plan: PreparedPublish | None = None
        self._scan: ProjectScan | None = None
        self._result: PublishResult | None = None
        self._close_after_work = False
        self._setting_files = False
        self.setObjectName("PublishProjectDialog")
        self.setStyleSheet("""
            QPushButton:disabled {
                color: #6d7d96;
                background: #111a2b;
                border: 1px solid #26354c;
            }
            QLineEdit:disabled, QComboBox:disabled { color: #71819b; }
        """)
        self.setWindowTitle("Publish project — " + project_name)
        self.setMinimumSize(780, 620)
        self.resize(920, 790)
        outer = QVBoxLayout(self)
        heading = QLabel("Publish " + project_name)
        heading.setObjectName("InspectorTitle")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(heading)
        path_label = QLabel(str(self.project_path))
        path_label.setTextFormat(Qt.TextFormat.PlainText)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_label.setWordWrap(True)
        outer.addWidget(path_label)
        self.target_tabs = QTabWidget()
        outer.addWidget(self.target_tabs, 1)
        self._build_github_tab()
        self._build_vercel_tab()
        self._build_streamlit_tab()
        self.vercel_entry.setText(self.streamlit_entry.text())
        self.vercel_entry.textChanged.connect(self.streamlit_entry.setText)
        self.streamlit_entry.textChanged.connect(self.vercel_entry.setText)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setMaximumHeight(7)
        self.progress_bar.hide()
        outer.addWidget(self.progress_bar)
        self.status_label = QLabel("Select the files and destination, then review before publishing.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        outer.addWidget(self.status_label)
        footer = QHBoxLayout()
        self.cancel_operation_button = QPushButton("Cancel operation")
        self.cancel_operation_button.clicked.connect(self._cancel_operation)
        self.cancel_operation_button.hide()
        footer.addWidget(self.cancel_operation_button)
        footer.addStretch()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        outer.addLayout(footer)
        self.target_tabs.setCurrentIndex({"github": 0, "vercel": 1, "streamlit": 2}.get(initial_target, 0))
        self._update_compatibility()
        self._update_buttons()
        self._connection_timer = QTimer(self)
        self._connection_timer.setInterval(500)
        self._connection_timer.timeout.connect(self._check_connection_changed)
        self._connection_timer.start()
        if auto_scan:
            QTimer.singleShot(0, self._refresh_files)

    @staticmethod
    def _explanation(text: str) -> QLabel:
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _link(label: str, url: str) -> QLabel:
        link = QLabel(f'<a href="{url}" style="color:#a9c7ff">{label}</a>')
        link.setOpenExternalLinks(True)
        return link

    def _build_github_tab(self) -> None:
        self.github_page = QWidget()
        layout = QVBoxLayout(self.github_page)
        form = QFormLayout()
        self.github_mode = QComboBox()
        self.github_mode.addItems(["Create a new repository", "Update an existing repository"])
        self.github_repository = QLineEdit()
        self.github_repository.setPlaceholderText("my-project or owner/my-project")
        self.github_repository_label = QLabel("GitHub repository")
        self.github_existing_repository_label = QLabel("Your repository")
        self.github_existing_repository_row = QWidget()
        repository_layout = QVBoxLayout(self.github_existing_repository_row)
        repository_layout.setContentsMargins(0, 0, 0, 0)
        repository_actions = QHBoxLayout()
        self.github_existing_repository = QComboBox()
        self.github_existing_repository.setObjectName("GitHubOwnedRepositoryPicker")
        self.github_existing_repository.setEditable(False)
        self.github_existing_repository.setMinimumContentsLength(28)
        self.github_existing_repository.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.github_existing_repository.addItem("Connect GitHub to load repositories", None)
        self.github_refresh_repositories = QPushButton("Refresh list")
        self.github_refresh_repositories.setObjectName("GitHubRefreshRepositoryList")
        self.github_refresh_repositories.clicked.connect(self._refresh_repositories)
        repository_actions.addWidget(self.github_existing_repository, 1)
        repository_actions.addWidget(self.github_refresh_repositories)
        repository_layout.addLayout(repository_actions)
        self.github_repository_status = self._explanation("Connect your GitHub account to list your repositories.")
        repository_layout.addWidget(self.github_repository_status)
        self.github_visibility = QComboBox()
        self.github_visibility.addItems(["Private", "Public"])
        self.github_visibility_label = QLabel("New repository visibility")
        self.github_commit_message = QLineEdit("Update project from HoYo Project Studio")
        self.github_commit_message.setMaxLength(1000)
        form.addRow("Destination", self.github_mode)
        form.addRow(self.github_repository_label, self.github_repository)
        form.addRow(self.github_existing_repository_label, self.github_existing_repository_row)
        form.addRow(self.github_visibility_label, self.github_visibility)
        form.addRow("Commit message", self.github_commit_message)
        layout.addLayout(form)
        self.github_explanation = self._explanation(
            "Choose files to upload. A new repository starts with these files. An existing repository receives a "
            "new update branch for review; files you do not select are kept. Your local Git history and staged files stay intact."
        )
        layout.addWidget(self.github_explanation)
        setup = QHBoxLayout()
        self.github_login_button = QPushButton("Sign in with GitHub…")
        self.github_login_button.clicked.connect(self._login)
        setup.addWidget(self.github_login_button)
        self.github_account_label = QLabel("")
        self.github_account_label.setTextFormat(Qt.TextFormat.PlainText)
        self.github_account_label.setWordWrap(True)
        setup.addWidget(self.github_account_label, 1)
        self.github_disconnect_button = QPushButton("Disconnect")
        self.github_disconnect_button.clicked.connect(self._disconnect_github)
        setup.addWidget(self.github_disconnect_button)
        setup.addStretch()
        layout.addLayout(setup)
        self.github_content_tabs = QTabWidget()
        files_page = QWidget()
        files_layout = QVBoxLayout(files_page)
        file_actions = QHBoxLayout()
        self.refresh_files_button = QPushButton("Refresh files")
        self.refresh_files_button.clicked.connect(self._refresh_files)
        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(lambda: self._select_files(True))
        self.select_none_button = QPushButton("Select none")
        self.select_none_button.clicked.connect(lambda: self._select_files(False))
        for button in (self.refresh_files_button, self.select_all_button, self.select_none_button):
            file_actions.addWidget(button)
        file_actions.addStretch()
        files_layout.addLayout(file_actions)
        self.file_list = QListWidget()
        self.file_list.itemChanged.connect(self._file_selection_changed)
        files_layout.addWidget(self.file_list, 1)
        self.file_summary = self._explanation("Reading project files…")
        files_layout.addWidget(self.file_summary)
        files_layout.addWidget(self._explanation(
            "Local environments, credentials, caches and private Studio settings are excluded. "
            "Published UI settings are included as .studio/published.json. Review selected files for credentials before publishing."
        ))
        self.github_content_tabs.addTab(files_page, "Files")
        review_page = QWidget()
        review_layout = QVBoxLayout(review_page)
        self.review_summary = self._explanation("Prepare a publication to inspect the actual file changes here.")
        self.review_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        review_layout.addWidget(self.review_summary)
        self.review_diff = QPlainTextEdit()
        self.review_diff.setReadOnly(True)
        self.review_diff.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.review_diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        review_layout.addWidget(self.review_diff, 1)
        self.github_content_tabs.addTab(review_page, "Review changes")
        self.excluded_files = QPlainTextEdit()
        self.excluded_files.setReadOnly(True)
        self.github_content_tabs.addTab(self.excluded_files, "Excluded files")
        self.operation_log = QPlainTextEdit()
        self.operation_log.setReadOnly(True)
        self.operation_log.setMaximumBlockCount(4000)
        self.operation_log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.github_content_tabs.addTab(self.operation_log, "Progress")
        layout.addWidget(self.github_content_tabs, 1)
        actions = QHBoxLayout()
        self.review_button = QPushButton("Review publish")
        self.review_button.clicked.connect(self._prepare_publish)
        self.publish_button = QPushButton("Publish to GitHub")
        self.publish_button.setObjectName("PrimaryWelcomeButton")
        self.publish_button.clicked.connect(self._publish)
        self.github_result_button = QPushButton("Open published files")
        self.github_result_button.clicked.connect(self._open_result)
        self.github_result_button.hide()
        actions.addWidget(self.review_button)
        actions.addWidget(self.publish_button)
        actions.addStretch()
        actions.addWidget(self.github_result_button)
        layout.addLayout(actions)
        self.target_tabs.addTab(self.github_page, "GitHub")
        self.github_mode.currentIndexChanged.connect(self._repository_mode_changed)
        self.github_existing_repository.currentIndexChanged.connect(self._repository_selection_changed)
        self.github_repository.textChanged.connect(self._options_changed)
        self.github_visibility.currentIndexChanged.connect(self._options_changed)
        self.github_commit_message.textChanged.connect(self._options_changed)

    def _build_vercel_tab(self) -> None:
        self.vercel_page = QWidget()
        layout = QVBoxLayout(self.vercel_page)
        layout.addWidget(self._explanation(
            "Prepare your app, publish the files to GitHub, then choose your Vercel account, project and deployment "
            "settings in the browser. The final Deploy action happens in Vercel."
        ))
        form = QFormLayout()
        self.vercel_repository = QLineEdit()
        self.vercel_repository.setPlaceholderText("owner/repository")
        self.vercel_branch = QLineEdit("main")
        self.vercel_root = QLineEdit(".")
        self.vercel_root.setPlaceholderText(". or a frontend folder such as frontend")
        self.vercel_entry = QLineEdit()
        self.vercel_entry.setPlaceholderText("app.py")
        self.vercel_entry_label = QLabel("Streamlit entrypoint file")
        form.addRow("GitHub repository", self.vercel_repository)
        form.addRow("Branch to deploy", self.vercel_branch)
        form.addRow("Project root directory", self.vercel_root)
        form.addRow(self.vercel_entry_label, self.vercel_entry)
        layout.addLayout(form)
        self.vercel_compatibility_label = self._explanation("")
        layout.addWidget(self.vercel_compatibility_label)
        self.vercel_compatibility_details = self._explanation("")
        layout.addWidget(self.vercel_compatibility_details)
        self.container_prepare_button = QPushButton("Prepare Streamlit deployment files…")
        self.container_prepare_button.clicked.connect(self._prepare_container)
        layout.addWidget(self.container_prepare_button)
        layout.addWidget(self._link("Vercel Container Runtime documentation", VERCEL_CONTAINER_DOCS))
        layout.addWidget(self._explanation(
            "1. Publish the reviewed app and deployment files using the GitHub tab.\n"
            "2. Open Vercel import and select that repository. Choose the root directory shown above.\n"
            "3. Review the framework/container detection and environment variables, then select Deploy.\n"
            "For an existing repository, Studio publishes updates to a new branch. Merge that branch or select it "
            "when creating the Vercel deployment to use those changes."
        ))
        self.vercel_details = QPlainTextEdit()
        self.vercel_details.setReadOnly(True)
        self.vercel_details.setMaximumHeight(150)
        layout.addWidget(self.vercel_details)
        actions = QHBoxLayout()
        self.vercel_github_button = QPushButton("Publish files to GitHub first")
        self.vercel_github_button.clicked.connect(lambda: self.target_tabs.setCurrentIndex(0))
        self.vercel_copy_button = QPushButton("Copy deployment details")
        self.vercel_copy_button.clicked.connect(self._copy_vercel_details)
        self.vercel_open_button = QPushButton("Open Vercel import")
        self.vercel_open_button.setObjectName("PrimaryWelcomeButton")
        self.vercel_open_button.clicked.connect(self._open_vercel)
        actions.addWidget(self.vercel_github_button)
        actions.addWidget(self.vercel_copy_button)
        actions.addWidget(self.vercel_open_button)
        layout.addLayout(actions)
        layout.addStretch()
        self.target_tabs.addTab(self.vercel_page, "Vercel")
        self.vercel_root.textChanged.connect(self._update_compatibility)
        self.vercel_repository.textChanged.connect(self._update_compatibility)
        self.vercel_branch.textChanged.connect(self._update_compatibility)

    def _build_streamlit_tab(self) -> None:
        self.streamlit_page = QWidget()
        layout = QVBoxLayout(self.streamlit_page)
        layout.addWidget(self._explanation(
            "Deploy the complete Streamlit app from a GitHub repository using Streamlit Community Cloud. "
            "Publish your selected files to GitHub first, then copy the app URL below into its Create app form."
        ))
        form = QFormLayout()
        self.streamlit_repository = QLineEdit()
        self.streamlit_repository.setPlaceholderText("owner/repository")
        self.streamlit_branch = QLineEdit("main")
        entry = "app.py"
        try:
            manifest = json.loads((self.project_path / ".studio" / "project.json").read_text(encoding="utf-8"))
            entry = str(manifest.get("entry_point") or entry)
        except (OSError, ValueError, AttributeError):
            pass
        self.streamlit_entry = QLineEdit(entry)
        form.addRow("GitHub repository", self.streamlit_repository)
        form.addRow("Branch", self.streamlit_branch)
        form.addRow("Entrypoint file", self.streamlit_entry)
        layout.addLayout(form)
        self.streamlit_entry_link = QLineEdit()
        self.streamlit_entry_link.setReadOnly(True)
        layout.addWidget(self.streamlit_entry_link)
        layout.addWidget(self._explanation(
            "On Streamlit Community Cloud choose Create app → Yup, I have an app → Paste GitHub URL. "
            "Add required secrets in Advanced settings and review the Python version before deploying. "
            "The GitHub upload excludes your local secrets.toml and .env files."
        ))
        layout.addWidget(self._link("Streamlit deployment instructions", STREAMLIT_DEPLOY_DOCS))
        actions = QHBoxLayout()
        self.streamlit_github_button = QPushButton("Publish files to GitHub first")
        self.streamlit_github_button.clicked.connect(lambda: self.target_tabs.setCurrentIndex(0))
        self.streamlit_copy_button = QPushButton("Copy app URL")
        self.streamlit_copy_button.clicked.connect(self._copy_streamlit_url)
        self.streamlit_open_button = QPushButton("Open Streamlit Community Cloud")
        self.streamlit_open_button.setObjectName("PrimaryWelcomeButton")
        self.streamlit_open_button.clicked.connect(self._open_streamlit)
        actions.addWidget(self.streamlit_github_button)
        actions.addWidget(self.streamlit_copy_button)
        actions.addWidget(self.streamlit_open_button)
        layout.addLayout(actions)
        layout.addStretch()
        self.target_tabs.addTab(self.streamlit_page, "Streamlit Cloud")
        for field in (self.streamlit_repository, self.streamlit_branch, self.streamlit_entry):
            field.textChanged.connect(self._update_streamlit_url)
        self._update_streamlit_url()

    def _selected_files(self) -> tuple[PublishFile, ...]:
        return tuple(
            item.data(Qt.ItemDataRole.UserRole)
            for index in range(self.file_list.count())
            if (item := self.file_list.item(index)).checkState() == Qt.CheckState.Checked
        )

    def _connection_signature(self):
        try:
            if not self._github_session.has_token:
                return None
            token = self._github_session.get_token()
            if not isinstance(token, str) or not token:
                return None
            # Only a fingerprint is retained. The generation also notices a
            # disconnect/reconnect with the same token between timer ticks.
            generation = getattr(self._github_session, "_generation", None)
            return hashlib.sha256(token.encode("utf-8")).digest(), generation
        except Exception:
            return None

    def _check_connection_changed(self) -> bool:
        signature = self._connection_signature()
        account = self._github_session.account
        account_id = account.get("id") if account else None
        account_changed = (
            account_id is not None and self._observed_account_id is not None
            and account_id != self._observed_account_id
        )
        if signature == self._observed_connection and not account_changed:
            if account_id is not None:
                self._observed_account_id = account_id
            self._update_github_connection()
            return False
        self._observed_connection = signature
        self._observed_account_id = account_id
        self._repository_generation += 1
        self._repository_list = None
        self._repository_signature = None
        self._repository_pending_selection = None
        self._repository_origin_candidate = ""
        self._repository_selection_missing = False
        self._repository_request_pending = (
            signature is not None and self.github_mode.currentIndex() == 1 and not self._close_after_work
        )
        if self._repository_worker is not None:
            self._repository_worker.request_cancel()
        if self._worker is not None and self._worker_account_bound:
            self._worker.request_cancel()
            self._discard_plan_when_idle = True
        else:
            self._discard_plan()
        self._set_repository_placeholder(
            "Loading repositories…" if self._repository_request_pending else "Connect GitHub to load repositories"
        )
        self.github_repository_status.setText(
            "GitHub connection changed. Choose a repository from this account."
            if signature is not None else "Connect your GitHub account to list your repositories."
        )
        self.github_refresh_repositories.setText("Refresh list")
        self.status_label.setStyleSheet("")
        self.status_label.setText("The GitHub connection changed. Review your destination and files again before publishing.")
        self._update_buttons()
        self._start_pending_repository_list()
        return True

    def _set_repository_placeholder(self, text: str) -> None:
        combo = self.github_existing_repository
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(text, None)
            combo.setCurrentIndex(0)
        finally:
            combo.blockSignals(blocked)

    def _existing_repository(self) -> GitHubRepository | None:
        value = self.github_existing_repository.currentData()
        return value if isinstance(value, GitHubRepository) else None

    def _selected_repository_name(self) -> str:
        if self.github_mode.currentIndex() == 0:
            return self.github_repository.text().strip()
        repository = self._existing_repository()
        return repository.full_name if repository is not None else ""

    def _repository_mode_changed(self, *_args) -> None:
        self._options_changed()
        if self.github_mode.currentIndex() == 0:
            self._repository_request_pending = False
            self._repository_pending_selection = None
            if self._repository_worker is not None:
                self._repository_generation += 1
                self._repository_worker.request_cancel()
            self._update_buttons()
            return
        if self._check_connection_changed():
            return
        if self._repository_list is None:
            self._refresh_repositories()

    def _repository_selection_changed(self, *_args) -> None:
        self._repository_origin_candidate = ""
        self._repository_selection_missing = False
        self._options_changed()
        repository = self._existing_repository()
        if repository is not None:
            visibility = "Private" if repository.private else "Public"
            self.github_repository_status.setText(
                f"{visibility} repository · {repository.unavailable_reason}"
                if not repository.can_publish else
                f"{visibility} repository · Review the selected files before uploading."
            )
        elif self._repository_list is not None:
            self._repository_list_status()

    def _repository_list_status(self) -> None:
        if self._repository_list is None:
            return
        count = len(self._repository_list.repositories)
        if not count:
            message = (
                "The previous repository is no longer available for upload. "
                if self._repository_selection_missing else ""
            ) + "No repositories owned by this account are available to this connection. Check token access or create a new repository."
        elif self._repository_selection_missing:
            message = "The previous repository is no longer available for upload. Choose a repository explicitly."
        else:
            message = f"{count} owned repositories visible to this connection. Choose the repository to update."
        self.github_repository_status.setText(message)

    def _refresh_repositories(self) -> None:
        if self.github_mode.currentIndex() != 1 or self._close_after_work:
            return
        if self._check_connection_changed():
            return
        signature = self._connection_signature()
        if signature is None:
            self._set_repository_placeholder("Connect GitHub to load repositories")
            self.github_repository_status.setText("Connect your GitHub account to list your repositories.")
            self._update_buttons()
            return
        repository = self._existing_repository()
        selection = (repository.id, repository.full_name) if repository else (
            (None, self._repository_origin_candidate) if self._repository_origin_candidate else None
        )
        self._repository_origin_candidate = ""
        self._repository_generation += 1
        self._repository_list = None
        self._repository_signature = None
        self._repository_pending_selection = selection
        self._repository_selection_missing = False
        self._repository_request_pending = True
        if self._repository_worker is not None:
            self._repository_worker.request_cancel()
        if self._worker is None:
            self._discard_plan()
        self._set_repository_placeholder("Loading repositories…")
        self.github_repository_status.setText("Loading repositories owned by your connected account…")
        self.github_refresh_repositories.setText("Refresh list")
        self._update_buttons()
        self._start_pending_repository_list()

    def _start_pending_repository_list(self) -> None:
        if (
            not self._repository_request_pending or self._repository_worker is not None
            or self._worker is not None or self._close_after_work
            or self.github_mode.currentIndex() != 1
        ):
            return
        signature = self._connection_signature()
        if signature is None:
            self._repository_request_pending = False
            self._update_buttons()
            return
        self._repository_request_pending = False
        worker = RepositoryListWorker(
            self._github_session, self._repository_generation, signature,
            self._repository_pending_selection, self,
        )
        self._repository_pending_selection = None
        self._repository_worker = worker
        worker.progress.connect(lambda message: self._repository_progress(worker, message))
        worker.finished.connect(self._repository_worker_finished)
        self._update_buttons()
        worker.start()

    def _repository_progress(self, worker: RepositoryListWorker, message: str) -> None:
        if (
            worker is self._repository_worker and worker.generation == self._repository_generation
            and worker.signature == self._connection_signature() and self.github_mode.currentIndex() == 1
            and not self._close_after_work
        ):
            self.github_repository_status.setText(message)

    def _repository_worker_finished(self) -> None:
        worker, self._repository_worker = self._repository_worker, None
        if worker is None:
            return
        self._check_connection_changed()
        current = (
            worker.generation == self._repository_generation
            and worker.signature == self._connection_signature()
            and self.github_mode.currentIndex() == 1 and not self._close_after_work
        )
        if current and not worker.cancelled.is_set():
            if worker.result is not None:
                result = worker.result
                account = self._github_session.account
                if account and result.account_id != account.get("id"):
                    self._set_repository_placeholder("Refresh repositories to try again")
                    self.github_repository_status.setText("The GitHub account changed while loading. Refresh the list to try again.")
                    self.github_refresh_repositories.setText("Retry")
                else:
                    self._repositories_ready(result, worker.signature, worker.selection)
            else:
                self._set_repository_placeholder("Could not load repositories")
                self.github_repository_status.setText(worker.error or "Could not load repositories. Refresh the list to try again.")
                self.github_refresh_repositories.setText("Retry")
        worker.deleteLater()
        self._update_buttons()
        if self._close_after_work:
            self.reject()
        else:
            self._start_pending_repository_list()

    def _repositories_ready(self, result: RepositoryList, signature, selection=None) -> None:
        self._repository_list = result
        self._repository_signature = signature
        self._observed_account_id = result.account_id
        combo = self.github_existing_repository
        blocked = combo.blockSignals(True)
        selected_index = 0
        try:
            combo.clear()
            combo.addItem("Select one of your repositories…" if result.repositories else "No owned repositories available", None)
            for repository in result.repositories:
                label = repository.full_name + ("  ·  Private" if repository.private else "  ·  Public")
                if not repository.can_publish:
                    label += "  ·  " + repository.unavailable_reason
                combo.addItem(label, repository)
                index = combo.count() - 1
                if not repository.can_publish:
                    combo.model().item(index).setEnabled(False)
                combo.setItemData(index, label, Qt.ItemDataRole.ToolTipRole)
                if selection and repository.can_publish:
                    previous_id, previous_name = selection
                    if (
                        repository.full_name.casefold() == previous_name.casefold()
                        and (previous_id is None or repository.id == previous_id)
                    ):
                        selected_index = index
            combo.setCurrentIndex(selected_index)
        finally:
            combo.blockSignals(blocked)
        self._repository_selection_missing = bool(selection and selected_index == 0)
        if selected_index:
            self._repository_selection_changed()
        else:
            self._repository_list_status()
        self.github_refresh_repositories.setText("Refresh list")
        self._update_buttons()

    def _discard_plan(self) -> None:
        if self._plan is not None:
            self._plan.close()
            self._plan = None
        self._plan_signature = None
        self.review_summary.setText("Prepare a publication to inspect the actual file changes here.")
        self.review_diff.clear()

    def _options_changed(self, *_args) -> None:
        if self._worker is None:
            self._discard_plan()
        self._update_buttons()

    def _file_selection_changed(self, _item=None) -> None:
        if self._setting_files:
            return
        self._options_changed()
        selected = self._selected_files()
        total = sum(item.size for item in selected)
        count = self.file_list.count()
        self.file_summary.setText(f"{len(selected)} of {count} files selected · {total / 1_000_000:.2f} MB")

    def _select_files(self, selected: bool) -> None:
        self._setting_files = True
        try:
            for index in range(self.file_list.count()):
                self.file_list.item(index).setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
        finally:
            self._setting_files = False
        self._file_selection_changed()

    def _start_worker(self, work: Callable[[CommandRunner], object], on_result: Callable, *, account_bound=False) -> None:
        if self._worker is not None:
            return
        self.status_label.setStyleSheet("")
        worker = PublishWorker(work, self)
        self._worker = worker
        self._worker_account_bound = account_bound
        worker.progress.connect(self._progress)
        worker.completed.connect(on_result)
        worker.failed.connect(self._failure)
        worker.finished.connect(self._worker_finished)
        self.progress_bar.show()
        self.cancel_operation_button.show()
        self._update_buttons()
        worker.start()

    def _progress(self, message: str) -> None:
        text = message.strip()
        if text:
            self.operation_log.appendPlainText(text)
            self.status_label.setText(text[-600:])

    def _failure(self, message: str) -> None:
        self.status_label.setStyleSheet("color:#ffb5c5")
        self.status_label.setText(message[:900])
        self.operation_log.appendPlainText(message)
        self.target_tabs.setCurrentIndex(0)
        self.github_content_tabs.setCurrentIndex(3)

    def _worker_finished(self) -> None:
        worker, self._worker = self._worker, None
        self._worker_account_bound = False
        if worker is not None:
            worker.deleteLater()
        self._check_connection_changed()
        if self._discard_plan_when_idle:
            self._discard_plan_when_idle = False
            self._discard_plan()
        self._update_buttons()
        if self._close_after_work:
            self.reject()
        else:
            self._start_pending_repository_list()

    def _cancel_operation(self) -> None:
        self._repository_request_pending = False
        self._repository_pending_selection = None
        if self._repository_worker:
            self._repository_generation += 1
            self._repository_worker.request_cancel()
            self._set_repository_placeholder("Refresh repositories to try again")
            self.github_repository_status.setText("Loading repositories was cancelled. Refresh the list to try again.")
        if self._worker:
            self._worker.request_cancel()
            self.status_label.setText("Cancelling the current operation…")
            self.cancel_operation_button.setEnabled(False)
        self._update_buttons()

    def _update_buttons(self) -> None:
        busy = self._worker is not None
        repository_busy = self._repository_worker is not None or self._repository_request_pending
        new_repository = self.github_mode.currentIndex() == 0
        signature = self._connection_signature()
        # Keep the progress log and tabs readable while the worker runs.
        for widget in (self.github_mode, self.github_repository, self.github_commit_message,
                       self.github_login_button, self.github_disconnect_button,
                       self.refresh_files_button, self.select_all_button,
                       self.select_none_button, self.file_list):
            widget.setEnabled(not busy)
        for widget in (self.github_repository, self.github_repository_label,
                       self.github_visibility, self.github_visibility_label):
            widget.setVisible(new_repository)
        self.github_existing_repository_label.setVisible(not new_repository)
        self.github_existing_repository_row.setVisible(not new_repository)
        self.github_visibility.setEnabled(not busy and new_repository)
        self.github_explanation.setText(
            "Choose a repository name and visibility, then select the files to upload. "
            "Review the upload before creating the repository."
            if new_repository else
            "Choose the files to upload to this repository. Studio creates an update branch for review; "
            "files you do not select stay on GitHub."
        )
        self.github_existing_repository.setEnabled(not busy and not repository_busy and signature is not None)
        self.github_refresh_repositories.setEnabled(not busy and not repository_busy and signature is not None)
        selected_repository = self._existing_repository()
        destination_ready = bool(self.github_repository.text().strip()) if new_repository else (
            not repository_busy and self._repository_list is not None
            and self._repository_signature == signature and selected_repository is not None
            and selected_repository.can_publish
        )
        self.review_button.setEnabled(not busy and signature is not None and bool(self._selected_files()) and destination_ready)
        self.publish_button.setEnabled(
            not busy and self._plan is not None and signature is not None
            and self._plan_signature == signature
        )
        if hasattr(self, "container_prepare_button"):
            self.container_prepare_button.setEnabled(not busy)
        if hasattr(self, "cancel_operation_button"):
            active = busy or self._repository_worker is not None
            self.cancel_operation_button.setVisible(active)
            self.cancel_operation_button.setEnabled(active and not self._close_after_work)
            self.progress_bar.setVisible(active)
        self._update_github_connection()

    def _update_github_connection(self) -> None:
        account = self._github_session.account
        has_token = self._github_session.has_token
        if account:
            persistence = "remembered" if self._github_session.remembered else "this session"
            self.github_account_label.setText(f"Connected as {account['login']} · {persistence}")
        elif has_token:
            self.github_account_label.setText("Saved connection · verified when you review or load repositories")
        else:
            self.github_account_label.setText("Connect your GitHub account to publish.")
        self.github_login_button.setText("Change connection…" if has_token else "Sign in with GitHub…")
        self.github_disconnect_button.setVisible(has_token)

    def _refresh_files(self) -> None:
        if self._worker is not None or self._close_after_work:
            return
        self._discard_plan()
        self._start_worker(lambda runner: scan_project(self.project_path, runner), self._files_ready)

    def _files_ready(self, scan: ProjectScan) -> None:
        self._scan = scan
        self._setting_files = True
        try:
            self.file_list.clear()
            for candidate in scan.files:
                item = QListWidgetItem(f"{candidate.path}    ({candidate.size / 1000:.1f} KB)")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if candidate.selected else Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, candidate)
                item.setToolTip(candidate.path)
                self.file_list.addItem(item)
            self.excluded_files.setPlainText("\n".join(f"{path}\n  {reason}" for path, reason in scan.excluded) or "No files were excluded.")
            suggest_destination = not self._destination_initialized and not self.github_repository.text().strip()
            self._destination_initialized = True
            if suggest_destination:
                if scan.github_repository:
                    self._repository_origin_candidate = scan.github_repository
                    self.github_mode.setCurrentIndex(1)
                    if self._repository_list is None and self._repository_worker is None and not self._repository_request_pending:
                        self._refresh_repositories()
                else:
                    import re
                    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.project_path.name).strip(".-") or "my-project"
                    self.github_repository.setText(name[:100])
            if scan.github_repository:
                for field in (self.vercel_repository, self.streamlit_repository):
                    if not field.text().strip():
                        field.setText(scan.github_repository)
        finally:
            self._setting_files = False
        self._file_selection_changed()
        self.status_label.setText("Choose files and a destination, then select Review publish. Nothing has been uploaded.")
        self._update_compatibility()

    def _login(self) -> None:
        if self._worker is not None:
            return
        self._discard_plan()
        dialog = GitHubConnectDialog(
            self._github_session, self._selected_repository_name(),
            new_repository=self.github_mode.currentIndex() == 0,
            workflows=any(item.path.startswith(".github/workflows/") for item in self._selected_files()),
            parent=self,
        )
        result = dialog.exec()
        dialog.deleteLater()
        self._check_connection_changed()
        self._update_buttons()
        if result == QDialog.DialogCode.Accepted and self._github_session.account:
            self.status_label.setStyleSheet("")
            message = f"Connected as {self._github_session.account['login']}. Select Review publish to check your changes."
            if self._github_session.storage_warning:
                message += " " + self._github_session.storage_warning
            self._progress(message)

    def _disconnect_github(self) -> None:
        if self._worker is not None:
            return
        self._discard_plan()
        try:
            self._github_session.disconnect()
        except GitHubError as exc:
            self._failure(str(exc))
        else:
            self.status_label.setStyleSheet("")
            self._progress("GitHub disconnected from Studio. Your repositories are unchanged.")
        self._check_connection_changed()
        if not self._github_session.has_token:
            self.status_label.setText("GitHub disconnected from Studio. Your repositories are unchanged.")
        self._update_buttons()

    def _prepare_publish(self) -> None:
        if self._worker is not None:
            return
        if self._check_connection_changed():
            return
        self._discard_plan()
        selected = self._selected_files()
        signature = self._connection_signature()
        if signature is None:
            self.status_label.setText("Connect your GitHub account before reviewing an upload.")
            self._update_buttons()
            return
        repository = self._selected_repository_name()
        new_repository = self.github_mode.currentIndex() == 0
        existing = self._existing_repository() if not new_repository else None
        if not new_repository and (
            self._repository_worker is not None or self._repository_request_pending
            or self._repository_list is None or self._repository_signature != signature
            or existing is None or not existing.can_publish
        ):
            self.status_label.setText("Choose an available repository from your GitHub list before reviewing the upload.")
            self._update_buttons()
            return
        if not selected or not repository:
            self.status_label.setText("Select files and a repository before reviewing the upload.")
            return
        # Existing visibility comes from the selected remote repository and is
        # verified again by prepare_github_publish. The hidden new preference
        # cannot change an existing repository's visibility.
        visibility = self.github_visibility.currentText().lower() if new_repository else (
            "private" if existing.private else "public"
        )
        self._review_signature = signature
        self._review_repository = existing
        self._review_account_id = self._repository_list.account_id if not new_repository else None
        message = self.github_commit_message.text()
        self.github_content_tabs.setCurrentIndex(3)
        def prepare(runner):
            if signature != self._connection_signature():
                raise PublishError("The GitHub connection changed. Review your destination again.")
            client = self._github_session.client(cancelled=runner.cancelled, log=runner.log)
            return prepare_github_publish(
                self.project_path, selected, repository, new_repository=new_repository,
                visibility=visibility, message=message, runner=runner, client=client,
            )
        self._start_worker(
            prepare, self._publish_prepared, account_bound=True,
        )

    def _publish_prepared(self, plan: PreparedPublish) -> None:
        if self._close_after_work or (self._worker is not None and self._worker._cancelled.is_set()) or (
            self._review_signature is not None and self._review_signature != self._connection_signature()
        ):
            plan.close()
            self.status_label.setText("The GitHub connection changed or review was cancelled. Review again before publishing.")
            return
        expected = self._review_repository
        if expected is not None and (
            plan.new_repository or plan.repository.casefold() != expected.full_name.casefold()
            or plan.repository_id != expected.id or plan.account_id != self._review_account_id
            or plan.owner_id != self._review_account_id or plan.owner_type != "User"
        ):
            plan.close()
            self._repository_list = None
            self._repository_signature = None
            self._set_repository_placeholder("Refresh repositories to try again")
            self.github_repository_status.setText("The repository changed since it was listed. Refresh the list and choose it again.")
            self.status_label.setText("The GitHub destination changed. Refresh your repositories and review again.")
            return
        self._plan = plan
        self._plan_signature = self._connection_signature()
        action = "Create a new" if plan.new_repository else "Add an update branch to the existing"
        self.review_summary.setText(
            f"{action} {plan.visibility} repository:\n{plan.repository_url}\n"
            f"Signed in as: {plan.account}    •    Branch: {plan.branch}\n"
            f"{len(plan.files)} selected files    •    Snapshot: {plan.revision[:12]}\n"
            "Review the snapshot below, then press Publish to GitHub."
        )
        self.review_diff.setPlainText(plan.change_summary + "\n\n" + plan.diff)
        self.github_content_tabs.setCurrentIndex(1)
        self.status_label.setText("Ready to publish the reviewed files. No remote files have been changed yet.")

    def _publish(self) -> None:
        if self._worker is not None or self._plan is None:
            return
        if self._check_connection_changed():
            return
        signature = self._connection_signature()
        if signature is None or signature != self._plan_signature:
            self._discard_plan()
            self.status_label.setText("Connect your GitHub account and review the upload again before publishing.")
            self._update_buttons()
            return
        plan = self._plan
        self.github_content_tabs.setCurrentIndex(3)
        def publish(runner):
            if signature != self._connection_signature():
                raise PublishError("The GitHub connection changed. Review your destination again.")
            client = self._github_session.client(cancelled=runner.cancelled, log=runner.log)
            return publish_to_github(plan, runner, client=client)
        self._start_worker(publish, self._published, account_bound=True)

    def _published(self, result: PublishResult) -> None:
        self._result = result
        self.github_result_button.setText("Open repository" if result.new_repository else "Review update branch")
        self.github_result_button.show()
        self._repository_origin_candidate = result.repository
        self._repository_list = None
        self._repository_signature = None
        self.github_mode.setCurrentIndex(1)
        if not self._repository_request_pending:
            self._refresh_repositories()
        for repository in (self.vercel_repository, self.streamlit_repository):
            repository.setText(result.repository)
        for branch in (self.vercel_branch, self.streamlit_branch):
            branch.setText(result.branch)
        self._discard_plan()
        self.status_label.setText(
            f"Published to {result.repository}, branch {result.branch}. "
            + ("Open the Vercel or Streamlit Cloud tab to deploy it." if result.new_repository else
               "Review the branch on GitHub, or select this branch when deploying.")
        )
        self._update_compatibility()

    def _open_result(self) -> None:
        if self._result is not None:
            self._open_url(self._result.review_url)

    def _update_compatibility(self, *_args) -> None:
        compatibility = detect_vercel_compatibility(self.project_path, self.vercel_root.text())
        self.vercel_compatibility_label.setText(compatibility.label)
        is_project_root = self.vercel_root.text().strip().replace("\\", "/") in {"", ".", "./"}
        details = compatibility.explanation
        if compatibility.label == "Streamlit app" and not is_project_root:
            details += " To generate deployment files, use this project's root (.) or import that subfolder as its own Studio project."
        self.vercel_compatibility_details.setText(details)
        can_prepare = compatibility.label == "Streamlit app" and is_project_root
        self.container_prepare_button.setVisible(can_prepare)
        self.vercel_entry.setVisible(can_prepare)
        self.vercel_entry_label.setVisible(can_prepare)
        valid = False
        try:
            repository = normalize_github_repository(self.vercel_repository.text())
            valid = bool(self.vercel_branch.text().strip())
        except PublishError:
            repository = ""
        self.vercel_open_button.setEnabled(compatibility.supported and valid)
        self.vercel_copy_button.setEnabled(valid)
        self.vercel_details.setPlainText(
            f"Repository: {'https://github.com/' + repository if repository else 'Publish to GitHub first, or enter owner/repository above'}\n"
            f"Branch: {self.vercel_branch.text().strip() or 'main'}\n"
            f"Root directory: {self.vercel_root.text().strip() or '.'}\n"
            f"Detected: {compatibility.label}\n"
            "Import the repository at https://vercel.com/new and review settings before Deploy."
        )

    def _prepare_container(self) -> None:
        try:
            if deployment_root(self.project_path, self.vercel_root.text()) != self.project_path:
                raise ValueError("Use this project's root (.) when preparing Streamlit deployment files.")
            from .vercel_container import prepare_streamlit_container, install_streamlit_container
            plan = prepare_streamlit_container(self.project_path, self.vercel_entry.text().strip())
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            self.status_label.setText(f"Could not prepare deployment files: {exc}")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Review Vercel deployment files")
        dialog.resize(820, 650)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self._explanation(
            "Review the files below before adding them to your project. Vercel Container Runtime is in beta. "
            "The files will be included in the next GitHub file review."
        ))
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        preview.setPlainText("\n\n".join(f"FILE: {name}\n\n{content}" for name, content in plan.files.items()))
        layout.addWidget(preview, 1)
        if plan.notes:
            layout.addWidget(self._explanation("\n".join(plan.notes)))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Add deployment files")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            written = install_streamlit_container(self.project_path, plan)
        except (OSError, ValueError, RuntimeError) as exc:
            self.status_label.setText(f"Could not add deployment files: {exc}")
            return
        self.status_label.setText(f"Added {len(written)} deployment files. Publish them from the GitHub tab next.")
        self._update_compatibility()
        self._refresh_files()

    def _copy_vercel_details(self) -> None:
        QApplication.clipboard().setText(self.vercel_details.toPlainText())
        self.status_label.setText("Deployment details copied.")

    def _open_vercel(self) -> None:
        compatibility = detect_vercel_compatibility(self.project_path, self.vercel_root.text())
        if not compatibility.supported:
            self.status_label.setText(compatibility.explanation)
            return
        try:
            normalize_github_repository(self.vercel_repository.text())
        except PublishError as exc:
            self.status_label.setText(str(exc))
            return
        if self._open_url(VERCEL_IMPORT_URL):
            self.status_label.setText("Vercel import opened. Use the repository, branch and root directory shown above; review settings before Deploy.")

    def _update_streamlit_url(self, *_args) -> None:
        try:
            url = github_entry_url(self.streamlit_repository.text(), self.streamlit_branch.text(), self.streamlit_entry.text())
        except PublishError:
            url = ""
        self.streamlit_entry_link.setText(url)
        self.streamlit_copy_button.setEnabled(bool(url))
        self.streamlit_open_button.setEnabled(bool(url))

    def _copy_streamlit_url(self) -> None:
        if self.streamlit_entry_link.text():
            QApplication.clipboard().setText(self.streamlit_entry_link.text())
            self.status_label.setText("GitHub app URL copied. Paste it into Streamlit Community Cloud's Create app form.")

    def _open_streamlit(self) -> None:
        if not self.streamlit_entry_link.text():
            self.status_label.setText("Enter the GitHub repository, branch and app file first.")
            return
        if self._open_url(STREAMLIT_DEPLOY_URL):
            self.status_label.setText("Streamlit Community Cloud opened. Use Create app and paste the app URL shown above.")

    def _open_url(self, url: str) -> bool:
        if not QDesktopServices.openUrl(QUrl(url)):
            self.status_label.setText("Could not open your browser. Open this address manually: " + url)
            return False
        return True

    def reject(self) -> None:
        self._close_after_work = True
        self._repository_request_pending = False
        if self._worker is not None or self._repository_worker is not None:
            self._cancel_operation()
            return
        self._connection_timer.stop()
        self._discard_plan()
        super().reject()

    def closeEvent(self, event) -> None:
        if self._worker is not None or self._repository_worker is not None:
            self._close_after_work = True
            self._cancel_operation()
            event.ignore()
            return
        self._close_after_work = True
        self._repository_request_pending = False
        self._connection_timer.stop()
        self._discard_plan()
        super().closeEvent(event)
