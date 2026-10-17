from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


STUDIO_DIR_NAME = ".studio"
MANIFEST_FILE_NAME = "project.json"


@dataclass(slots=True)
class ProjectManifest:
    """Configuration required to run and preview a managed project."""

    name: str
    project_type: str = "streamlit"
    entry_point: str = "app.py"
    preview_port: int = 8501
    python_executable: str = ""

    @property
    def is_streamlit(self) -> bool:
        return self.project_type.lower() == "streamlit"

    def validate(self, project_path: Path) -> list[str]:
        errors: list[str] = []

        if not self.name.strip():
            errors.append("Project name cannot be empty.")

        if self.project_type.lower() != "streamlit":
            errors.append("The MVP currently supports Streamlit projects only.")

        if not (1 <= int(self.preview_port) <= 65535):
            errors.append("Preview port must be between 1 and 65535.")

        entry_path = project_path / self.entry_point
        if not entry_path.exists():
            errors.append(f"Entry point does not exist: {entry_path}")

        return errors


def studio_directory(project_path: Path) -> Path:
    return project_path / STUDIO_DIR_NAME


def manifest_path(project_path: Path) -> Path:
    return studio_directory(project_path) / MANIFEST_FILE_NAME


def load_manifest(project_path: Path) -> ProjectManifest | None:
    path = manifest_path(project_path)
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None

    if not isinstance(payload, dict):
        return None

    try:
        return ProjectManifest(
            name=str(payload.get("name") or project_path.name),
            project_type=str(payload.get("project_type") or "streamlit"),
            entry_point=str(payload.get("entry_point") or "app.py"),
            preview_port=int(payload.get("preview_port") or 8501),
            python_executable=str(payload.get("python_executable") or ""),
        )
    except (TypeError, ValueError):
        return None


def save_manifest(project_path: Path, manifest: ProjectManifest) -> Path:
    studio_directory(project_path).mkdir(parents=True, exist_ok=True)
    path = manifest_path(project_path)
    path.write_text(
        json.dumps(asdict(manifest), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path
