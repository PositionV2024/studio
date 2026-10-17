from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

from .project_manifest import ProjectManifest


def _slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return value.strip("-._") or "new-streamlit-project"


class NewProjectDialog(QDialog):
    """Collect the settings required to create a new Streamlit project."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create New Project")
        self.setMinimumWidth(520)

        self.name = QLineEdit("My Streamlit Project")
        self.folder_name = QLineEdit("my-streamlit-project")
        self.parent_folder = QLineEdit()
        self.entry_point = QLineEdit("app.py")
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(8501)

        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.addWidget(self.parent_folder, 1)
        folder_layout.addWidget(browse)

        self.name.textEdited.connect(
            lambda text: self.folder_name.setText(_slugify(text).lower())
        )

        form = QFormLayout(self)
        form.addRow("Project name", self.name)
        form.addRow("Parent folder", folder_row)
        form.addRow("Folder name", self.folder_name)
        form.addRow("Entry point", self.entry_point)
        form.addRow("Preview port", self.port)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create Project")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @property
    def target_path(self) -> Path:
        return Path(self.parent_folder.text().strip()).expanduser() / self.folder_name.text().strip()

    def manifest(self) -> ProjectManifest:
        return ProjectManifest(
            name=self.name.text().strip(),
            project_type="streamlit",
            entry_point=self.entry_point.text().strip() or "app.py",
            preview_port=self.port.value(),
        )

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose where the new project should be created",
        )
        if selected:
            self.parent_folder.setText(selected)

    def _accept_if_valid(self) -> None:
        parent = Path(self.parent_folder.text().strip()).expanduser()
        folder = self.folder_name.text().strip()
        manifest = self.manifest()

        errors: list[str] = []
        if not manifest.name:
            errors.append("Project name cannot be empty.")
        if not parent.exists() or not parent.is_dir():
            errors.append("Choose an existing parent folder.")
        if not folder:
            errors.append("Folder name cannot be empty.")
        if self.target_path.exists():
            errors.append(f"The target folder already exists:\n{self.target_path}")

        if errors:
            QMessageBox.warning(self, "Cannot create project", "\n\n".join(errors))
            return
        self.accept()
