from __future__ import annotations

import json
import shutil
from pathlib import Path

from PySide6.QtCore import QSize, QSettings, QSignalBlocker, QStandardPaths, Qt, QUrl
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
        self.preview.preview_loaded.connect(
            lambda url: self.statusBar().showMessage(f"Preview loaded: {url}")
        )

        self.inspector = PropertyInspector()
        self.inspector.patch_changed.connect(self._property_changed)

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
        self.document_tabs.addTab(self.preview, "Preview")
        self.document_tabs.setTabToolTip(
            0,
            "Live Streamlit preview. Closing this tab does not stop the project.",
        )

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

    def _close_workspace_tab(self, index: int) -> None:
        widget = self.document_tabs.widget(index)
        if widget is None:
            return

        if widget is self.preview:
            self.document_tabs.removeTab(index)
            self._preview_user_closed = True
            if hasattr(self, "edit_action") and self.edit_action.isChecked():
                self.edit_action.setChecked(False)
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
        )
        editor.language_changed.connect(
            lambda mode, target=path: self._remember_editor_language(target, mode)
        )
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

    def _save_all_documents(self) -> None:
        for editor in list(self.open_editors.values()):
            if editor.is_modified and not editor.save():
                return
        self.statusBar().showMessage("All open files saved")

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
        if not url:
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
        if self.preview.current_preview_url != url:
            self.preview.load_preview(url)

        running_path = self.runner.running_project_path
        if running_path is not None:
            try:
                settings = SettingsStore(running_path)
                self.preview.set_draft_settings(settings.load_draft())
            except OSError:
                pass
        self.statusBar().showMessage(
            f"Opened running preview: {project_name}"
        )

    def _refresh_running_preview(self) -> None:
        if self._preview_tab_index() < 0:
            self.open_running_preview()
            return
        if self.runner.preview_url:
            self.preview.refresh_preview()
            self.document_tabs.setCurrentWidget(self.preview)

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
            "inspector": "Inspector",
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
            lambda _checked=False: self.runner.restart_project()
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
            "Publish the current visual draft to the selected project"
        )
        self.publish_action.triggered.connect(self.publish)
        toolbar.addAction(self.publish_action)
        publish_button = toolbar.widgetForAction(self.publish_action)
        if isinstance(publish_button, QToolButton):
            publish_button.setObjectName("PublishToolbarButton")

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
            ("inspector", "Inspector"),
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

        self._update_project_actions()

    def _update_project_actions(self) -> None:
        """Keep actions aligned with the selected and running projects."""

        if not hasattr(self, "run_action"):
            return

        has_project = self.current_project is not None
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
            self.runner.is_preview_running and preview_open
        )
        self.open_preview_action.setEnabled(
            self.runner.is_preview_running and bool(self.runner.preview_url)
        )
        self.edit_action.setEnabled(
            selected_is_running and preview_open and preview_selected
        )
        self.undo_action.setEnabled(has_project or current_editor is not None)
        self.redo_action.setEnabled(has_project or current_editor is not None)
        self.publish_action.setEnabled(has_project)
        self.save_file_action.setEnabled(current_editor is not None)
        self.save_all_action.setEnabled(bool(self.open_editors))
        self.close_tab_action.setEnabled(self.document_tabs.count() > 0)

        # If another project is running, Run performs a smooth switch instead
        # of making the user stop it manually first.
        can_switch_or_run = (
            not any_process_running or self.runner.is_preview_running
        )
        self.run_action.setEnabled(
            has_project and not selected_is_running and can_switch_or_run
        )
        self.run_action.setText(
            "Switch and Run"
            if has_project and any_process_running and not selected_is_running
            else "Run"
        )
        self.run_action.setToolTip(
            "Stop the current preview and run the selected project"
            if any_process_running and not selected_is_running
            else "Run the selected Streamlit project"
        )

        self.stop_action.setEnabled(any_process_running)
        self.restart_action.setEnabled(selected_is_running)
        self.install_action.setEnabled(has_project and not any_process_running)

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
            self.current_project = None
            self.current_settings = None
            self.project_explorer.clear_project()
            self.inspector.clear_selection()
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
        if self.runner.is_project_running(self.current_project.path):
            self.runner.stop_project(reason="user")
        self.library.remove(self.current_project.id)
        self.current_project = None
        self.current_settings = None
        self.project_explorer.clear_project()
        self.inspector.clear_selection()
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
        self.current_project = project
        self.current_settings = SettingsStore(project.path)
        self.library.touch(project.id)
        self.project_explorer.set_project(project.path)
        self.console.set_working_directory(project.path)
        self.inspector.clear_selection()
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
                if self.preview.current_preview_url != self.runner.preview_url:
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
        # A preview may become ready after the user selected another project or
        # closed the Preview tab. Never force the tab back open: closing the
        # tab only hides the browser while the Streamlit process stays alive.
        selected_matches = (
            self.current_project is not None
            and Path(project_path).resolve() == self.current_project.path.resolve()
        )
        preview_index = self._preview_tab_index()

        if selected_matches and preview_index >= 0:
            self.document_tabs.setTabText(
                preview_index,
                f"Preview — {self.current_project.name}",
            )
            self.preview.load_preview(url)
            if self.current_settings:
                self.preview.set_draft_settings(
                    self.current_settings.load_draft()
                )
            self.document_tabs.setCurrentIndex(preview_index)
            self.statusBar().showMessage(f"Preview: {url}")
        elif selected_matches:
            self._append_log(
                f"Preview is ready but its tab is closed: {url}\n"
            )
            self.statusBar().showMessage(
                "Project is running. Use Run → Open Running Preview."
            )
        else:
            self._append_log(
                f"Preview is ready in the background: {url}\n"
            )
        self._update_project_actions()

    def _runner_state_changed(self, state: str) -> None:
        self._update_project_actions()
        self.statusBar().showMessage(f"Project state: {state}")

    def _runner_process_started(
        self,
        pid: int,
        project_name: str,
        project_path: str,
    ) -> None:
        self.console.append_system_message(
            f"Streamlit started: {project_name} (PID {pid})"
        )
        self.console.set_working_directory(Path(project_path))
        self.console.show_process(pid)

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
        self._update_project_actions()

        if self.edit_action.isChecked():
            self.edit_action.setChecked(False)

        if (
            self.current_project is not None
            and not self.runner.is_project_running(self.current_project.path)
            and self.runner.running_project_path is None
            and not self.runner.has_pending_start
            and self._preview_tab_index() >= 0
        ):
            index = self._preview_tab_index()
            self.document_tabs.setTabText(
                index,
                f"Preview — {self.current_project.name}",
            )
            self.preview.show_project_placeholder(self.current_project.name)

    def _show_runner_error(self, message: str) -> None:
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

    def _toggle_edit_mode(self, enabled: bool) -> None:
        self.preview.set_edit_mode(enabled)
        self.inspector.clear_selection()
        if enabled:
            self.statusBar().showMessage(
                "Edit Mode is on — point at any visible element and click it"
            )
        else:
            self.statusBar().showMessage("Edit Mode is off")

    def _element_selected(self, data: dict) -> None:
        self.inspector.set_selection(data)
        self.statusBar().showMessage(
            f"Selected element: {data.get('label') or data.get('id', '')}"
        )

    def _property_changed(self, element_id: str, patch: dict) -> None:
        if not self.current_settings:
            return
        self.current_settings.set_element_patch(element_id, patch)
        self.preview.apply_patch(element_id, patch)
        self.statusBar().showMessage(f"Draft updated: {element_id}")

    def publish(self) -> None:
        if not self.current_settings:
            return
        self.current_settings.publish()
        QMessageBox.information(
            self,
            "Published",
            "Draft settings were copied to .studio/published.json.\n\n"
            "Changes are always re-applied in Project Studio. For the public "
            "website to use them, the managed project must load published.json "
            "or include the Studio runtime helper.",
        )

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
        if not self.current_settings:
            return
        state = self.current_settings.undo()
        if state is not None:
            self.preview.set_draft_settings(state)
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
        if not self.current_settings:
            return
        state = self.current_settings.redo()
        if state is not None:
            self.preview.set_draft_settings(state)
            self.statusBar().showMessage("Redo")

    def open_current_folder(self) -> None:
        if self.current_project:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.current_project.path))
            )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self._can_close_all_editors():
            event.ignore()
            return
        self._save_workspace_layout()
        for editor in list(self.open_editors.values()):
            editor.shutdown()
        shutdown_python_assist()
        self.runner.shutdown()
        self.console.shutdown()
        super().closeEvent(event)
