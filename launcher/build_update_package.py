"""Build one complete Studio update ZIP using installed Python.

This maintainer command packages application files only. It never contacts
GitHub, installs dependencies, or bundles user projects and Python environments.
Run it from a flat source checkout or the compact application's app folder.
The ZIP includes its own package manifest; no separate metadata upload is needed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import tomllib
import zipfile


APPLICATION = "hoyo-project-studio"
EXE_NAME = "HoYo Project Studio.exe"
MANIFEST_NAME = "app/package-manifest.json"
ARCHIVE_PREFIX = "hoyo_project_studio/"
PACKAGE_DIRECTORIES = ("studio", "launcher", "resources", "templates", "tests")
ROOT_FILES = (
    "main.py", "studio_runtime.py", "pyproject.toml", "requirements.txt",
    "HoYoProjectStudio.spec", "README.md", "EDITOR_UPGRADE.md", "LAUNCHER.md",
    "PUBLISHING.md", "UPDATING.md", "UPDATE_NOTES.md", "VERIFICATION.md",
    "ADAPTING_YOUR_STREAMLIT_PROJECT.md", "Launch HoYo Project Studio.vbs",
    "Launch HoYo Project Studio.cmd", "Setup HoYo Project Studio.cmd",
    "Create Desktop Shortcut.cmd", "Repair Python Completion.cmd",
    "Build Windows EXE.cmd", "Build Update Package.cmd", ".gitignore",
)
BUNDLED_CONFIGURATION = {
    ".gitignore",
    "templates/streamlit_demo/.studio/defaults.json",
    "templates/streamlit_demo/.studio/draft.json",
    "templates/streamlit_demo/.studio/project.json",
    "templates/streamlit_demo/.studio/published.json",
}
EXCLUDED_DIRECTORIES = {
    "__pycache__", ".pytest_cache", ".venv", "venv", "build", "dist", "qa", "release",
    "node_modules", ".git", ".hg", ".svn",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".o", ".obj", ".deb", ".tmp"}


class PackageError(Exception):
    pass


def package_version(root: Path) -> str:
    """Read version constants without importing or executing application code."""
    module = ast.parse((root / "studio/__init__.py").read_text(encoding="utf-8"))
    values = [
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    ]
    if len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", values[0],
    ):
        raise PackageError("studio/__init__.py must contain one stable version such as 0.4.13.")
    version = values[0]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if metadata.get("project", {}).get("name") != APPLICATION:
        raise PackageError("This folder is not the HoYo Project Studio application.")
    if metadata["project"].get("version") != version:
        raise PackageError("Set the same version in studio/__init__.py and pyproject.toml before building.")
    return version


def collect_files(root: Path) -> dict[str, bytes]:
    """Use application locations, excluding environments and unrelated folders."""
    root = root.resolve()
    exe = (root.parent if root.name.casefold() == "app" else root) / EXE_NAME
    if not exe.is_file() or exe.is_symlink():
        raise PackageError("Keep the supplied HoYo Project Studio.exe beside the app folder.")
    executable = exe.read_bytes()
    if len(executable) < 1024 or executable[:2] != b"MZ":
        raise PackageError("The launcher is not a Windows executable.")
    pe_offset = struct.unpack_from("<I", executable, 0x3C)[0]
    if (
        pe_offset + 26 > len(executable)
        or executable[pe_offset:pe_offset + 4] != b"PE\0\0"
        or struct.unpack_from("<H", executable, pe_offset + 4)[0] != 0x8664
        or struct.unpack_from("<H", executable, pe_offset + 24)[0] != 0x20B
    ):
        raise PackageError("The release needs the supplied Windows x64 launcher.")
    files = {EXE_NAME: executable}

    def include(path: Path) -> None:
        relative = path.relative_to(root)
        if relative.as_posix() not in BUNDLED_CONFIGURATION and any(
            part.startswith(".") or part in EXCLUDED_DIRECTORIES for part in relative.parts
        ):
            return
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            return
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise PackageError(f"Application files must not be symbolic links: {relative}")
        if not path.is_file():
            return
        files["app/" + relative.as_posix()] = path.read_bytes()

    for name in ROOT_FILES:
        path = root / name
        if path.exists():
            include(path)
    for directory in PACKAGE_DIRECTORIES:
        base = root / directory
        if base.is_symlink():
            raise PackageError(f"Application folders must not be symbolic links: {directory}")
        for path in sorted(base.rglob("*")):
            include(path)
    required = {
        "app/main.py", "app/pyproject.toml", "app/studio/__init__.py",
        "app/studio/application.py", "app/studio/main_window.py",
        "app/studio/update_service.py", "app/studio/update_dialog.py", "app/studio/update_install.py",
        "app/launcher/launcher.pyw", "app/launcher/startup.py", "app/launcher/startup.ps1",
        "app/launcher/update_apply.py", "app/resources/hoyo_project_studio.ico",
    }
    missing = required - files.keys()
    if missing:
        raise PackageError("Application files are missing: " + ", ".join(sorted(missing)))
    if sum(map(len, files.values())) > 256 * 1024 * 1024:
        raise PackageError("Application files exceed the compact update package size limit.")
    return files


def package_files(root: Path) -> tuple[str, dict[str, bytes]]:
    version = package_version(root)
    files = collect_files(root)
    manifest = {
        "schema_version": 1, "application": APPLICATION, "version": version,
        "platform": "windows-x64", "layout": "compact-v1",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    }
    files[MANIFEST_NAME] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return version, files


def build_release(
    root: Path, output: Path, *, legacy_metadata: bool = False,
) -> tuple[Path, Path | None]:
    """Write the complete ZIP and, only when requested, legacy release metadata.

    ``output`` remains the exact destination supplied by the caller. Existing
    unrelated files are retained. The command-line default uses a separate
    version folder so earlier releases do not obscure the newly built ZIP.
    """
    version, files = package_files(root)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = f"HoYoProjectStudio-{version}-windows-x64.zip"
    package_path = output / name
    descriptor_path = output / "studio-update.json" if legacy_metadata else None
    # Finish the requested assets in isolation before replacing previous output.
    with tempfile.TemporaryDirectory(prefix=".studio-release-", dir=output) as temporary:
        temporary = Path(temporary)
        staged_zip = temporary / name
        with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, data in sorted(files.items()):
                entry = zipfile.ZipInfo(ARCHIVE_PREFIX + relative, date_time=(2026, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = (0o100644 << 16)
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, data)
        with zipfile.ZipFile(staged_zip) as archive:
            if archive.testzip() is not None:
                raise PackageError("The release ZIP failed its integrity check.")
        staged_descriptor = None
        if descriptor_path is not None:
            descriptor = {
                "schema_version": 1, "application": APPLICATION, "version": version,
                "platform": "windows-x64", "layout": "compact-v1",
                "package": {
                    "name": name, "size": staged_zip.stat().st_size,
                    "sha256": hashlib.sha256(staged_zip.read_bytes()).hexdigest(),
                },
            }
            staged_descriptor = temporary / "studio-update.json"
            staged_descriptor.write_text(json.dumps(descriptor, indent=2) + "\n", encoding="utf-8")
        staged_zip.replace(package_path)
        if staged_descriptor is not None and descriptor_path is not None:
            staged_descriptor.replace(descriptor_path)
    return package_path, descriptor_path


def show_package(package: Path) -> None:
    """Select the completed ZIP in Windows Explorer without invoking a shell."""
    if sys.platform != "win32":
        print(f"Find the update ZIP in: {package.parent}")
        return
    explorer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
    try:
        subprocess.Popen(
            [str(explorer), "/select,", str(package)], shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        # A desktop shell problem must not invalidate a successfully built ZIP.
        print(f"Explorer could not be opened. Find the update ZIP in: {package.parent}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, help="Output folder (default: release/vVERSION).")
    parser.add_argument(
        "--legacy-metadata", action="store_true",
        help="Also generate studio-update.json for Studio 0.4.13 and 0.4.14 clients.",
    )
    parser.add_argument(
        "--show-package", action="store_true",
        help="Select the completed ZIP in Windows Explorer.",
    )
    args = parser.parse_args()
    root = args.project_root.resolve()
    try:
        version = package_version(root)
        output = args.output or root / "release" / f"v{version}"
        package, descriptor = build_release(root, output, legacy_metadata=args.legacy_metadata)
    except (PackageError, OSError, SyntaxError, ValueError, zipfile.BadZipFile) as error:
        print(f"Could not build the Studio update: {error}")
        return 1
    print(f"Update package ready: {package}")
    if descriptor is not None:
        print(f"Legacy update information ready: {descriptor}")
        print(f"Upload both files to the GitHub Release tagged v{version} to support older Studio clients.")
    else:
        print(f"Upload this ZIP to the GitHub Release tagged v{version}.")
    print("Your repository has not been changed.")
    if args.show_package:
        show_package(package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
