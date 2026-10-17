from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir, QEvent, QModelIndex, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFileSystemModel,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from studio.file_operations import (
    copy_project_file,
    create_project_file,
    create_project_folder,
    project_entry,
    validate_entry_name,
)


class NewFileDialog(QDialog):
    """Choose a file type and show the exact name before creating it."""

    def __init__(self, project_root: Path, folder: Path, parent=None) -> None:
        super().__init__(parent)
        self._project_root = project_root
        self._folder = folder
        self.created_path: Path | None = None
        self.setWindowTitle("New File")
        self.setMinimumWidth(450)

        layout = QVBoxLayout(self)
        self.file_type_combo = QComboBox()
        self.file_type_combo.setObjectName("NewFileType")
        self.file_type_combo.addItem("Python (.py)", ".py")
        self.file_type_combo.addItem("Plain text (.txt)", ".txt")
        self.file_type_combo.addItem("Other (keep name)", "")
        type_label = QLabel("File type:")
        type_label.setBuddy(self.file_type_combo)
        layout.addWidget(type_label)
        layout.addWidget(self.file_type_combo)

        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("NewFileName")
        self.name_edit.setPlaceholderText("For example: helpers or helpers.py")
        name_label = QLabel("File name:")
        name_label.setBuddy(self.name_edit)
        layout.addWidget(name_label)
        layout.addWidget(self.name_edit)

        self.extension_hint = QLabel()
        self.extension_hint.setWordWrap(True)
        self.extension_hint.setStyleSheet("color: #a6b5ce;")
        layout.addWidget(self.extension_hint)
        self.filename_preview = QLabel()
        self.filename_preview.setObjectName("NewFilePreview")
        self.filename_preview.setTextFormat(Qt.TextFormat.PlainText)
        self.filename_preview.setWordWrap(True)
        self.filename_preview.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.filename_preview)

        destination = QLabel(f"Create in: {folder}")
        destination.setTextFormat(Qt.TextFormat.PlainText)
        destination.setWordWrap(True)
        destination.setStyleSheet("color: #a6b5ce;")
        layout.addWidget(destination)

        self.error_label = QLabel()
        self.error_label.setObjectName("NewFileError")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ff9595;")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.create_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.create_button.setText("Create")
        self.create_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.name_edit.textChanged.connect(self._update_preview)
        self.file_type_combo.currentIndexChanged.connect(self._update_preview)
        self._update_preview()
        self.name_edit.setFocus()

    def file_name(self) -> str:
        name = self.name_edit.text()
        # Validate what the user typed before appending anything. Otherwise an
        # invalid trailing dot/space could be hidden by a generated extension.
        validate_entry_name(name)
        extension = self.file_type_combo.currentData()
        if extension and not Path(name).suffix and not name.startswith("."):
            return name + extension
        # Typed extensions and conventional dotfiles always keep their name.
        return name

    def _update_preview(self, *_args) -> None:
        extension = self.file_type_combo.currentData()
        self.extension_hint.setText(
            f"Add {extension} to names without an extension. Any extension you type is kept."
            if extension else "Keep the filename exactly as typed, including any extension."
        )
        self.name_edit.setPlaceholderText(
            "For example: helpers or helpers.py"
            if extension == ".py" else
            "For example: notes or notes.txt"
            if extension == ".txt" else "For example: config.json or Dockerfile"
        )
        self.error_label.hide()
        try:
            name = self.file_name()
        except ValueError as error:
            self.filename_preview.setText("Enter a valid file name to see what will be created.")
            self.create_button.setEnabled(False)
            if self.name_edit.text():
                self.error_label.setText(str(error))
                self.error_label.show()
            return
        self.filename_preview.setText(f"File to create: {name}")
        self.create_button.setEnabled(True)

    def accept(self) -> None:
        try:
            name = self.file_name()
            self.created_path = create_project_file(self._project_root, self._folder, name)
        except (ValueError, OSError) as error:
            # Keep the same dialog and entered name so the user can correct a
            # collision or validation error, or cancel, without a prompt loop.
            self.error_label.setText(str(error))
            self.error_label.show()
            self.name_edit.setFocus()
            return
        super().accept()


class ProjectExplorer(QWidget):
    """File-system tree rooted at the selected project folder."""

    file_opened = Signal(object)
    collapse_requested = Signal()
    delete_requested = Signal(object)
    paths_added = Signal(object)
    status_message = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._project_path: Path | None = None
        self._pending_selection: Path | None = None
        self._pending_expanded_paths: set[Path] = set()
        self._loaded_directories: set[Path] = set()

        self.path_label = QLabel("No project selected")
        self.path_label.setObjectName("ExplorerPath")
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.path_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Refresh the project explorer")
        self.refresh_button.clicked.connect(self.refresh)

        self.collapse_button = QPushButton("−")
        self.collapse_button.setObjectName("PanelCollapseButton")
        self.collapse_button.setFixedWidth(30)
        self.collapse_button.setToolTip(
            "Collapse Project Explorer. Restore it from the Panels menu."
        )
        self.collapse_button.clicked.connect(self.collapse_requested.emit)

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addWidget(self.path_label, 1)
        heading.addWidget(self.refresh_button)
        heading.addWidget(self.collapse_button)

        self.model = QFileSystemModel(self)
        self.model.setReadOnly(True)
        self.model.setFilter(
            QDir.Filter.AllDirs
            | QDir.Filter.Files
            | QDir.Filter.NoDotAndDotDot
            | QDir.Filter.Hidden
        )

        self.tree = QTreeView()
        self.tree.setObjectName("ProjectTree")
        self.tree.setModel(self.model)
        # Alternating rows use the operating-system palette on some Windows
        # themes, which can turn every second row bright white. Keep the tree
        # fully dark and use hover/selection styling instead.
        self.tree.setAlternatingRowColors(False)
        self.tree.setAnimated(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setItemsExpandable(True)
        # Folder rows can be toggled by their chevron with one click. Folder
        # double-clicking is handled explicitly below so it cannot toggle twice.
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setIndentation(22)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.viewport().installEventFilter(self)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.clicked.connect(self._clicked_index)
        self.tree.doubleClicked.connect(self._open_index)
        self.tree.activated.connect(self._open_index)
        self.tree.selectionModel().selectionChanged.connect(self._update_actions)
        self.model.directoryLoaded.connect(self._directory_loaded)
        self.model.rowsInserted.connect(self._restore_tree_state)
        self.model.rowsRemoved.connect(self._update_actions)

        self.delete_action = QAction("Delete", self.tree)
        self.delete_action.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        # A Delete key pressed in an editor must only edit its text.
        self.delete_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self.delete_action.triggered.connect(self.delete_selected)
        self.tree.addAction(self.delete_action)

        self.new_file_button = QPushButton("New File")
        self.new_file_button.setToolTip(
            "Create a Python file (.py) or choose another file type, then open it"
        )
        self.new_file_button.clicked.connect(self.new_file)
        self.new_folder_button = QPushButton("New Folder")
        self.new_folder_button.setToolTip("Create a folder inside the selected folder")
        self.new_folder_button.clicked.connect(self.new_folder)
        self.add_files_button = QPushButton("Add Files…")
        self.add_files_button.setToolTip("Copy existing files into the selected folder")
        self.add_files_button.clicked.connect(self.add_files)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setToolTip(
            "Remove the selected file or folder after confirmation (recycle bin / trash)"
        )
        self.delete_button.clicked.connect(self.delete_selected)

        controls = QGridLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setHorizontalSpacing(6)
        controls.setVerticalSpacing(5)
        for position, button in enumerate((
            self.new_file_button,
            self.new_folder_button,
            self.add_files_button,
            self.delete_button,
        )):
            button.setObjectName("ExplorerFileAction")
            button.setStyleSheet(
                "QPushButton#ExplorerFileAction { padding: 5px 8px; "
                "min-height: 23px; font-size: 12px; border-radius: 6px; } "
                "QPushButton#ExplorerFileAction:disabled { color: #667895; "
                "background: #10192b; border-color: #253550; }"
            )
            controls.addWidget(button, position // 2, position % 2)
        controls.setColumnStretch(0, 1)
        controls.setColumnStretch(1, 1)

        self.destination_label = QLabel()
        self.destination_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.destination_label.setStyleSheet("color: #93a6c8; font-size: 11px;")

        resource_root = Path(__file__).resolve().parent.parent / "resources"
        closed_icon = (resource_root / "chevron-right.svg").as_posix()
        open_icon = (resource_root / "chevron-down.svg").as_posix()
        self.tree.setStyleSheet(
            f"""
            QTreeView#ProjectTree::branch:has-children:closed {{
                image: url("{closed_icon}");
            }}
            QTreeView#ProjectTree::branch:has-children:open {{
                image: url("{open_icon}");
            }}
            QTreeView#ProjectTree::branch:!has-children {{
                image: none;
            }}
            """
        )

        for column in range(1, 4):
            self.tree.hideColumn(column)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(heading)
        layout.addLayout(controls)
        layout.addWidget(self.destination_label)
        layout.addWidget(self.tree, 1)

        self.clear_project()

    @property
    def project_path(self) -> Path | None:
        return self._project_path

    @property
    def selected_path(self) -> Path | None:
        """The selected, existing project entry, with boundary validation."""

        path = self._selected_model_path()
        if self._project_path is None or path is None:
            return None
        try:
            return project_entry(self._project_path, path)
        except ValueError:
            return None

    def set_project(self, project_path: Path) -> None:
        self._project_path = project_path.expanduser().resolve()
        self._pending_selection = None
        self._pending_expanded_paths.clear()
        self._loaded_directories.clear()
        self.tree.clearSelection()
        root = self.model.setRootPath(str(self._project_path))
        self.tree.setRootIndex(root)
        self.path_label.setText(self._project_path.name)
        self.tree.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.path_label.setToolTip(str(self._project_path))
        self.tree.expand(root)
        self._update_actions()

    def clear_project(self) -> None:
        self._project_path = None
        self._pending_selection = None
        self._pending_expanded_paths.clear()
        self._loaded_directories.clear()
        self.tree.clearSelection()
        self.path_label.setText("No project selected")
        self.path_label.setToolTip("")
        self.tree.setRootIndex(QModelIndex())
        self.tree.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self._update_actions()

    def refresh(self) -> None:
        if self._project_path is None:
            return
        current = self._project_path
        self._pending_selection = self.selected_path
        self._pending_expanded_paths = self._expanded_paths()
        # Resetting the root path asks QFileSystemModel to rescan the directory.
        self.model.setRootPath("")
        root = self.model.setRootPath(str(current))
        self.tree.setRootIndex(root)
        self.tree.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self._restore_tree_state()

    def _selected_model_path(self) -> Path | None:
        indexes = self.tree.selectionModel().selectedRows(0)
        return self._path_for_index(indexes[0]) if indexes else None

    def _target_folder(self, path: Path | None = None) -> Path:
        if self._project_path is None:
            raise ValueError("Open a project before adding files or folders.")
        target = path if path is not None else self._selected_model_path()
        target = project_entry(
            self._project_path, target or self._project_path, allow_root=True
        )
        directory = target if target.is_dir() else target.parent
        return project_entry(self._project_path, directory, allow_root=True)

    def _update_actions(self, *_args) -> None:
        try:
            directory = self._target_folder()
        except ValueError:
            directory = None
        enabled = directory is not None
        self.new_file_button.setEnabled(enabled)
        self.new_folder_button.setEnabled(enabled)
        self.add_files_button.setEnabled(enabled)
        can_delete = self.selected_path is not None
        self.delete_button.setEnabled(can_delete)
        self.delete_action.setEnabled(can_delete)
        if directory is not None:
            relative = directory.relative_to(self._project_path)
            label = "project root" if str(relative) == "." else str(relative)
            self.destination_label.setText(f"Add to: {label}")
            self.destination_label.setToolTip(
                f"New files, new folders, and copied files go to:\n{directory}\n\n"
                "Select a folder to change the destination. Selecting a file uses its parent folder.\n"
                "Click empty space in the tree to use the project root."
            )
        else:
            self.destination_label.setText(
                "Select a folder inside the project"
                if self._project_path is not None else "Open a project to manage files"
            )
            self.destination_label.setToolTip("")

    def new_file(self) -> None:
        self._create_item(is_folder=False)

    def new_folder(self) -> None:
        self._create_item(is_folder=True)

    def _create_item(self, is_folder: bool, folder: Path | None = None) -> None:
        try:
            directory = self._target_folder(folder)
        except ValueError as error:
            self.status_message.emit(str(error))
            return
        if not is_folder:
            dialog = NewFileDialog(self._project_path, directory, self)
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.created_path is None:
                return
            path = dialog.created_path
        else:
            name = ""
            while True:
                name, accepted = QInputDialog.getText(
                    self, "New Folder", f"Folder name:\n\nCreate in: {directory}", text=name
                )
                if not accepted:
                    return
                try:
                    path = create_project_folder(self._project_path, directory, name)
                except (ValueError, FileExistsError) as error:
                    QMessageBox.warning(self, "New Folder", str(error))
                    continue
                except OSError as error:
                    QMessageBox.warning(self, "New Folder", f"Could not create {name!r}:\n{error}")
                    return
                break
        self.paths_added.emit([path])
        self.select_path(path)
        self.status_message.emit(f"Created {path.relative_to(self._project_path)}")
        if not is_folder:
            self.file_opened.emit(path)

    def add_files(self) -> None:
        self._add_files_to()

    def _add_files_to(self, folder: Path | None = None) -> None:
        try:
            directory = self._target_folder(folder)
        except ValueError as error:
            self.status_message.emit(str(error))
            return
        selected, _filter = QFileDialog.getOpenFileNames(
            self, f"Add Files — copy into {directory}", str(Path.home()), "All files (*)"
        )
        if not selected:
            return
        added: list[Path] = []
        skipped: list[str] = []
        for filename in selected:
            source = Path(filename)
            try:
                added.append(copy_project_file(self._project_path, directory, source))
            except (OSError, ValueError) as error:
                skipped.append(f"{source.name}: {error}")
        if added:
            self.paths_added.emit(added)
            self.select_path(added[0])
        message = f"Added {len(added)} file{'s' if len(added) != 1 else ''} to {directory.name}"
        if skipped:
            message += f"; skipped {len(skipped)}"
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle("Add Files")
            dialog.setText(message + ".")
            dialog.setInformativeText(
                "Files with existing names are skipped. Existing files are never replaced.\n\n"
                + "\n".join(skipped[:6])
                + ("\n\nSee details for the full list." if len(skipped) > 6 else "")
            )
            dialog.setDetailedText("\n".join(skipped))
            dialog.exec()
        self.status_message.emit(message)

    def delete_selected(self) -> None:
        """Let the main window confirm, check unsaved tabs, and recycle it."""

        path = self.selected_path
        if path is not None:
            self.delete_requested.emit(path)

    def select_path(self, path: Path) -> None:
        if self._project_path is None:
            return
        try:
            candidate = project_entry(self._project_path, path, allow_root=True)
            if candidate == self._project_path:
                # The root row is hidden by setRootIndex(). Selecting the root
                # means clearing a child selection, not selecting an invisible
                # row or retaining the deleted entry's former model index.
                self._pending_selection = None
                self.tree.selectionModel().clear()
                self._update_actions()
                return
            self._pending_selection = project_entry(self._project_path, path)
        except ValueError:
            return
        self._restore_tree_state()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if (
            watched is self.tree.viewport()
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton)
            and not self.tree.indexAt(event.position().toPoint()).isValid()
            and self._project_path is not None
        ):
            self.select_path(self._project_path)
        return super().eventFilter(watched, event)

    def _expanded_paths(self) -> set[Path]:
        paths: set[Path] = set()

        def visit(parent: QModelIndex) -> None:
            for row in range(self.model.rowCount(parent)):
                index = self.model.index(row, 0, parent)
                if self.tree.isExpanded(index):
                    path = self._path_for_index(index)
                    if path is not None:
                        paths.add(path)
                    visit(index)

        visit(self.tree.rootIndex())
        return paths

    def _directory_loaded(self, directory: str) -> None:
        self._loaded_directories.add(Path(directory))
        self._restore_tree_state()

    def _restore_tree_state(self, *_args) -> None:
        if self._project_path is None:
            return
        for path in tuple(self._pending_expanded_paths):
            index = self.model.index(str(path))
            if index.isValid():
                self.tree.expand(index)
                self._pending_expanded_paths.discard(path)
        if self._pending_selection is not None:
            selected_path = self._pending_selection
            index = self.model.index(str(selected_path))
            if index.isValid():
                parent = index.parent()
                while parent.isValid() and parent != self.tree.rootIndex():
                    self.tree.expand(parent)
                    parent = parent.parent()
                self.tree.setCurrentIndex(index)
                self.tree.scrollTo(index)
                # QFileSystemModel loads asynchronously. Its first directory
                # snapshot may predate a just-created file and temporarily
                # remove that row. Keep the request until that scan completes.
                if selected_path.parent in self._loaded_directories:
                    self._pending_selection = None
        self._update_actions()

    def _path_for_index(self, index: QModelIndex) -> Path | None:
        if not index.isValid():
            return None
        value = self.model.filePath(index)
        return Path(value) if value else None

    def _clicked_index(self, index: QModelIndex) -> None:
        """Open text files on a normal click, like an IDE Explorer."""

        self._pending_selection = None
        path = self._path_for_index(index)
        if path is not None and path.is_file():
            self.file_opened.emit(path)

    def _open_index(self, index: QModelIndex) -> None:
        path = self._path_for_index(index)
        if path is None:
            return
        if path.is_dir():
            self.tree.setExpanded(index, not self.tree.isExpanded(index))
            return
        self.file_opened.emit(path)

    def _show_context_menu(self, position) -> None:
        index = self.tree.indexAt(position)
        path = self._path_for_index(index)
        if index.isValid():
            self._pending_selection = None
            self.tree.setCurrentIndex(index)
        elif self._project_path is not None:
            self.select_path(self._project_path)
        try:
            folder = self._target_folder(path or self._project_path)
        except ValueError:
            folder = None

        menu = QMenu(self)

        new_file_action = menu.addAction("New File…")
        new_file_action.setEnabled(folder is not None)
        new_file_action.triggered.connect(
            lambda: self._create_item(is_folder=False, folder=folder)
        )
        new_folder_action = menu.addAction("New Folder…")
        new_folder_action.setEnabled(folder is not None)
        new_folder_action.triggered.connect(
            lambda: self._create_item(is_folder=True, folder=folder)
        )
        add_files_action = menu.addAction("Add Files…")
        add_files_action.setEnabled(folder is not None)
        add_files_action.triggered.connect(lambda: self._add_files_to(folder))
        menu.addSeparator()

        if path is not None:
            open_action = QAction(
                "Open folder" if path.is_dir() else "Open in Project Studio",
                self,
            )
            open_action.triggered.connect(lambda: self._open_index(index))
            menu.addAction(open_action)

            if path.is_file():
                system_action = QAction("Open with system application", self)
                system_action.triggered.connect(
                    lambda: QDesktopServices.openUrl(
                        QUrl.fromLocalFile(str(path))
                    )
                )
                menu.addAction(system_action)

            reveal_action = QAction("Show in File Explorer", self)
            reveal_action.triggered.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(path if path.is_dir() else path.parent))
                )
            )
            menu.addAction(reveal_action)

            copy_action = QAction("Copy path", self)
            copy_action.triggered.connect(
                lambda: QGuiApplication.clipboard().setText(str(path))
            )
            menu.addAction(copy_action)
            menu.addSeparator()
            menu.addAction(self.delete_action)

            if path.is_dir():
                menu.addSeparator()
                if self.tree.isExpanded(index):
                    collapse_action = QAction("Collapse folder", self)
                    collapse_action.triggered.connect(
                        lambda: self.tree.collapse(index)
                    )
                    menu.addAction(collapse_action)
                else:
                    expand_action = QAction("Expand folder", self)
                    expand_action.triggered.connect(
                        lambda: self.tree.expand(index)
                    )
                    menu.addAction(expand_action)

            menu.addSeparator()

        expand_all_action = QAction("Expand all folders", self)
        expand_all_action.triggered.connect(self.tree.expandAll)
        menu.addAction(expand_all_action)

        collapse_all_action = QAction("Collapse all folders", self)
        collapse_all_action.triggered.connect(self.tree.collapseAll)
        menu.addAction(collapse_all_action)
        menu.addSeparator()

        hide_action = QAction("Collapse Project Explorer panel", self)
        hide_action.triggered.connect(self.collapse_requested.emit)
        menu.addAction(hide_action)
        menu.addSeparator()

        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.refresh)
        menu.addAction(refresh_action)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        enabled = self._project_path is not None
        self.tree.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self._update_actions()
