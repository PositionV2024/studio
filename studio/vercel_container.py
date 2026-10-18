"""Prepare reviewable Streamlit files for Vercel Container Runtime.

Preparation only reads the project. Installation creates the exact reviewed
files exclusively; existing deployment files are never replaced.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_GENERATED_NAMES = (
    "Dockerfile.vercel", ".dockerignore", "requirements-vercel.txt", "studio_vercel_start.py",
)
_CONFLICT_NAMES = (
    *_GENERATED_NAMES, "Containerfile.vercel", "vercel.json", "Dockerfile", "Containerfile",
    "Dockerfile.vercel.dockerignore", "Containerfile.vercel.dockerignore",
)


@dataclass(frozen=True)
class StreamlitContainerPlan:
    project_path: Path
    entry_point: str
    files: dict[str, str]
    notes: list[str]


def _project_root(project_path: Path) -> Path:
    selected = Path(project_path).expanduser().absolute()
    if selected.is_symlink():
        raise ValueError("Choose the actual project folder rather than a symbolic link.")
    root = selected.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The selected project folder does not exist.")
    return root


def _plain_file(root: Path, relative: str) -> Path:
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Use a project file without symbolic links: {relative}")
    if not path.resolve().is_relative_to(root):
        raise ValueError("The selected file must be inside this project.")
    if not path.is_file():
        raise ValueError(f"The project file does not exist: {relative}")
    return path


def _entry_point(root: Path, entry_point: str) -> str:
    if not isinstance(entry_point, str):
        raise ValueError("Choose a Python entry point inside this project.")
    relative = entry_point.strip().replace("\\", "/")
    if relative.startswith("-"):
        raise ValueError("The entry point cannot start with '-'. Choose a Python file without a leading dash.")
    parts = relative.split("/")
    if (
        not relative
        or any(part in {"", ".", ".."} for part in parts)
        or any(character in relative for character in ("\x00", "\n", "\r", ":"))
        or any(ord(character) < 32 for character in relative)
        or PurePosixPath(relative).is_absolute()
        or PurePosixPath(relative).suffix.lower() != ".py"
    ):
        raise ValueError("Choose a relative .py entry point inside this project, such as app.py.")
    _plain_file(root, relative)
    return relative


def _check_conflicts(root: Path, *, created_names: set[str] | None = None) -> None:
    created_names = created_names or set()
    existing = [
        name for name in _CONFLICT_NAMES
        if name not in created_names and os.path.lexists(root / name)
    ]
    if existing:
        raise ValueError(
            "Existing deployment files were left unchanged: " + ", ".join(existing)
            + ". Review and adapt that configuration for Vercel instead of generating over it."
        )


def _requirements(root: Path) -> tuple[str, list[str]]:
    notes = []
    if os.path.lexists(root / "requirements.txt"):
        _plain_file(root, "requirements.txt")
        dependencies = ["-r requirements.txt", "streamlit"]
        notes.append("The container installs requirements.txt and Streamlit; existing version constraints still apply.")
    elif os.path.lexists(root / "pyproject.toml"):
        path = _plain_file(root, "pyproject.toml")
        try:
            manifest = tomllib.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError("Fix the project's pyproject.toml before preparing deployment files.") from exc
        project = manifest.get("project", {})
        declared = project.get("dependencies", []) if isinstance(project, dict) else []
        if not isinstance(declared, list) or any(
            not isinstance(item, str) or not item.strip()
            or any(ord(character) < 32 for character in item)
            or item.lstrip().startswith(("-", "#"))
            for item in declared
        ):
            raise ValueError("pyproject.toml project.dependencies must contain one dependency per string.")
        dependencies = [*declared, "streamlit"]
        notes.append(
            "requirements-vercel.txt copies the declared project.dependencies from pyproject.toml. "
            "Add any optional, dynamic, or tool-specific dependencies your app needs before deployment."
        )
    else:
        dependencies = ["streamlit"]
        notes.append(
            "No requirements.txt or pyproject.toml was found. Add the app's other Python dependencies "
            "to requirements-vercel.txt before deployment."
        )
    return (
        "# Reviewed deployment dependencies. Update this file when project dependencies change.\n"
        + "\n".join(dependencies) + "\n",
        notes,
    )


def prepare_streamlit_container(project_path: Path, entry_point: str) -> StreamlitContainerPlan:
    """Return complete file contents for review without changing the project."""

    root = _project_root(project_path)
    entry = _entry_point(root, entry_point)
    _check_conflicts(root)
    requirements, dependency_notes = _requirements(root)
    launcher = '''"""Run the reviewed Streamlit entry point on Vercel's assigned HTTP port."""
from __future__ import annotations

import os
import sys
from pathlib import Path


ENTRY_POINT = __ENTRY_POINT__


def main() -> None:
    try:
        port = int(os.environ.get("PORT", "80"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("PORT must be an integer between 1 and 65535.") from None
    os.chdir(Path(__file__).resolve().parent)
    # exec lets Streamlit receive the container's shutdown signals directly.
    os.execv(sys.executable, [
        sys.executable, "-m", "streamlit", "run", ENTRY_POINT,
        "--server.address=0.0.0.0",
        f"--server.port={port}",
        "--server.headless=true",
        "--server.fileWatcherType=none",
        "--server.runOnSave=false",
        "--browser.gatherUsageStats=false",
    ])


if __name__ == "__main__":
    main()
'''.replace("__ENTRY_POINT__", repr(entry))
    dockerfile = '''# Vercel detects Dockerfile.vercel in the selected deployment root.
# Review this Python version and any required system packages for your project.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY . .
RUN python -m pip install -r requirements-vercel.txt

EXPOSE 80
CMD ["python", "studio_vercel_start.py"]
'''
    dockerignore = '''# Keep local state, credentials, caches and virtual environments out of the image.
**/.git
**/.hg
**/.svn
**/.venv
**/venv
**/env
**/node_modules
**/__pycache__
**/*.pyc
**/*.pyo
**/.pytest_cache
**/.mypy_cache
**/.ruff_cache
**/.tox
**/.nox
**/.idea
**/.vscode
**/.vercel
**/.env
**/.env.*
**/.streamlit/secrets.toml
**/secrets.toml
**/secrets.json
**/secrets.yaml
**/secrets.yml
**/credentials.json
**/.aws
**/.ssh
**/.azure
**/.gcloud
**/.netrc
**/.npmrc
**/.pypirc
**/*.pem
**/*.key
**/*.p12
**/*.pfx
**/*.log
**/.studio/project.json
**/.studio/draft.json
**/.studio/published.json.tmp
# .studio/published.json and defaults.json remain available to the app runtime.
'''
    files = dict(zip(_GENERATED_NAMES, (dockerfile, dockerignore, requirements, launcher)))
    notes = [
        "Vercel Container Runtime and WebSockets are beta features. Deploy and check your app's logs and interactions.",
        "Connections can reconnect or instances can restart, which may reset an in-memory Streamlit session.",
        "The image uses Python 3.12. Review the Python version, Linux system packages and dependencies your project needs.",
        *dependency_notes,
        "Set private values through the hosting service's environment/secrets settings. Local secrets are excluded from the image.",
    ]
    return StreamlitContainerPlan(root, entry, files, notes)


def install_streamlit_container(project_path: Path, plan: StreamlitContainerPlan) -> list[Path]:
    """Create only the reviewed bundle, refusing conflicts and rolling back a failed write."""

    root = _project_root(project_path)
    if not isinstance(plan, StreamlitContainerPlan) or root != plan.project_path:
        raise ValueError("The reviewed deployment files belong to a different project. Prepare them again.")
    _entry_point(root, plan.entry_point)
    _check_conflicts(root)
    if set(plan.files) != set(_GENERATED_NAMES) or any(
        not isinstance(content, str) or "\x00" in content for content in plan.files.values()
    ):
        raise ValueError("The deployment file plan is invalid. Prepare it again.")
    created: list[tuple[Path, int, int]] = []
    try:
        for name in _GENERATED_NAMES:
            _check_conflicts(root, created_names={path.name for path, _device, _inode in created})
            path = root / name
            data = plan.files[name].encode("utf-8")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o644)
            metadata = os.fstat(descriptor)
            created.append((path, metadata.st_dev, metadata.st_ino))
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        _check_conflicts(root, created_names={path.name for path, _device, _inode in created})
        return [path for path, _device, _inode in created]
    except BaseException:
        for path, device, inode in reversed(created):
            try:
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == (device, inode):
                    path.unlink()
            except OSError:
                pass
        raise
