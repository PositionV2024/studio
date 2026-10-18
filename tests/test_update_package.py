"""Validate the actual release builder against the actual update archive reader."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import shutil
from unittest.mock import Mock
import zipfile

import pytest

from launcher import build_update_package as builder
from launcher import update_apply


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "Studio source 文件 & space"
    root.mkdir()
    for name in builder.ROOT_FILES:
        if (ROOT / name).is_file():
            shutil.copyfile(ROOT / name, root / name)
    for name in builder.PACKAGE_DIRECTORIES:
        shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    exe = (ROOT.parent if ROOT.name == "app" else ROOT) / builder.EXE_NAME
    shutil.copyfile(exe, root / builder.EXE_NAME)
    return root


def test_single_zip_is_complete_and_installer_accepts_it(source, tmp_path):
    version = builder.package_version(source)
    output = tmp_path / "release"
    package, descriptor = builder.build_release(source, output)
    assert descriptor is None
    assert package.name == f"HoYoProjectStudio-{version}-windows-x64.zip"
    assert list(output.iterdir()) == [package]
    manifest = update_apply.stage_archive(package, tmp_path / "staged", version)
    assert manifest["version"] == version
    assert {name.split("/", 1)[0] for name in manifest["files"]} == {builder.EXE_NAME, "app"}
    assert "app/UPDATING.md" in manifest["files"]
    assert "app/Build Update Package.cmd" in manifest["files"]
    assert {"app/" + path for path in builder.BUNDLED_CONFIGURATION} <= set(manifest["files"])
    assert update_apply.validate_staging(tmp_path / "staged", version) == manifest
    with zipfile.ZipFile(package) as archive:
        assert set(archive.namelist()) == {
            builder.ARCHIVE_PREFIX + name for name in manifest["files"]
        } | {builder.ARCHIVE_PREFIX + builder.MANIFEST_NAME}
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(archive.read(builder.ARCHIVE_PREFIX + name)).hexdigest() == digest


def test_legacy_metadata_is_optional_and_matches_the_same_complete_zip(source, tmp_path):
    package, descriptor = builder.build_release(source, tmp_path / "legacy", legacy_metadata=True)
    assert descriptor is not None
    assert set(package.parent.iterdir()) == {package, descriptor}
    metadata = json.loads(descriptor.read_text())
    assert metadata["version"] == builder.package_version(source)
    assert metadata["package"] == {
        "name": package.name, "size": package.stat().st_size,
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
    }
    single_package, single_descriptor = builder.build_release(source, tmp_path / "single")
    assert single_descriptor is None
    assert single_package.read_bytes() == package.read_bytes()


def test_builder_keeps_unrelated_projects_secrets_environments_and_outputs_out(source):
    private_payload = secrets.token_bytes(64)
    for name in ("projects/example/app.py", ".venv/Scripts/python.exe", ".env", "release/old.zip", "studio/__pycache__/editor.pyc", "launcher/setup.log"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(private_payload)
    _version, files = builder.package_files(source)
    assert not any(private_payload in data for data in files.values())
    assert not any("__pycache__" in name or "/.venv/" in name or "/projects/" in name for name in files)


def test_compact_source_rebuild_keeps_exe_outside_app_and_same_bytes(source, tmp_path):
    compact = tmp_path / "compact"
    compact.mkdir()
    shutil.move(str(source / builder.EXE_NAME), compact / builder.EXE_NAME)
    shutil.move(str(source), compact / "app")
    version, files = builder.package_files(compact / "app")
    assert files[builder.EXE_NAME] == (compact / builder.EXE_NAME).read_bytes()
    assert "app/" + builder.EXE_NAME not in files
    assert version


def test_same_application_build_has_reproducible_archive_bytes(source, tmp_path):
    first, first_meta = builder.build_release(source, tmp_path / "first")
    second, second_meta = builder.build_release(source, tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    assert first_meta is second_meta is None


def test_cli_default_shows_one_zip_in_its_version_folder_and_keeps_old_outputs(
    source, monkeypatch, capsys,
):
    version = builder.package_version(source)
    old_output = source / "release" / "studio-update.json"
    old_output.parent.mkdir()
    old_output.write_text("previous release information")
    old_zip = source / "release" / "v0.0.1" / "previous.zip"
    old_zip.parent.mkdir()
    old_zip.write_bytes(b"previous release")
    show = Mock()
    monkeypatch.setattr(builder, "show_package", show)
    monkeypatch.setattr(builder.sys, "argv", [
        "build_update_package.py", "--project-root", str(source), "--show-package",
    ])
    assert builder.main() == 0
    package = source / "release" / f"v{version}" / f"HoYoProjectStudio-{version}-windows-x64.zip"
    assert list(package.parent.iterdir()) == [package]
    show.assert_called_once_with(package)
    output = capsys.readouterr().out
    assert "Upload this ZIP" in output
    assert f"tagged v{version}" in output
    assert "studio-update.json" not in output
    assert "Upload both" not in output
    assert old_output.read_text() == "previous release information"
    assert old_zip.read_bytes() == b"previous release"


def test_cli_explicit_output_and_legacy_option_preserve_older_client_workflow(
    source, tmp_path, monkeypatch, capsys,
):
    output = tmp_path / "chosen folder 文件"
    monkeypatch.setattr(builder.sys, "argv", [
        "build_update_package.py", "--project-root", str(source),
        "--output", str(output), "--legacy-metadata",
    ])
    assert builder.main() == 0
    files = set(output.iterdir())
    assert len(files) == 2
    assert output / "studio-update.json" in files
    assert "Upload both files" in capsys.readouterr().out


def test_select_package_uses_safe_arguments_for_windows_paths(tmp_path, monkeypatch):
    package = tmp_path / "Studio 文件 & spaces" / "HoYoProjectStudio-1.2.3-windows-x64.zip"
    windows_root = tmp_path / "Windows"
    process = Mock()
    monkeypatch.setattr(builder.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(windows_root))
    monkeypatch.setattr(builder.subprocess, "Popen", process)
    builder.show_package(package)
    assert process.call_args.args[0] == [str(windows_root / "explorer.exe"), "/select,", str(package)]
    assert process.call_args.kwargs["shell"] is False
    assert process.call_args.kwargs["stdin"] == builder.subprocess.DEVNULL


def test_explorer_failure_keeps_completed_package_available(tmp_path, monkeypatch, capsys):
    package = tmp_path / "HoYoProjectStudio-1.2.3-windows-x64.zip"
    package.write_bytes(b"completed package")
    monkeypatch.setattr(builder.sys, "platform", "win32")
    monkeypatch.setattr(builder.subprocess, "Popen", Mock(side_effect=OSError("no desktop shell")))
    builder.show_package(package)
    assert package.read_bytes() == b"completed package"
    assert str(package.parent) in capsys.readouterr().out


def test_mismatched_package_versions_fail_without_replacing_output(source, tmp_path):
    path = source / "pyproject.toml"
    version = builder.package_version(source)
    path.write_text(path.read_text().replace('version = "' + version + '"', 'version = "9.9.9"'))
    output = tmp_path / "release"
    output.mkdir()
    original = output / "studio-update.json"
    original.write_bytes(b"previous-release")
    with pytest.raises(builder.PackageError, match="same version"):
        builder.build_release(source, output)
    assert original.read_bytes() == b"previous-release"
    assert list(output.iterdir()) == [original]


def test_version_is_read_without_executing_application_module(source, tmp_path):
    path = source / "studio/__init__.py"
    version = builder.package_version(source)
    path.write_text(path.read_text() + '\nraise RuntimeError("Do not execute me")\n')
    assert builder.package_version(source) == version


def test_missing_update_helper_is_not_packaged_as_a_valid_update(source, tmp_path):
    (source / "launcher/update_apply.py").unlink()
    with pytest.raises(builder.PackageError, match="missing"):
        builder.build_release(source, tmp_path / "release")


def test_vendor_symlink_cannot_include_files_from_outside_application(source, tmp_path):
    external = tmp_path / "private.txt"
    external.write_text("private")
    try:
        (source / "resources/private.txt").symlink_to(external)
    except OSError:
        pytest.skip("Symlink creation requires a permission unavailable on this Windows host.")
    with pytest.raises(builder.PackageError, match="symbolic"):
        builder.package_files(source)
