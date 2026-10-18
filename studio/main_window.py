from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QSize, QSettings, QSignalBlocker, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGroupBox,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QLineEdit,
    QPlainTextEdit,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .code_editor import (
    CodeEditorDocument,
    is_supported_text_file,
    shutdown_python_assist,
)
from .file_operations import project_entry
from .file_removal import editor_path_is_removed, move_project_entry_to_trash
from .import_dialog import ImportProjectDialog
from .inspector import PropertyInspector
from .new_project_dialog import NewProjectDialog
from .preview import PreviewView
from .project_explorer import ProjectExplorer
from .project_library import ProjectLibrary, ProjectRecord
from .project_manifest import ProjectManifest, load_manifest, save_manifest
from .project_runner import ProjectRunner
from .script_runner import ScriptRunner
from .script_test_panel import ScriptTestPanel
from .settings_store import SettingsStore
from .styles import APP_STYLE
from .terminal_console import TerminalConsole
from .welcome import WelcomePage


class MainWindow(QMainWindow):
    def __init__(self, app_root: Path) -> None:
        super().__init__()
        self.app_root = app_root
        self.setWindowTitle(f"HoYo Project Studio {__version__}")
        self.resize(1580, 960)
        self.setStyleSheet(APP_STYLE)
        self.ui_settings = QSettings("Clarence", "HoYo Project Studio")

        app_data = Path(
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation
            )
        )
        self.library = ProjectLibrary(app_data / "project_library.db")
        self.runner = ProjectRunner(self)
        self.current_project: ProjectRecord | None = None
        self.current_settings: SettingsStore | None = None
        self._visual_settings: dict[Path, SettingsStore] = {}
        self._visual_project_names: dict[Path, str] = {}
        self._visual_selection_project: Path | None = None
        self._inline_visual_owner: tuple[Path, str] | None = None
        self._inline_pending = False
        self._visual_commit_in_progress = False
        self._closing_approved = False
        self.open_editors: dict[Path, CodeEditorDocument] = {}
        self._preview_user_closed = False

        self.project_list = QListWidget()
        self.project_list.setObjectName("ProjectLibrary")
        self.project_list.currentItemChanged.connect(self._project_changed)
        self.project_list.itemDoubleClicked.connect(
            lambda _item: self.run_current_project()
        )

        self.project_explorer = ProjectExplorer()
        self.project_explorer.file_opened.connect(self._open_file_in_editor)
        self.project_explorer.delete_requested.connect(self._delete_project_path)
        self.project_explorer.status_message.connect(self.statusBar().showMessage)
        self.project_explorer.tree.selectionModel().selectionChanged.connect(
            lambda *_args: self._update_project_actions()
        )
        self.project_explorer.collapse_requested.connect(
            lambda: self._set_panel_visible("project_explorer", False)
        )

        self.preview = PreviewView(app_root / "resources")
        self.preview.element_selected.connect(self._element_selected)
        self.preview.browser_message.connect(self._append_log)
        self.preview.preview_loaded.connect(self._preview_loaded)
        self.preview.inline_edit_started.connect(self._inline_edit_started)
        self.preview.inline_patch_changed.connect(self._inline_text_changed)
        self.preview.inline_edit_finished.connect(self._inline_edit_finished)
        self.preview.inline_pending_changed.connect(self._inline_pending_changed)

        self.inspector = PropertyInspector()
        self.inspector.patch_changed.connect(self._property_changed)
        self.inspector.save_requested.connect(lambda: self.save_visual_changes())
        self.inspector.pending_edits_changed.connect(
            lambda _pending: self._update_visual_save_state()
        )

        self.logs = QPlainTextEdit()
        self.logs.setObjectName("OutputPanel")
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(5000)
        self.logs.setPlaceholderText("Project output will appear here.")
        self.logs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.logs.customContextMenuRequested.connect(
            self._show_output_context_menu
        )

        self.console = TerminalConsole(self)

        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setObjectName("BottomPanelTabs")
        self.bottom_tabs.setDocumentMode(True)
        self.bottom_tabs.setMovable(False)
        self.bottom_tabs.addTab(self.logs, "Output")
        self.bottom_tabs.addTab(self.console, "Console")
        self._setup_script_testing()

        self.workspace = self._build_workspace()
        self.welcome = WelcomePage()
        self.welcome.create_project_requested.connect(self.create_new_project)
        self.welcome.open_project_requested.connect(self.import_project)
        self.welcome.recent_project_requested.connect(self.open_project_by_id)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.welcome)
        self.pages.addWidget(self.workspace)
        self.setCentralWidget(self.pages)

        self._create_toolbar()
        self._restore_panel_visibility()
        self.statusBar().showMessage("Ready")
        self.runner.log_received.connect(self._append_log)
        self.runner.state_changed.connect(self._runner_state_changed)
        self.runner.preview_ready.connect(self._preview_ready)
        self.runner.failed.connect(self._show_runner_error)
        self.runner.process_started.connect(self._runner_process_started)
        self.runner.process_stopped.connect(self._runner_process_stopped)

        self._reload_library()
        self.show_welcome()
        self._initialize_updates()

    def _initialize_updates(self) -> None:
        from .update_dialog import UpdateController

        self.update_controller = UpdateController(
            self.app_root, self.ui_settings, self,
        )
        self._update_restart_requested = None
        self._waiting_for_update_shutdown = False
        self.update_controller.state_changed.connect(self._update_button_state)
        self.update_controller.restart_requested.connect(self._restart_for_update)
        self.update_controller.idle.connect(self._update_worker_idle)
        self._update_button_state()

    def open_update_dialog(self) -> None:
        controller = getattr(self, "update_controller", None)
        if controller is None:
            self._initialize_updates()
            controller = self.update_controller
        controller.open_dialog()

    def _update_button_state(self, _state: str = "") -> None:
        controller = getattr(self, "update_controller", None)
        action = getattr(self, "update_action", None)
        if controller is not None and action is not None:
            action.setText(controller.button_text)
            action.setToolTip(controller.button_tooltip)

    def _restart_for_update(self, prepared) -> None:
        controller = getattr(self, "update_controller", None)
        if (
            controller is None
            or controller.pending_update is not prepared
            or getattr(self, "_closing_approved", False)
        ):
            return
        self._update_restart_requested = prepared
        self.close()
        # Save/Discard/Cancel belongs to the normal application close path.
        # A cancelled close leaves the downloaded update available for later.
        if (
            not getattr(self, "_closing_approved", False)
            and self._update_restart_requested is prepared
        ):
            self._update_restart_requested = None
            controller.cancel_restart("Restart cancelled. Your update is still ready.")

    def _update_worker_idle(self) -> None:
        if getattr(self, "_waiting_for_update_shutdown", False):
            self._waiting_for_update_shutdown = False
            QTimer.singleShot(0, self.close)

    def _set_closing_controls_enabled(self, enabled: bool) -> None:
        self.centralWidget().setEnabled(enabled)
        self.menuBar().setEnabled(enabled)
        for toolbar in self.findChildren(QToolBar):
            toolbar.setEnabled(enabled)

    def start_background_setup(self) -> None:
        """Finish Preview packages after the source application's window opens."""

        if getattr(sys, "frozen", False) or getattr(self, "_closing_approved", False):
            return
        controller = getattr(self, "background_setup", None)
        if controller is None:
            from .background_setup import BackgroundSetupController, StartupBanner

            self.background_setup = BackgroundSetupController(self.app_root, self)
            controller = self.background_setup
            self.startup_banner = StartupBanner(controller, self)
            content = self.takeCentralWidget()
            container = QWidget(self)
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            layout.addWidget(self.startup_banner)
            if content is not None:
                layout.addWidget(content, 1)
            self.setCentralWidget(container)
            controller.state_changed.connect(self._background_setup_state_changed)
            controller.log_received.connect(lambda message: self._append_log(f"[Setup] {message}\n"))
            controller.finished.connect(self._background_setup_finished)
        controller.start()

    def _setup_runtime_blocked(
        self, executable: str | Path | None = None, *, project_path: Path | None = None,
    ) -> bool:
        controller = getattr(self, "background_setup", None)
        if controller is None or not controller.blocks_runtime:
            return False
        if not executable:
            return True
        path = Path(executable).expanduser()
        if not path.is_absolute():
            base = project_path or self.app_root
            candidate = base / path
            command = shutil.which(str(path)) if path.name == str(path) and not candidate.is_file() else None
            path = Path(command) if command else candidate
        # Keep the virtual-environment path: resolving a POSIX python symlink
        # could falsely identify an independent project environment as Studio's.
        path = Path(os.path.abspath(path))
        roots = (Path(os.path.abspath(self.app_root / ".venv")), Path(os.path.abspath(sys.prefix)))
        return any(path.is_relative_to(root) for root in roots)

    def _selected_setup_blocked(self) -> bool:
        if getattr(self, "background_setup", None) is None or self.current_project is None:
            return False
        manifest = load_manifest(self.current_project.path)
        if manifest is None:
            return False
        try:
            executable = self.runner._discover_python(self.current_project.path, manifest)
        except RuntimeError:
            return False
        return self._setup_runtime_blocked(executable, project_path=self.current_project.path)

    def _background_setup_state_changed(self, state: str) -> None:
        self._update_project_actions()
        messages = {
            "checking": "Preparing Preview support. You can continue editing files.",
            "running": "Preparing Preview support. You can continue editing files.",
            "cancelling": "Stopping setup safely...",
            "ready": "Preview support is ready.",
            "failed": "Preview setup could not finish. Choose Retry in the setup banner.",
            "cancelled": "Preview setup was stopped. Your files are still available.",
        }
        self.statusBar().showMessage(messages.get(state, "Ready"), 8000)

    def _background_setup_finished(self, _success: bool) -> None:
        if getattr(self, "_waiting_for_setup_shutdown", False):
            QTimer.singleShot(0, self.close)

    def _build_workspace(self) -> QWidget:
        # Every major workspace area is separated by a visible QSplitter handle.
        # Users can drag these handles to resize the Project Explorer, preview,
        # output console, and property inspector without restarting the app.
        self.document_tabs = QTabWidget()
        self.document_tabs.setObjectName("WorkspaceDocumentTabs")
        self.document_tabs.setDocumentMode(True)
        self.document_tabs.setMovable(True)
        self.document_tabs.setTabsClosable(True)
        self.document_tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.document_tabs.tabCloseRequested.connect(
            self._close_workspace_tab
        )
        self.document_tabs.currentChanged.connect(
            self._document_tab_changed
        )
        # Keep the reusable browser owned by the window, but only show its tab
        # while a preview is starting or running.
        self.preview.setParent(self.document_tabs)
        self.preview.hide()

        self.center_splitter = QSplitter(Qt.Orientation.Vertical)
        self.center_splitter.setObjectName("CenterWorkspaceSplitter")
        self.center_splitter.setChildrenCollapsible(True)
        self.center_splitter.setHandleWidth(8)
        self.center_splitter.addWidget(self.document_tabs)
        self.center_splitter.addWidget(self.bottom_tabs)
        self.center_splitter.setSizes([720, 210])
        self.center_splitter.setStretchFactor(0, 5)
        self.center_splitter.setStretchFactor(1, 1)

        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("WorkspaceSplitter")
        self.workspace_splitter.setChildrenCollapsible(True)
        self.workspace_splitter.setHandleWidth(8)

        self.project_sidebar = self._project_panel()
        self.project_sidebar.setMinimumWidth(220)
        self.inspector.setMinimumWidth(270)

        self.workspace_splitter.addWidget(self.project_sidebar)
        self.workspace_splitter.addWidget(self.center_splitter)
        self.workspace_splitter.addWidget(self.inspector)
        self.workspace_splitter.setSizes([300, 980, 320])
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setStretchFactor(2, 0)

        self._restore_workspace_layout()
        return self.workspace_splitter

    def _preview_tab_index(self) -> int:
        return self.document_tabs.indexOf(self.preview)

    def _ensure_preview_tab(
        self,
        *,
        select: bool = True,
        title: str | None = None,
    ) -> int:
        """Open the reusable preview tab without starting or stopping a process."""

        index = self._preview_tab_index()
        if index < 0:
            index = self.document_tabs.insertTab(
                0,
                self.preview,
                title or "Preview",
            )
            self.document_tabs.setTabToolTip(
                index,
                "Live Streamlit preview. Closing this tab does not stop the project.",
            )
        elif title:
            self.document_tabs.setTabText(index, title)
        self._preview_user_closed = False
        if select:
            self.document_tabs.setCurrentIndex(index)
        self._update_project_actions()
        return index

    def _close_preview_tab(self, *, user_initiated: bool = False) -> None:
        """Detach the stopped/hidden page and invalidate its pending callbacks."""

        if not self._commit_visual_fields() and user_initiated:
            self._show_inline_commit_error()
            return
        self.preview.stop_preview()
        index = self._preview_tab_index()
        if index >= 0:
            self.document_tabs.removeTab(index)
        if user_initiated:
            self._preview_user_closed = True
        if hasattr(self, "edit_action") and self.edit_action.isChecked():
            with QSignalBlocker(self.edit_action):
                self.edit_action.setChecked(False)
            self.preview.set_edit_mode(False)
        self._clear_visual_selection(force=True)
        self._update_project_actions()

    def _close_workspace_tab(self, index: int) -> None:
        widget = self.document_tabs.widget(index)
        if widget is None:
            return

        if widget is self.preview:
            self._close_preview_tab(user_initiated=True)
            running = self.runner.running_project_name
            message = "Preview tab closed"
            if running:
                message += f" — {running} is still running"
            self.statusBar().showMessage(message)
            self._update_project_actions()
            return

        if isinstance(widget, CodeEditorDocument):
            if not widget.can_close():
                return
            widget.shutdown()
            self.open_editors.pop(widget.path, None)
            self.document_tabs.removeTab(index)
            widget.deleteLater()
            self.statusBar().showMessage(f"Closed {widget.path.name}")
            self._update_project_actions()

    def _document_tab_changed(self, _index: int) -> None:
        self._update_script_test_actions()
        if not hasattr(self, "run_action"):
            return
        if (
            self.document_tabs.currentWidget() is not self.preview
            and hasattr(self, "edit_action")
            and self.edit_action.isChecked()
        ):
            self.edit_action.setChecked(False)
        self._update_project_actions()

    def _editor_language_overrides(self) -> dict[str, str]:
        settings = getattr(self, "ui_settings", None)
        if settings is None:
            return {}
        try:
            value = json.loads(settings.value("code_editor/language_overrides", "{}"))
        except (TypeError, ValueError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {path: mode for path, mode in value.items()
                if isinstance(path, str) and isinstance(mode, str)
                and mode in {"python", "plain"}}

    def _remember_editor_language(self, path: Path, mode: str) -> None:
        settings = getattr(self, "ui_settings", None)
        if settings is None:
            return
        overrides = self._editor_language_overrides()
        key = str(path)
        if mode in {"python", "plain"}:
            overrides[key] = mode
        else:
            overrides.pop(key, None)
        settings.setValue("code_editor/language_overrides", json.dumps(overrides))

    def _open_file_in_editor(self, file_path: Path | str) -> None:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            return

        if not is_supported_text_file(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            self.statusBar().showMessage(
                f"Opened {path.name} with the system application"
            )
            return

        existing = self.open_editors.get(path)
        if existing is not None:
            index = self.document_tabs.indexOf(existing)
            if index >= 0:
                self.document_tabs.setCurrentIndex(index)
                existing.editor.setFocus()
                return
            self.open_editors.pop(path, None)

        # Tabs keep their original project context when the sidebar changes.
        project_path = path.parent
        if self.current_project is not None:
            candidate = self.current_project.path.resolve()
            if path.is_relative_to(candidate):
                project_path = candidate
        manifest = load_manifest(project_path) or ProjectManifest(project_path.name)
        try:
            python_executable = str(self.runner._discover_python(project_path, manifest))
        except RuntimeError:
            python_executable = ""
        editor = CodeEditorDocument(
            path,
            self,
            project_path=project_path,
            python_executable=python_executable,
            script_python_executable=manifest.python_executable or python_executable,
        )
        editor.language_changed.connect(
            lambda mode, target=path: self._remember_editor_language(target, mode)
        )
        editor.language_changed.connect(lambda _mode: self._update_script_test_actions())
        editor.test_script_requested.connect(self.test_current_script)
        script_runner = getattr(self, "script_runner", None)
        editor.set_script_test_busy(bool(script_runner and script_runner.is_running))
        editor.set_language_mode(self._editor_language_overrides().get(str(path), "auto"))
        editor.title_changed.connect(
            lambda title, document=editor: self._update_editor_tab_title(
                document,
                title,
            )
        )
        editor.status_message.connect(self.statusBar().showMessage)
        editor.saved.connect(lambda _path: self.project_explorer.refresh())

        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        index = self.document_tabs.addTab(editor, icon, editor.display_title)
        self.document_tabs.setTabToolTip(index, str(path))
        self.document_tabs.setCurrentIndex(index)
        self.open_editors[path] = editor
        editor.editor.setFocus()
        self.show_workspace()
        self._update_project_actions()

    def _delete_project_path(self, file_path: Path | str) -> None:
        """Recycle one Explorer entry after handling any affected open buffers."""

        root = self.project_explorer.project_path
        if root is None:
            return
        try:
            target = project_entry(root, Path(file_path))
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "Unable to delete item", str(error))
            return

        affected = [
            editor for editor in list(self.open_editors.values())
            if editor_path_is_removed(editor.path, target)
        ]
        description = str(target.relative_to(root))
        is_link = target.is_symlink() or getattr(target, "is_junction", lambda: False)()
        if is_link:
            detail = "The link will be moved. Its target stays in place."
        else:
            detail = "The folder and its contents will be moved." if target.is_dir() else ""
        if affected:
            detail += "\nAffected editor tabs will close after the move succeeds."
        if any(editor.is_modified for editor in affected):
            detail += "\nYou will be asked about unsaved changes before continuing."

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle("Delete from project?")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f"Move {description} to the Recycle Bin / Trash?")
        dialog.setInformativeText(detail.strip())
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        if dialog.exec() != QMessageBox.StandardButton.Yes:
            return

        # Do not discard a buffer or close a tab until the filesystem move works.
        for editor in affected:
            if not editor.can_close():
                return
        try:
            move_project_entry_to_trash(root, target)
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "Unable to delete item", str(error))
            return

        for editor in affected:
            editor.shutdown()
            self.open_editors.pop(editor.path, None)
            index = self.document_tabs.indexOf(editor)
            if index >= 0:
                self.document_tabs.removeTab(index)
            editor.deleteLater()
        self.project_explorer.refresh()
        self.project_explorer.select_path(target.parent)
        self.statusBar().showMessage(f"Moved {description} to the Recycle Bin / Trash")
        self._update_project_actions()

    def _update_editor_tab_title(
        self,
        editor: CodeEditorDocument,
        title: str,
    ) -> None:
        index = self.document_tabs.indexOf(editor)
        if index >= 0:
            self.document_tabs.setTabText(index, title)
            self.document_tabs.setTabToolTip(index, str(editor.path))

    def _current_editor(self) -> CodeEditorDocument | None:
        widget = self.document_tabs.currentWidget()
        return widget if isinstance(widget, CodeEditorDocument) else None

    def _save_current_document(self) -> None:
        editor = self._current_editor()
        if editor is not None:
            editor.save()
        elif self.document_tabs.currentWidget() is self.preview:
            self.save_visual_changes()

    def _save_all_documents(self) -> None:
        for editor in list(self.open_editors.values()):
            if editor.is_modified and not editor.save():
                return
        self.statusBar().showMessage("All open files saved")

    def _setup_script_testing(self) -> None:
        """Create a separate process and output panel for individual scripts."""

        if getattr(self, "script_runner", None) is None:
            self.script_runner = ScriptRunner(self)
        if getattr(self, "script_test_panel", None) is None:
            self.script_test_panel = ScriptTestPanel(self.script_runner, self)
            self.script_runner.state_changed.connect(self._script_test_state_changed)
            self.script_runner.failed.connect(
                lambda message: self.statusBar().showMessage(f"Script test: {message}")
            )
            self.script_runner.finished.connect(self._script_test_finished)
        bottom_tabs = getattr(self, "bottom_tabs", None)
        if bottom_tabs is not None and bottom_tabs.indexOf(self.script_test_panel) < 0:
            bottom_tabs.addTab(self.script_test_panel, "Script Test")
        self._update_script_test_actions()

    def _ensure_script_test_panel(self) -> ScriptTestPanel:
        self._setup_script_testing()
        return self.script_test_panel

    def _show_script_test_panel(self) -> None:
        panel = self._ensure_script_test_panel()
        bottom_tabs = getattr(self, "bottom_tabs", None)
        if bottom_tabs is not None:
            bottom_tabs.setCurrentWidget(panel)
            self._set_panel_visible("output", True, persist=False)
        self.show_workspace()
        splitter = getattr(self, "center_splitter", None)
        if splitter is not None:
            sizes = splitter.sizes()
            if len(sizes) > 1 and sizes[1] < 250:
                total = sum(sizes)
                output_height = min(280, max(180, total // 3))
                splitter.setSizes([max(120, total - output_height), output_height])

    def test_current_script(self, editor: CodeEditorDocument | None = None) -> bool:
        """Save and run exactly the selected document in its original project."""

        editor = editor if isinstance(editor, CodeEditorDocument) else self._current_editor()
        if editor is None:
            self.statusBar().showMessage("Open a Python script in an editor tab, then choose Test Script.")
            return False
        if self._setup_runtime_blocked(editor.script_python_executable, project_path=editor.project_path):
            self.statusBar().showMessage("This Python environment is still being prepared. You can continue editing files.")
            return False
        panel = self._ensure_script_test_panel()
        if self.script_runner.is_running:
            self._show_script_test_panel()
            self.statusBar().showMessage("A script test is already running. Use Stop before starting another.")
            return False
        if not editor.can_test_script:
            self.statusBar().showMessage(
                "Choose Python in the editor's Language selector to test this file."
                if not editor.editor.is_python else "This editor is not ready to run a script."
            )
            return False

        # Save only this buffer. Other tabs may contain unfinished work, and the
        # selected sidebar project may differ from the one this tab belongs to.
        if editor.is_modified and not editor.save():
            self.statusBar().showMessage("Script test cancelled because the file could not be saved.")
            return False
        editor.editor.hide_completions()
        panel.prepare_run(editor.path, editor.project_path)
        self._show_script_test_panel()
        accepted = self.script_runner.start(
            editor.path, editor.project_path, editor.script_python_executable,
        )
        self._update_script_test_actions()
        return accepted

    def stop_script_test(self) -> None:
        runner = getattr(self, "script_runner", None)
        if runner is None or not runner.is_running:
            return
        self._show_script_test_panel()
        runner.stop()

    def _script_test_state_changed(self, state: str) -> None:
        self._update_script_test_actions()
        if state in {"starting", "running", "stopping"}:
            path = self.script_runner.script_path
            name = path.name if path is not None else "script"
            messages = {"starting": "Starting", "running": "Testing", "stopping": "Stopping"}
            self.statusBar().showMessage(f"{messages[state]} {name} — Script Test")

    def _script_test_finished(self, path: str, exit_code: int, stopped: bool) -> None:
        self._update_script_test_actions()
        succeeded = exit_code == 0 and self.script_runner.state != "failed"
        result = "stopped" if stopped else "finished successfully" if succeeded else "failed"
        self.statusBar().showMessage(
            f"{Path(path).name} {result} (exit code {exit_code}). See Script Test for output."
        )

    def _update_script_test_actions(self) -> None:
        runner = getattr(self, "script_runner", None)
        running = bool(runner and runner.is_running)
        for document in list(getattr(self, "open_editors", {}).values()):
            document.set_script_test_busy(running)
            blocked = self._setup_runtime_blocked(document.script_python_executable, project_path=document.project_path)
            document.set_runtime_preparing(
                "This Python environment is being prepared. You can continue editing files."
                if blocked else ""
            )
        tabs = getattr(self, "document_tabs", None)
        current = self._current_editor() if tabs is not None else None
        action = getattr(self, "test_script_action", None)
        if action is not None:
            action.setEnabled(current is not None and current.can_test_script and not running)
        stop_action = getattr(self, "stop_script_test_action", None)
        if stop_action is not None:
            stop_action.setEnabled(running)

    def _close_current_tab(self) -> None:
        index = self.document_tabs.currentIndex()
        if index >= 0:
            self._close_workspace_tab(index)

    def _can_close_all_editors(self) -> bool:
        for editor in list(self.open_editors.values()):
            if not editor.can_close():
                return False
        return True

    def open_running_preview(self) -> None:
        """Reopen the preview tab without restarting Streamlit."""

        url = self.runner.preview_url
        if not url or not self.runner.is_preview_running or self.runner.is_stopping:
            QMessageBox.information(
                self,
                "No running preview",
                "Run a Streamlit project first.",
            )
            return

        project_name = self.runner.running_project_name or "Project"
        self._ensure_preview_tab(
            select=True,
            title=f"Preview — {project_name}",
        )
        if not self.preview.preview_matches(url):
            self.preview.load_preview(url)

        running_path = self.runner.running_project_path
        if running_path is not None:
            try:
                settings = self._settings_for_project(running_path)
                self.preview.set_draft_settings(settings.load_draft())
            except OSError:
                pass
        self.statusBar().showMessage(
            f"Opened running preview: {project_name}"
        )

    def _refresh_running_preview(self) -> None:
        if not self._commit_visual_fields():
            self._show_inline_commit_error()
            return
        if self._preview_tab_index() < 0:
            self.open_running_preview()
            return
        if self.runner.preview_url and not self.runner.is_stopping:
            self.preview.refresh_preview()
            self.document_tabs.setCurrentWidget(self.preview)

    def restart_running_project(self) -> None:
        """An explicit restart opens the new run's preview when it starts."""

        if not self.runner.is_preview_running or self.runner.is_stopping:
            return
        self._preview_user_closed = False
        self.runner.restart_project()

    def _project_panel(self) -> QWidget:
        self.projects_box = QGroupBox("Project Library")
        projects_box = self.projects_box
        projects_layout = QVBoxLayout(projects_box)
        projects_layout.setContentsMargins(7, 10, 7, 7)
        projects_layout.addWidget(self.project_list)

        self.explorer_box = QGroupBox("Project Explorer")
        explorer_box = self.explorer_box
        explorer_layout = QVBoxLayout(explorer_box)
        explorer_layout.setContentsMargins(7, 10, 7, 7)
        explorer_layout.addWidget(self.project_explorer)

        self.project_panel_splitter = QSplitter(Qt.Orientation.Vertical)
        self.project_panel_splitter.setObjectName("ProjectPanelSplitter")
        self.project_panel_splitter.setChildrenCollapsible(True)
        self.project_panel_splitter.setHandleWidth(8)
        self.project_panel_splitter.addWidget(projects_box)
        self.project_panel_splitter.addWidget(explorer_box)
        self.project_panel_splitter.setSizes([240, 560])
        self.project_panel_splitter.setStretchFactor(0, 0)
        self.project_panel_splitter.setStretchFactor(1, 1)

        panel = QWidget()
        panel.setObjectName("ProjectSidebar")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.addWidget(self.project_panel_splitter)
        return panel

    def _restore_workspace_layout(self) -> None:
        """Restore the user's last splitter sizes when available."""

        workspace_state = self.ui_settings.value("workspace/splitter")
        center_state = self.ui_settings.value("workspace/center_splitter")
        project_state = self.ui_settings.value("workspace/project_splitter")

        if workspace_state:
            self.workspace_splitter.restoreState(workspace_state)
        if center_state:
            self.center_splitter.restoreState(center_state)
        if project_state:
            self.project_panel_splitter.restoreState(project_state)

    def _save_workspace_layout(self) -> None:
        """Persist all adjustable pane sizes between application launches."""

        if hasattr(self, "workspace_splitter"):
            self.ui_settings.setValue(
                "workspace/splitter",
                self.workspace_splitter.saveState(),
            )
        if hasattr(self, "center_splitter"):
            self.ui_settings.setValue(
                "workspace/center_splitter",
                self.center_splitter.saveState(),
            )
        if hasattr(self, "project_panel_splitter"):
            self.ui_settings.setValue(
                "workspace/project_splitter",
                self.project_panel_splitter.saveState(),
            )

        for panel_name, widget in (
            ("project_library", getattr(self, "projects_box", None)),
            ("project_explorer", getattr(self, "explorer_box", None)),
            ("output", getattr(self, "bottom_tabs", None)),
            ("inspector", getattr(self, "inspector", None)),
        ):
            if widget is not None:
                self.ui_settings.setValue(
                    f"workspace/panels/{panel_name}",
                    not widget.isHidden(),
                )

    def _restore_panel_visibility(self) -> None:
        """Restore which auxiliary panels were visible on the last run."""

        for panel_name in (
            "project_library",
            "project_explorer",
            "output",
            "inspector",
        ):
            visible = self.ui_settings.value(
                f"workspace/panels/{panel_name}",
                True,
                type=bool,
            )
            self._set_panel_visible(panel_name, visible, persist=False)

    def _set_panel_visible(
        self,
        panel_name: str,
        visible: bool,
        *,
        persist: bool = True,
    ) -> None:
        """Show or collapse a panel while keeping it recoverable from Panels."""

        panels = {
            "project_library": getattr(self, "projects_box", None),
            "project_explorer": getattr(self, "explorer_box", None),
            "output": getattr(self, "bottom_tabs", None),
            "inspector": getattr(self, "inspector", None),
        }
        widget = panels.get(panel_name)
        if widget is None:
            return

        widget.setVisible(bool(visible))

        # The two left-side sections share one outer sidebar. Hide that outer
        # container only when both sections are collapsed. The Panels menu
        # remains in the application chrome, so either section can be restored.
        if panel_name in {"project_library", "project_explorer"}:
            left_visible = (
                not self.projects_box.isHidden()
                or not self.explorer_box.isHidden()
            )
            self.project_sidebar.setVisible(left_visible)

        action = getattr(self, "panel_actions", {}).get(panel_name)
        if action is not None and action.isChecked() != bool(visible):
            with QSignalBlocker(action):
                action.setChecked(bool(visible))

        if persist:
            self.ui_settings.setValue(
                f"workspace/panels/{panel_name}",
                bool(visible),
            )

        labels = {
            "project_library": "Project Library",
            "project_explorer": "Project Explorer",
            "output": "Output / Console",
            "inspector": "Properties",
        }
        label = labels.get(panel_name, panel_name.replace("_", " ").title())
        state = "shown" if visible else "collapsed"
        self.statusBar().showMessage(
            f"{label} {state}. Use the Panels menu to restore hidden panels."
        )

    def _show_all_panels(self) -> None:
        for panel_name in (
            "project_library",
            "project_explorer",
            "output",
            "inspector",
        ):
            self._set_panel_visible(panel_name, True)

    def _hide_auxiliary_panels(self) -> None:
        """Give the preview maximum room without losing panel state."""

        for panel_name in (
            "project_library",
            "project_explorer",
            "output",
            "inspector",
        ):
            self._set_panel_visible(panel_name, False)

    def _reset_workspace_layout(self) -> None:
        """Return the resizable workspace panes to practical defaults."""

        self._show_all_panels()
        self.workspace_splitter.setSizes([300, 980, 320])
        self.center_splitter.setSizes([720, 210])
        self.project_panel_splitter.setSizes([240, 560])
        self._save_workspace_layout()
        self.statusBar().showMessage("Workspace layout reset")

    def _create_toolbar(self) -> None:
        """Create a compact, task-oriented toolbar.

        Secondary actions live inside Project, Run, Edit, and View menus so
        the top of the application stays calm and easy to scan.
        """

        toolbar = QToolBar("Workspace")
        toolbar.setObjectName("MainToolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.addToolBar(toolbar)
        self.main_toolbar = toolbar

        standard_icon = self.style().standardIcon

        # -------------------------------------------------------------
        # HOME
        # -------------------------------------------------------------
        self.home_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_DirHomeIcon),
            "Home",
            self,
        )
        self.home_action.setToolTip("Return to the welcome page")
        self.home_action.setShortcut(QKeySequence("Ctrl+Shift+H"))
        self.home_action.triggered.connect(self.show_welcome)
        toolbar.addAction(self.home_action)
        home_button = toolbar.widgetForAction(self.home_action)
        if isinstance(home_button, QToolButton):
            home_button.setObjectName("HomeToolbarButton")

        toolbar.addSeparator()

        # -------------------------------------------------------------
        # PROJECT MENU
        # -------------------------------------------------------------
        self.new_project_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_FileDialogNewFolder),
            "New Project…",
            self,
        )
        self.new_project_action.setShortcut(QKeySequence.StandardKey.New)
        self.new_project_action.setToolTip("Create a new Streamlit project")
        self.new_project_action.triggered.connect(self.create_new_project)

        self.open_project_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_DialogOpenButton),
            "Open Existing Project…",
            self,
        )
        self.open_project_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_project_action.setToolTip(
            "Import an existing Streamlit project folder"
        )
        self.open_project_action.triggered.connect(self.import_project)

        self.open_folder_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_DirOpenIcon),
            "Open Project Folder",
            self,
        )
        self.open_folder_action.setToolTip(
            "Show the selected project in File Explorer"
        )
        self.open_folder_action.triggered.connect(self.open_current_folder)

        self.remove_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_TrashIcon),
            "Remove from Library",
            self,
        )
        self.remove_action.setToolTip(
            "Remove the project from Project Studio without deleting its files"
        )
        self.remove_action.triggered.connect(self.remove_current_project)

        self.new_file_action = QAction("New File…", self)
        self.new_file_action.setShortcut(QKeySequence("Ctrl+Alt+N"))
        self.new_file_action.triggered.connect(self.project_explorer.new_file)
        self.new_folder_action = QAction("New Folder…", self)
        self.new_folder_action.triggered.connect(self.project_explorer.new_folder)
        self.add_files_action = QAction("Add Files…", self)
        self.add_files_action.triggered.connect(self.project_explorer.add_files)
        self.delete_file_action = QAction("Delete Selected File / Folder…", self)
        self.delete_file_action.triggered.connect(self.project_explorer.delete_selected)

        self.save_file_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_DialogSaveButton),
            "Save Current File",
            self,
        )
        self.save_file_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_file_action.triggered.connect(self._save_current_document)

        self.save_all_action = QAction("Save All Files", self)
        self.save_all_action.setShortcut(QKeySequence("Ctrl+Alt+S"))
        self.save_all_action.triggered.connect(self._save_all_documents)

        self.close_tab_action = QAction("Close Current Tab", self)
        self.close_tab_action.setShortcut(QKeySequence.StandardKey.Close)
        self.close_tab_action.triggered.connect(self._close_current_tab)

        self.test_script_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_MediaPlay), "Test Current Script", self,
        )
        self.test_script_action.setShortcut(QKeySequence("F7"))
        self.test_script_action.setToolTip("Save changes and run the Python script in the selected editor tab")
        self.test_script_action.triggered.connect(lambda _checked=False: self.test_current_script())
        self.stop_script_test_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_MediaStop), "Stop Script Test", self,
        )
        self.stop_script_test_action.setShortcut(QKeySequence("Shift+F7"))
        self.stop_script_test_action.setToolTip("Stop the script test, including when its editor tab is closed")
        self.stop_script_test_action.triggered.connect(self.stop_script_test)
        # These shortcuts also work while the Project menu is closed.
        self.addAction(self.test_script_action)
        self.addAction(self.stop_script_test_action)

        project_menu = QMenu("Project", self)
        project_menu.addSection("Create or open")
        project_menu.addAction(self.new_project_action)
        project_menu.addAction(self.open_project_action)
        project_menu.addSeparator()
        project_menu.addSection("Current project")
        project_menu.addAction(self.open_folder_action)
        project_menu.addAction(self.remove_action)
        project_menu.addSeparator()
        project_menu.addSection("Project files")
        project_menu.addAction(self.new_file_action)
        project_menu.addAction(self.new_folder_action)
        project_menu.addAction(self.add_files_action)
        project_menu.addAction(self.delete_file_action)
        project_menu.addSeparator()
        project_menu.addSection("Editor")
        project_menu.addAction(self.test_script_action)
        project_menu.addAction(self.stop_script_test_action)
        project_menu.addAction(self.save_file_action)
        project_menu.addAction(self.save_all_action)
        project_menu.addAction(self.close_tab_action)

        project_button = QToolButton(self)
        project_button.setObjectName("ToolbarMenuButton")
        project_button.setText("Project")
        project_button.setIcon(
            standard_icon(QStyle.StandardPixmap.SP_DirIcon)
        )
        project_button.setToolTip("Create, open, or manage projects")
        project_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        project_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        project_button.setMenu(project_menu)
        toolbar.addWidget(project_button)
        self.project_button = project_button

        # -------------------------------------------------------------
        # RUN / PREVIEW MENU
        # -------------------------------------------------------------
        self.run_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_MediaPlay),
            "Run",
            self,
        )
        self.run_action.setShortcut(QKeySequence("F5"))
        self.run_action.setToolTip("Run the selected Streamlit project")
        self.run_action.triggered.connect(self.run_current_project)

        self.stop_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_MediaStop),
            "Stop",
            self,
        )
        self.stop_action.setShortcut(QKeySequence("Shift+F5"))
        self.stop_action.setToolTip("Stop the running project")
        self.stop_action.triggered.connect(
            lambda _checked=False: self.runner.stop_project(reason="user")
        )
        self.stop_action.setEnabled(False)

        self.restart_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_BrowserReload),
            "Restart Project",
            self,
        )
        self.restart_action.setShortcut(QKeySequence("Ctrl+F5"))
        self.restart_action.setToolTip("Stop and start the project again")
        self.restart_action.triggered.connect(
            lambda _checked=False: self.restart_running_project()
        )

        self.refresh_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_BrowserReload),
            "Refresh Preview",
            self,
        )
        self.refresh_action.setShortcut(QKeySequence("F6"))
        self.refresh_action.setToolTip("Reload the embedded website preview")
        self.refresh_action.triggered.connect(self._refresh_running_preview)

        self.open_preview_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_ComputerIcon),
            "Open Running Preview",
            self,
        )
        self.open_preview_action.setShortcut(QKeySequence("Ctrl+Alt+P"))
        self.open_preview_action.setToolTip(
            "Open or restore the preview tab without restarting the project"
        )
        self.open_preview_action.triggered.connect(self.open_running_preview)

        self.install_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_ArrowDown),
            "Install Dependencies…",
            self,
        )
        self.install_action.setToolTip(
            "Install the selected project's requirements.txt"
        )
        self.install_action.triggered.connect(
            self.install_current_dependencies
        )

        run_menu = QMenu("Run", self)
        run_menu.addSection("Preview")
        run_menu.addAction(self.run_action)
        run_menu.addAction(self.stop_action)
        run_menu.addAction(self.restart_action)
        run_menu.addAction(self.refresh_action)
        run_menu.addAction(self.open_preview_action)
        run_menu.addSeparator()
        run_menu.addSection("Environment")
        run_menu.addAction(self.install_action)

        self.run_button = QToolButton(self)
        self.run_button.setObjectName("RunToolbarButton")
        self.run_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.run_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup
        )
        self.run_button.setDefaultAction(self.run_action)
        self.run_button.setMenu(run_menu)
        self.run_button.setToolTip(
            "Run or stop the selected project. Use the arrow for more options."
        )
        toolbar.addWidget(self.run_button)

        toolbar.addSeparator()

        # -------------------------------------------------------------
        # VISUAL EDITING
        # -------------------------------------------------------------
        self.edit_action = QAction("Edit Mode", self)
        self.edit_action.setCheckable(True)
        self.edit_action.setShortcut(QKeySequence("Ctrl+E"))
        self.edit_action.setToolTip(
            "Click visible website elements and edit their properties"
        )
        self.edit_action.toggled.connect(self._toggle_edit_mode)

        self.undo_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_ArrowBack),
            "Undo",
            self,
        )
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.undo)

        self.redo_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_ArrowForward),
            "Redo",
            self,
        )
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self.redo)

        edit_menu = QMenu("Edit", self)
        edit_menu.addAction(self.edit_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)

        self.edit_button = QToolButton(self)
        self.edit_button.setObjectName("EditModeToolbarButton")
        self.edit_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.edit_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup
        )
        self.edit_button.setDefaultAction(self.edit_action)
        self.edit_button.setMenu(edit_menu)
        toolbar.addWidget(self.edit_button)

        self.publish_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_DialogApplyButton),
            "Publish",
            self,
        )
        self.publish_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.publish_action.setToolTip(
            "Publish visual settings to the selected project; open the menu for GitHub and hosting"
        )
        self.publish_action.triggered.connect(self.publish)
        toolbar.addAction(self.publish_action)
        publish_button = toolbar.widgetForAction(self.publish_action)
        if isinstance(publish_button, QToolButton):
            publish_button.setObjectName("PublishToolbarButton")
            publish_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        publish_menu = QMenu("Publish", self)
        local_publish = publish_menu.addAction("Publish UI Settings")
        local_publish.triggered.connect(self.publish)
        publish_menu.addSeparator()
        self.publish_github_action = publish_menu.addAction("Publish to GitHub…")
        self.publish_vercel_action = publish_menu.addAction("Publish to Vercel…")
        self.publish_streamlit_action = publish_menu.addAction("Publish to Streamlit Community Cloud…")
        self.publish_github_action.triggered.connect(lambda: self.open_publish_dialog("github"))
        self.publish_vercel_action.triggered.connect(lambda: self.open_publish_dialog("vercel"))
        self.publish_streamlit_action.triggered.connect(lambda: self.open_publish_dialog("streamlit"))
        self.publish_menu = publish_menu
        if isinstance(publish_button, QToolButton):
            publish_button.setMenu(publish_menu)

        toolbar.addSeparator()

        # -------------------------------------------------------------
        # VIEW / PANELS MENU
        # -------------------------------------------------------------
        self.panel_actions: dict[str, QAction] = {}
        view_menu = QMenu("View", self)
        view_menu.addSection("Panels")
        for panel_name, label in (
            ("project_library", "Project Library"),
            ("project_explorer", "Project Explorer"),
            ("output", "Output / Console"),
            ("inspector", "Properties"),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(
                lambda checked, name=panel_name: self._set_panel_visible(
                    name,
                    checked,
                )
            )
            view_menu.addAction(action)
            self.panel_actions[panel_name] = action

        view_menu.addSeparator()
        show_all_action = view_menu.addAction("Show All Panels")
        show_all_action.triggered.connect(self._show_all_panels)

        focus_preview_action = view_menu.addAction("Focus Preview")
        focus_preview_action.setToolTip(
            "Hide auxiliary panels and give the website preview maximum room"
        )
        focus_preview_action.triggered.connect(
            self._hide_auxiliary_panels
        )

        view_menu.addSeparator()
        reset_layout_action = view_menu.addAction("Reset Workspace Layout")
        reset_layout_action.setToolTip(
            "Restore the default panel visibility and sizes"
        )
        reset_layout_action.triggered.connect(
            self._reset_workspace_layout
        )

        view_button = QToolButton(self)
        view_button.setObjectName("ToolbarMenuButton")
        view_button.setText("View")
        view_button.setIcon(
            standard_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
        )
        view_button.setToolTip("Show, hide, and arrange workspace panels")
        view_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        view_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        view_button.setMenu(view_menu)
        toolbar.addWidget(view_button)
        self.view_button = view_button

        # Keep the application update control at the right edge, independent
        # of whether a project is selected or the preview is running.
        update_spacer = QWidget(toolbar)
        update_spacer.setObjectName("UpdateToolbarSpacer")
        update_spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        toolbar.addWidget(update_spacer)
        self.update_action = QAction(
            standard_icon(QStyle.StandardPixmap.SP_BrowserReload), "Update", self,
        )
        self.update_action.setToolTip("Check for Studio updates")
        self.update_action.triggered.connect(self.open_update_dialog)
        toolbar.addAction(self.update_action)
        update_button = toolbar.widgetForAction(self.update_action)
        if isinstance(update_button, QToolButton):
            update_button.setObjectName("UpdateToolbarButton")
        self.update_button = update_button

        self._update_project_actions()

    def _update_project_actions(self) -> None:
        """Keep actions aligned with the selected and running projects."""

        self._update_script_test_actions()
        self._update_visual_save_state()
        if not hasattr(self, "run_action"):
            return

        has_project = self.current_project is not None
        setup_blocked = self._selected_setup_blocked()
        any_process_running = self.runner.is_running
        selected_is_running = bool(
            self.current_project
            and self.runner.is_project_running(self.current_project.path)
        )

        preview_open = self._preview_tab_index() >= 0
        preview_selected = self.document_tabs.currentWidget() is self.preview
        current_editor = self._current_editor()

        self.remove_action.setEnabled(has_project)
        self.open_folder_action.setEnabled(has_project)
        self.new_file_action.setEnabled(has_project)
        self.new_folder_action.setEnabled(has_project)
        self.add_files_action.setEnabled(has_project)
        self.delete_file_action.setEnabled(
            has_project and self.project_explorer.selected_path is not None
        )
        self.refresh_action.setEnabled(
            self.runner.is_preview_running
            and not self.runner.is_stopping
            and bool(self.runner.preview_url)
            and preview_open
        )
        self.open_preview_action.setEnabled(
            self.runner.is_preview_running
            and not self.runner.is_stopping
            and bool(self.runner.preview_url)
        )
        self.edit_action.setEnabled(
            selected_is_running
            and not self.runner.is_stopping
            and preview_open
            and preview_selected
        )
        self.undo_action.setEnabled(has_project or current_editor is not None)
        self.redo_action.setEnabled(has_project or current_editor is not None)
        self.publish_action.setEnabled(has_project)
        for action_name in ("publish_github_action", "publish_vercel_action", "publish_streamlit_action"):
            action = getattr(self, action_name, None)
            if action is not None:
                action.setEnabled(has_project)
        self.save_file_action.setText(
            "Save UI Changes" if preview_selected else "Save Current File"
        )
        self.save_file_action.setEnabled(
            current_editor is not None
            or (preview_selected and self.current_settings is not None)
        )
        self.save_all_action.setEnabled(bool(self.open_editors))
        self.close_tab_action.setEnabled(self.document_tabs.count() > 0)

        # If another project is running, Run performs a smooth switch instead
        # of making the user stop it manually first.
        can_switch_or_run = (
            not any_process_running or self.runner.is_preview_running
        )
        self.run_action.setEnabled(
            has_project and not selected_is_running and can_switch_or_run and not setup_blocked
        )
        self.run_action.setText(
            "Switch and Run"
            if has_project and any_process_running and not selected_is_running
            else "Run"
        )
        self.run_action.setToolTip(
            "Preview support is being prepared. You can continue editing files."
            if setup_blocked else "Stop the current preview and run the selected project"
            if any_process_running and not selected_is_running
            else "Run the selected Streamlit project"
        )

        self.stop_action.setEnabled(any_process_running)
        self.restart_action.setEnabled(selected_is_running and not self.runner.is_stopping and not setup_blocked)
        self.install_action.setEnabled(has_project and not any_process_running and not setup_blocked)

        desired_default = (
            self.stop_action
            if selected_is_running
            or (any_process_running and not self.runner.is_preview_running)
            else self.run_action
        )
        if self.run_button.defaultAction() is not desired_default:
            self.run_button.setDefaultAction(desired_default)

        self.run_button.setProperty(
            "running",
            "true"
            if selected_is_running
            or (any_process_running and not self.runner.is_preview_running)
            else "false",
        )
        self.run_button.style().unpolish(self.run_button)
        self.run_button.style().polish(self.run_button)
        self.run_button.update()

    def show_welcome(self) -> None:
        self.welcome.set_projects(self.library.list_projects())
        self.pages.setCurrentWidget(self.welcome)
        self.statusBar().showMessage(
            "Welcome — create a new project or open an existing project"
        )

    def show_workspace(self) -> None:
        self.pages.setCurrentWidget(self.workspace)

    def _reload_library(self, select_id: str | None = None) -> None:
        projects = self.library.list_projects()
        selected_item: QListWidgetItem | None = None

        with QSignalBlocker(self.project_list):
            self.project_list.clear()
            for project in projects:
                item = QListWidgetItem(project.name)
                item.setData(Qt.ItemDataRole.UserRole, project.id)
                item.setToolTip(str(project.path))
                self.project_list.addItem(item)
                if select_id and project.id == select_id:
                    selected_item = item

        self.welcome.set_projects(projects)

        if selected_item is not None:
            self.project_list.setCurrentItem(selected_item)
        elif self.project_list.count() == 0:
            self._clear_visual_selection()
            self.current_project = None
            self.current_settings = None
            self.project_explorer.clear_project()
            self._update_project_actions()

    def open_project_by_id(self, project_id: str) -> None:
        for row in range(self.project_list.count()):
            item = self.project_list.item(row)
            if str(item.data(Qt.ItemDataRole.UserRole)) == project_id:
                if self.project_list.currentItem() is item:
                    self._project_changed(item, None)
                else:
                    self.project_list.setCurrentItem(item)
                return

    def import_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose Streamlit project folder",
        )
        if not selected:
            return
        project_path = Path(selected)
        existing = load_manifest(project_path)
        dialog = ImportProjectDialog(project_path, existing, self)
        if not dialog.exec():
            return
        manifest = dialog.manifest()
        save_manifest(project_path, manifest)
        SettingsStore(project_path)
        record = self.library.add_or_update(
            name=manifest.name,
            path=project_path,
            entry_point=manifest.entry_point,
            preview_port=manifest.preview_port,
        )
        self._reload_library(record.id)

    def create_new_project(self) -> None:
        dialog = NewProjectDialog(self)
        if not dialog.exec():
            return

        source = self.app_root / "templates" / "streamlit_demo"
        target = dialog.target_path
        manifest = dialog.manifest()

        try:
            shutil.copytree(source, target)
            if manifest.entry_point != "app.py":
                source_entry = target / "app.py"
                target_entry = target / manifest.entry_point
                target_entry.parent.mkdir(parents=True, exist_ok=True)
                source_entry.replace(target_entry)
            save_manifest(target, manifest)
            self._personalize_template(target, manifest.name, manifest.entry_point)
            SettingsStore(target)
        except OSError as error:
            shutil.rmtree(target, ignore_errors=True)
            QMessageBox.critical(
                self,
                "Project creation failed",
                str(error),
            )
            return

        record = self.library.add_or_update(
            name=manifest.name,
            path=target,
            entry_point=manifest.entry_point,
            preview_port=manifest.preview_port,
        )
        self._reload_library(record.id)

    @staticmethod
    def _personalize_template(
        project_path: Path,
        project_name: str,
        entry_point: str = "app.py",
    ) -> None:
        app_file = project_path / entry_point
        if app_file.exists():
            text = app_file.read_text(encoding="utf-8")
            text = text.replace("Project Studio Demo", project_name)
            text = text.replace("Visual Editor Demo", project_name)
            app_file.write_text(text, encoding="utf-8")

        for name in ("defaults.json", "draft.json", "published.json"):
            path = project_path / ".studio" / name
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            title = payload.setdefault("hero.title", {})
            if isinstance(title, dict):
                title["text"] = project_name
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    def remove_current_project(self) -> None:
        if not self.current_project:
            return
        answer = QMessageBox.question(
            self,
            "Remove project",
            "Remove this project from the library? The project folder will not be deleted.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._clear_visual_selection():
            self._show_inline_commit_error()
            return
        if self.runner.is_project_running(self.current_project.path):
            self.runner.stop_project(reason="user")
        self.library.remove(self.current_project.id)
        self.current_project = None
        self.current_settings = None
        self.project_explorer.clear_project()
        self._reload_library()
        self.show_welcome()

    def _project_changed(
        self,
        current: QListWidgetItem | None,
        _previous,
    ) -> None:
        if current is None:
            return
        project_id = str(current.data(Qt.ItemDataRole.UserRole))
        project = self.library.get(project_id)
        if project is None:
            return

        # Selecting a library item must remain instantaneous. The previous
        # version synchronously stopped Streamlit here, blocking Qt for several
        # seconds and producing a false "Process crashed" dialog on Windows.
        # Selection now only changes the workspace. The active preview remains
        # alive until the user explicitly presses Run/Switch or Stop.
        if not self._clear_visual_selection():
            self._show_inline_commit_error()
            project_list = getattr(self, "project_list", None)
            if project_list is not None:
                with QSignalBlocker(project_list):
                    project_list.setCurrentItem(_previous)
            return
        self.current_project = project
        self.current_settings = self._settings_for_project(project.path)
        self._visual_project_names[project.path.resolve()] = project.name
        self.library.touch(project.id)
        self.project_explorer.set_project(project.path)
        self.console.set_working_directory(project.path)
        self.show_workspace()

        if (
            hasattr(self, "edit_action")
            and self.edit_action.isChecked()
            and not self.runner.is_project_running(project.path)
        ):
            self.edit_action.setChecked(False)

        if self.runner.is_project_running(project.path):
            preview_index = self._preview_tab_index()
            if preview_index >= 0 and self.runner.preview_url:
                self.document_tabs.setTabText(
                    preview_index,
                    f"Preview — {project.name}",
                )
                if (
                    not self.runner.is_stopping
                    and not self.preview.preview_matches(self.runner.preview_url)
                ):
                    self.preview.load_preview(self.runner.preview_url)
                self.preview.set_draft_settings(
                    self.current_settings.load_draft()
                )
            message = f"Selected running project: {project.name}"
            if preview_index < 0:
                message += " — use Run → Open Running Preview to restore the tab"
        elif self.runner.is_preview_running:
            # Keep the active project's preview intact while browsing another
            # project's files. Selecting a library item must not replace the
            # running page or force a browser reload.
            message = (
                f"Selected: {project.name} — "
                f"{self.runner.running_project_name} is still running"
            )
        else:
            preview_index = self._preview_tab_index()
            if preview_index >= 0:
                self.document_tabs.setTabText(
                    preview_index,
                    f"Preview — {project.name}",
                )
                self.preview.show_project_placeholder(project.name)
            message = f"Selected: {project.name}"

        self._update_project_actions()
        self.statusBar().showMessage(message)

    def run_current_project(self) -> None:
        if not self.current_project:
            QMessageBox.information(
                self,
                "No project",
                "Create, open, or select a project first.",
            )
            return
        if self._selected_setup_blocked():
            self.statusBar().showMessage("Preview support is being prepared. You can continue editing files.")
            return
        manifest = load_manifest(self.current_project.path)
        if manifest is None:
            QMessageBox.warning(
                self,
                "Missing manifest",
                "The project is missing .studio/project.json. Re-import the project.",
            )
            return
        self.show_workspace()
        self.bottom_tabs.setCurrentWidget(self.logs)
        self._ensure_preview_tab(
            select=True,
            title=f"Preview — {self.current_project.name}",
        )
        self.preview.show_project_placeholder(
            self.current_project.name,
            starting=True,
        )
        self.runner.start_project(self.current_project.path, manifest)

    def install_current_dependencies(self) -> None:
        if not self.current_project:
            QMessageBox.information(
                self,
                "No project",
                "Create, open, or select a project first.",
            )
            return
        if self._selected_setup_blocked():
            self.statusBar().showMessage("Setup is already preparing this Python environment. You can continue editing files.")
            return
        manifest = load_manifest(self.current_project.path)
        if manifest is None:
            QMessageBox.warning(
                self,
                "Missing manifest",
                "Re-import the project first.",
            )
            return
        self.bottom_tabs.setCurrentWidget(self.logs)
        self.runner.install_requirements(
            self.current_project.path,
            manifest,
        )

    def _preview_ready(self, project_path: str, url: str) -> None:
        # Readiness belongs to the running process, even when another project's
        # files are selected. Ignore stale signals from a stopped/replaced run.
        running_path = self.runner.running_project_path
        if (
            running_path is None
            or self.runner.is_stopping
            or Path(project_path).resolve() != running_path.resolve()
            or not self.runner.preview_url
            or str(url).rstrip("/") != self.runner.preview_url.rstrip("/")
        ):
            return

        preview_index = self._preview_tab_index()
        project_name = self.runner.running_project_name or running_path.name
        if preview_index >= 0 and not self._preview_user_closed:
            self.document_tabs.setTabText(
                preview_index,
                f"Preview — {project_name}",
            )
            if not self.preview.preview_matches(url):
                self.preview.load_preview(url)
            try:
                self.preview.set_draft_settings(
                    self._settings_for_project(running_path).load_draft()
                )
            except OSError:
                pass
            self.statusBar().showMessage(f"Loading preview: {url}")
        else:
            self._append_log(
                f"Streamlit server is ready; its Preview tab is closed: {url}\n"
            )
            self.statusBar().showMessage(
                "Project is running. Use Run → Open Running Preview."
            )
        self._update_project_actions()

    def _preview_loaded(self, url: str) -> None:
        if (
            self._preview_tab_index() < 0
            or not self.runner.is_preview_running
            or self.runner.is_stopping
            or not self.runner.preview_url
            or str(url).rstrip("/") != self.runner.preview_url.rstrip("/")
            or not self.preview.preview_matches(url)
        ):
            return
        self._append_log(f"Preview loaded: {url}\n")
        self.statusBar().showMessage(f"Preview loaded: {url}")

    def _runner_state_changed(self, state: str) -> None:
        if state in {"stopping", "restarting", "switching"}:
            self._clear_visual_selection(force=True)
            self.preview.stop_preview()
        elif state == "stopped" and not self.runner.is_preview_running:
            # FailedToStart has no finished/process_stopped signal, so the
            # terminal state also closes any tab opened before process.start().
            self._close_preview_tab()
        self._update_project_actions()
        self.statusBar().showMessage(f"Project state: {state}")

    def _runner_process_started(
        self,
        pid: int,
        project_name: str,
        project_path: str,
    ) -> None:
        if (
            self.runner.is_stopping
            or not self.runner.is_project_running(project_path)
        ):
            return
        self.console.append_system_message(
            f"Streamlit started: {project_name} (PID {pid})"
        )
        self.console.set_working_directory(Path(project_path))
        self.console.show_process(pid)
        if not self._preview_user_closed:
            # A queued restart/switch begins after the old process has closed
            # its tab. Manual closure while waiting still takes precedence.
            existing_index = self._preview_tab_index()
            self._ensure_preview_tab(
                select=existing_index < 0,
                title=f"Preview — {project_name}",
            )
            self.preview.show_project_placeholder(project_name, starting=True)

    def _runner_process_stopped(
        self,
        project_name: str,
        exit_code: int,
        intentional: bool,
    ) -> None:
        reason = "stopped by Project Studio" if intentional else "finished"
        self.console.append_system_message(
            f"Streamlit {reason}: {project_name} (exit code {exit_code})"
        )
        if not self.runner.is_preview_running:
            self._close_preview_tab()

    def _show_runner_error(self, message: str) -> None:
        if not self.runner.is_preview_running and not self.runner.has_pending_start:
            self._close_preview_tab()
        self._append_log(f"\nERROR: {message}\n")
        QMessageBox.critical(self, "Project failed", message)

    def _append_log(self, text: str) -> None:
        self.logs.moveCursor(QTextCursor.MoveOperation.End)
        self.logs.insertPlainText(str(text))
        self.logs.ensureCursorVisible()

    def _show_output_context_menu(self, position) -> None:
        menu = self.logs.createStandardContextMenu(position)
        menu.addSeparator()
        clear_action = menu.addAction("Clear Output")
        clear_action.triggered.connect(self.logs.clear)
        menu.addSeparator()
        hide_action = menu.addAction("Collapse Output / Console Panel")
        hide_action.triggered.connect(
            lambda: self._set_panel_visible("output", False)
        )
        menu.exec(self.logs.viewport().mapToGlobal(position))

    def _settings_for_project(self, project_path: Path) -> SettingsStore:
        """Keep each project's working draft alive across workspace changes."""

        path = Path(project_path).expanduser().resolve()
        if not hasattr(self, "_visual_settings"):
            self._visual_settings = {}
            self._visual_project_names = {}
        if path not in self._visual_settings:
            current = getattr(self, "current_settings", None)
            if current is not None and current.project_path.resolve() == path:
                self._visual_settings[path] = current
            else:
                self._visual_settings[path] = SettingsStore(path)
        self._visual_project_names.setdefault(path, path.name)
        return self._visual_settings[path]

    def _commit_property_fields(self) -> None:
        inspector = getattr(self, "inspector", None)
        commit = getattr(inspector, "commit_pending_edits", None)
        if callable(commit):
            commit()

    def _commit_visual_fields(self) -> bool:
        """Flush browser text before saving or changing its project owner."""

        if getattr(self, "_visual_commit_in_progress", False):
            return False
        self._visual_commit_in_progress = True
        try:
            preview = getattr(self, "preview", None)
            commit = getattr(preview, "commit_inline_edit", None)
            if callable(commit) and not commit():
                return False
            self._commit_property_fields()
            return True
        finally:
            self._visual_commit_in_progress = False

    def _show_inline_commit_error(self) -> None:
        QMessageBox.warning(
            self,
            "Text edit is still open",
            "Studio could not finish reading the text edit from the Preview. "
            "Your changes are still open. Finish typing in the Preview, then try again.",
        )

    def _clear_visual_selection(self, *, force: bool = False) -> bool:
        if not self._commit_visual_fields() and not force:
            return False
        owner = getattr(self, "_inline_visual_owner", None)
        if owner is not None:
            # An exited/crashed server must still close its tab. Preserve every
            # acknowledged character as one working-draft edit in that case.
            self._settings_for_project(owner[0]).finish_text_edit()
        self._inline_visual_owner = None
        self._inline_pending = False
        inspector = getattr(self, "inspector", None)
        if inspector is not None:
            inspector.clear_selection()
        self._visual_selection_project = None
        self._update_visual_save_state()
        return True

    def _update_visual_save_state(self) -> None:
        inspector = getattr(self, "inspector", None)
        update = getattr(inspector, "set_save_state", None)
        if not callable(update):
            return
        settings = getattr(self, "current_settings", None)
        project = getattr(self, "current_project", None)
        owner = getattr(self, "_inline_visual_owner", None)
        pending_inline = bool(
            getattr(self, "_inline_pending", False)
            and owner is not None
            and project is not None
            and owner[0] == project.path.resolve()
        )
        update(
            bool(settings is not None and (settings.is_dirty or pending_inline)),
            project_name=getattr(project, "name", ""),
            enabled=settings is not None,
        )

    def save_visual_changes(self, *, project_path: Path | None = None) -> bool:
        """Commit the active field before saving its project's working draft."""

        if not self._commit_visual_fields():
            self._show_inline_commit_error()
            return False
        if project_path is None:
            if self.current_project is None or self.current_settings is None:
                return False
            project_path = self.current_project.path
        settings = self._settings_for_project(project_path)
        try:
            settings.save_draft()
        except (OSError, ValueError) as exc:
            self._update_visual_save_state()
            QMessageBox.critical(
                self,
                "Could not save UI changes",
                "Your UI changes are still open and have not been discarded.\n\n"
                f"{exc}",
            )
            return False
        self._update_visual_save_state()
        path = Path(project_path).resolve()
        name = self._visual_project_names.get(path, path.name)
        self.statusBar().showMessage(f"UI changes saved: {name}")
        return True

    def _confirm_visual_exit(self) -> bool:
        if not self._commit_visual_fields():
            self._show_inline_commit_error()
            return False
        current = getattr(self, "current_settings", None)
        if current is not None:
            self._settings_for_project(current.project_path)
        stores = getattr(self, "_visual_settings", {})
        names = getattr(self, "_visual_project_names", {})
        dirty_paths = sorted(
            (path for path, settings in stores.items() if settings.is_dirty),
            key=lambda path: (names.get(path, path.name).casefold(), str(path)),
        )
        if not dirty_paths:
            return True

        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Unsaved UI changes")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText("Save your UI changes before leaving?")
        project_names = "\n".join(names.get(path, path.name) for path in dirty_paths)
        dialog.setInformativeText(
            f"Unsaved changes in:\n{project_names}\n\n"
            "Save keeps these changes. Discard leaves the last saved version. "
            "Cancel keeps Studio open."
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Save)
        dialog.setEscapeButton(QMessageBox.StandardButton.Cancel)
        if len(dirty_paths) > 1:
            dialog.button(QMessageBox.StandardButton.Save).setText("Save All")
        answer = dialog.exec()
        if answer == QMessageBox.StandardButton.Save:
            return all(
                self.save_visual_changes(project_path=path) for path in dirty_paths
            )
        return answer == QMessageBox.StandardButton.Discard

    def _toggle_edit_mode(self, enabled: bool) -> None:
        if not self._clear_visual_selection():
            with QSignalBlocker(self.edit_action):
                self.edit_action.setChecked(not enabled)
            self._show_inline_commit_error()
            return
        self.preview.set_edit_mode(enabled)
        if enabled:
            self.statusBar().showMessage(
                "Edit Mode is on — click text to type, or select an element to change its Properties"
            )
        else:
            self.statusBar().showMessage("Edit Mode is off")

    def _element_selected(self, data: dict) -> None:
        running_path = self.runner.running_project_path
        if (
            getattr(self, "_closing_approved", False)
            or self.current_project is None
            or running_path is None
            or self.runner.is_stopping
            or self.current_project.path.resolve() != running_path.resolve()
            or not self.edit_action.isChecked()
            or self.document_tabs.currentWidget() is not self.preview
        ):
            return
        # JavaScript has already opened the new inline editor by the time this
        # queued selection reaches Qt. Only flush the previous Properties fields.
        self._commit_property_fields()
        self._visual_selection_project = running_path.resolve()
        self.inspector.set_selection(data)
        self.statusBar().showMessage(
            f"Selected element: {data.get('label') or data.get('id', '')}"
        )

    def _inline_edit_started(self, element_id: str) -> None:
        running_path = self.runner.running_project_path
        inspector = getattr(self, "inspector", None)
        if (
            getattr(self, "_closing_approved", False)
            or not isinstance(element_id, str)
            or not element_id
            or self.current_project is None
            or running_path is None
            or self.runner.is_stopping
            or self.current_project.path.resolve() != running_path.resolve()
            or getattr(self, "_visual_selection_project", None) != running_path.resolve()
            or not self.edit_action.isChecked()
            or self.document_tabs.currentWidget() is not self.preview
            or inspector is None
            or inspector.element_id != element_id
            or "text" not in getattr(inspector, "_allowed_fields", set())
        ):
            return
        owner = (running_path.resolve(), element_id)
        previous = getattr(self, "_inline_visual_owner", None)
        if previous is not None and previous != owner:
            self._settings_for_project(previous[0]).finish_text_edit()
        self._inline_visual_owner = owner
        original = getattr(self.preview, "inline_original_text", None)
        self._settings_for_project(owner[0]).begin_text_edit(
            element_id, original_text=original if isinstance(original, str) else None
        )
        self._update_visual_save_state()

    def _inline_text_changed(self, element_id: str, patch: dict) -> None:
        owner = getattr(self, "_inline_visual_owner", None)
        if (
            getattr(self, "_closing_approved", False)
            or owner is None
            or owner[1] != element_id
            or not isinstance(patch, dict)
            or set(patch) != {"text"}
            or not isinstance(patch["text"], str)
        ):
            return
        self._settings_for_project(owner[0]).set_element_patch(element_id, patch)
        update = getattr(self.inspector, "update_inline_text", None)
        if callable(update):
            update(element_id, patch["text"])
        # The browser already applied this exact input. Echoing a queued DOM
        # patch back on each key could overwrite a more recent character.
        self._update_visual_save_state()
        self.statusBar().showMessage("UI changes are unsaved. Use Save in Properties.")

    def _inline_edit_finished(self, element_id: str, cancelled: bool) -> None:
        owner = getattr(self, "_inline_visual_owner", None)
        if owner is None or owner[1] != element_id:
            return
        settings = self._settings_for_project(owner[0])
        settings.finish_text_edit(cancel=cancelled)
        self._inline_visual_owner = None
        self._inline_pending = False
        final_draft = settings.load_draft()
        final_element = final_draft.get(element_id, {})
        if isinstance(final_element, dict) and isinstance(final_element.get("text"), str):
            update = getattr(self.inspector, "update_inline_text", None)
            if callable(update):
                # Selection can have been queued before the prior Properties
                # field committed. Mirror the finalized draft, not that old DOM.
                update(element_id, final_element["text"])
        if self.runner.is_project_running(owner[0]) and not self.runner.is_stopping:
            # Escape and typing back to the original can remove a newly added
            # text override entirely. Keep future frame reloads consistent too.
            self.preview.set_draft_settings(final_draft)
        self._update_visual_save_state()

    def _inline_pending_changed(self, pending: bool) -> None:
        self._inline_pending = bool(pending)
        self._update_visual_save_state()

    def _property_changed(self, element_id: str, patch: dict) -> None:
        owner = getattr(self, "_visual_selection_project", None)
        if (
            getattr(self, "_closing_approved", False)
            or owner is None
            or not element_id
            or element_id != self.inspector.element_id
        ):
            return
        settings = self._settings_for_project(owner)
        before = settings.load_draft()
        after = settings.set_element_patch(element_id, patch)
        if before == after:
            return
        if self.runner.is_project_running(owner) and not self.runner.is_stopping:
            self.preview.apply_patch(element_id, patch)
        self._update_visual_save_state()
        self.statusBar().showMessage(
            "UI changes are unsaved. Use Save in Properties."
        )

    def publish(self) -> None:
        if not self._commit_visual_fields():
            self._show_inline_commit_error()
            return
        if not self.current_settings:
            return
        try:
            self.current_settings.publish()
        except (OSError, ValueError) as exc:
            self._update_visual_save_state()
            state = (
                "Your UI changes remain unsaved in Properties."
                if self.current_settings.is_dirty
                else "Your UI changes are saved, but publishing did not complete."
            )
            QMessageBox.critical(
                self, "Could not publish UI changes", f"{state}\n\n{exc}"
            )
            return
        self._update_visual_save_state()
        QMessageBox.information(
            self,
            "Published",
            "Draft settings were copied to .studio/published.json.\n\n"
            "Changes are always re-applied in Project Studio. For the public "
            "website to use them, the managed project must load published.json "
            "or include the Studio runtime helper.",
        )

    def open_publish_dialog(self, target: str = "github") -> None:
        """Save the selected project's files before local publication review."""

        if self.current_project is None or self.current_settings is None:
            return
        if not self._commit_visual_fields():
            self._show_inline_commit_error()
            return
        project = self.current_project
        project_path = project.path.resolve()
        for editor in list(self.open_editors.values()):
            if editor.project_path.resolve() == project_path and editor.is_modified:
                if not editor.save():
                    return
        try:
            self.current_settings.publish()
        except (OSError, ValueError) as exc:
            self._update_visual_save_state()
            QMessageBox.critical(
                self,
                "Could not prepare project for publishing",
                "Studio could not save the selected project's UI settings. "
                f"Your changes are still available.\n\n{exc}",
            )
            return
        self._update_visual_save_state()
        from .publish_dialog import PublishDialog

        dialog = PublishDialog(project.path, project.name, initial_target=target, parent=self)
        dialog.exec()

    def undo(self) -> None:
        focused = QApplication.focusWidget()
        if isinstance(focused, (QPlainTextEdit, QTextEdit, QLineEdit)):
            focused.undo()
            return
        editor = self._current_editor()
        if editor is not None:
            editor.editor.undo()
            return
        if self.document_tabs.currentWidget() is not self.preview:
            return
        if (
            not self.current_settings
            or self.current_project is None
            or not self.runner.is_project_running(self.current_project.path)
        ):
            return
        if not self._commit_visual_fields():
            self._show_inline_commit_error()
            return
        state = self.current_settings.undo()
        if state is not None:
            self.preview.set_draft_settings(state)
            self._clear_visual_selection()
            self.statusBar().showMessage("Undo")

    def redo(self) -> None:
        focused = QApplication.focusWidget()
        if isinstance(focused, (QPlainTextEdit, QTextEdit, QLineEdit)):
            focused.redo()
            return
        editor = self._current_editor()
        if editor is not None:
            editor.editor.redo()
            return
        if self.document_tabs.currentWidget() is not self.preview:
            return
        if (
            not self.current_settings
            or self.current_project is None
            or not self.runner.is_project_running(self.current_project.path)
        ):
            return
        if not self._commit_visual_fields():
            self._show_inline_commit_error()
            return
        state = self.current_settings.redo()
        if state is not None:
            self.preview.set_draft_settings(state)
            self._clear_visual_selection()
            self.statusBar().showMessage("Redo")

    def open_current_folder(self) -> None:
        if self.current_project:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.current_project.path))
            )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not getattr(self, "_closing_approved", False):
            if not self._commit_visual_fields():
                self._show_inline_commit_error()
                event.ignore()
                return
            if not self._can_close_all_editors():
                event.ignore()
                return
            if not self._confirm_visual_exit():
                event.ignore()
                return
            self._closing_approved = True
            self._save_workspace_layout()
        updates = getattr(self, "update_controller", None)
        if updates is not None:
            updates.begin_shutdown()
        if updates is not None and updates.is_active:
            self._waiting_for_update_shutdown = True
            self._set_closing_controls_enabled(False)
            event.ignore()
            return
        controller = getattr(self, "background_setup", None)
        if controller is not None and controller.is_active:
            self._waiting_for_setup_shutdown = True
            # The existing save/discard prompts have completed. Prevent new
            # edits while cancellation releases the installer and its lock.
            self._set_closing_controls_enabled(False)
            controller.cancel()
            event.ignore()
            return
        if getattr(self, "_update_restart_requested", None) is not None:
            try:
                # The trusted helper stages its process outside the app and
                # waits for this process to exit before replacing any file.
                updates.launch_pending_update()
            except Exception as error:
                self._closing_approved = False
                self._update_restart_requested = None
                self._waiting_for_setup_shutdown = False
                self._set_closing_controls_enabled(True)
                updates.update_launch_failed("Studio could not start the update. Download it again to prepare a fresh restart.")
                from .update_install import UpdateInstallError
                detail = str(error) if isinstance(error, UpdateInstallError) else (
                    "Studio could not start its update helper. Your installed files have not changed."
                )
                QMessageBox.warning(self, "Update could not start", detail)
                event.ignore()
                return
            self._update_restart_requested = None
        for editor in list(self.open_editors.values()):
            editor.shutdown()
        shutdown_python_assist()
        script_runner = getattr(self, "script_runner", None)
        if script_runner is not None:
            script_runner.shutdown()
        self.preview.stop_preview()
        self.runner.shutdown()
        self.console.shutdown()
        super().closeEvent(event)
