from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QSpinBox,
)

from .project_manifest import ProjectManifest


class ImportProjectDialog(QDialog):
    def __init__(self, project_path: Path, existing: ProjectManifest | None = None, parent=None) -> None:
        super().__init__(parent)
        self.project_path = project_path
        self.setWindowTitle("Import project")
        self.setMinimumWidth(440)

        existing = existing or ProjectManifest(name=project_path.name)
        self._python_executable = existing.python_executable
        self.name = QLineEdit(existing.name)
        self.entry = QLineEdit(existing.entry_point)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(existing.preview_port)

        form = QFormLayout(self)
        form.addRow("Project folder", QLineEdit(str(project_path)))
        form.addRow("Project name", self.name)
        form.addRow("Entry point", self.entry)
        form.addRow("Preview port", self.port)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def manifest(self) -> ProjectManifest:
        return ProjectManifest(
            name=self.name.text().strip(),
            project_type="streamlit",
            entry_point=self.entry.text().strip() or "app.py",
            preview_port=self.port.value(),
            python_executable=self._python_executable,
        )

    def _accept_if_valid(self) -> None:
        manifest = self.manifest()
        errors = manifest.validate(self.project_path)
        if errors:
            QMessageBox.warning(self, "Cannot import project", "\n".join(errors))
            return
        self.accept()
