"""Review/install boundaries and local startup of the generated container files."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from studio.vercel_container import install_streamlit_container, prepare_streamlit_container


GENERATED_FILES = {
    "Dockerfile.vercel", ".dockerignore", "requirements-vercel.txt", "studio_vercel_start.py",
}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("import streamlit as st\nst.title('Container fixture')\n", encoding="utf-8")
    return root


def snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def launcher_namespace(project, plan):
    namespace = {"__name__": "generated_launcher_test", "__file__": str(project / "studio_vercel_start.py")}
    exec(compile(plan.files["studio_vercel_start.py"], namespace["__file__"], "exec"), namespace)
    return namespace


def test_prepare_only_reads_project_and_install_writes_exact_reviewed_bundle(project):
    (project / "requirements.txt").write_text("streamlit==1.66.0\nrequests>=2.31\n", encoding="utf-8")
    (project / "assets").mkdir()
    (project / "assets" / "picture.svg").write_text("<svg/>", encoding="utf-8")
    (project / ".env").write_text("FIXTURE_VALUE=local-only\n", encoding="utf-8")
    before = snapshot(project)
    plan = prepare_streamlit_container(project, "app.py")
    assert snapshot(project) == before
    assert set(plan.files) == GENERATED_FILES
    assert plan.project_path == project.resolve()
    assert "-r requirements.txt\nstreamlit\n" in plan.files["requirements-vercel.txt"]
    installed = install_streamlit_container(project, plan)
    assert {path.name for path in installed} == GENERATED_FILES
    for name, contents in plan.files.items():
        assert (project / name).read_bytes() == contents.encode("utf-8")
    after = snapshot(project)
    assert {name: after[name] for name in before} == before
    ignored = set(plan.files[".dockerignore"].splitlines())
    assert {"**/.env", "**/.env.*", "**/.streamlit/secrets.toml", "**/.git", "**/.venv"} <= ignored
    assert "**/.studio/draft.json" in ignored
    assert "**/.studio/published.json" not in ignored
    assert "**/.studio/defaults.json" not in ignored
    assert "enableCORS" not in plan.files["studio_vercel_start.py"]
    assert "enableXsrfProtection" not in plan.files["studio_vercel_start.py"]


@pytest.mark.parametrize("name", [
    *sorted(GENERATED_FILES), "Containerfile.vercel", "vercel.json", "Dockerfile", "Containerfile",
    "Dockerfile.vercel.dockerignore", "Containerfile.vercel.dockerignore",
])
def test_existing_deployment_files_are_never_replaced(project, name):
    (project / name).write_text("Existing reviewed content", encoding="utf-8")
    before = snapshot(project)
    with pytest.raises(ValueError, match="Existing deployment files were left unchanged"):
        prepare_streamlit_container(project, "app.py")
    assert snapshot(project) == before


def test_deployment_directory_and_dangling_symlink_are_conflicts(project, tmp_path):
    (project / "Dockerfile.vercel").mkdir()
    with pytest.raises(ValueError, match="Existing deployment"):
        prepare_streamlit_container(project, "app.py")
    (project / "Dockerfile.vercel").rmdir()
    (project / "vercel.json").symlink_to(tmp_path / "missing-target")
    with pytest.raises(ValueError, match="Existing deployment"):
        prepare_streamlit_container(project, "app.py")
    assert (project / "vercel.json").is_symlink()


@pytest.mark.parametrize("entry", [
    "../app.py", "/app.py", "src/../app.py", "./app.py", "src//app.py", "C:\\app.py",
    "app.py\n--server.port=9000", "app\x00.py", "app\t.py", "app.txt", "", None,
])
def test_entry_rejects_traversal_absolute_paths_controls_and_non_python(project, entry):
    before = snapshot(project)
    with pytest.raises(ValueError):
        prepare_streamlit_container(project, entry)
    assert snapshot(project) == before


def test_leading_dash_is_rejected_before_streamlit_can_parse_it_as_an_option(project):
    (project / "-example.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot start with '-' ".strip()):
        prepare_streamlit_container(project, "-example.py")


@pytest.mark.parametrize("entry", ["app_🐍.py", "app';$(echo marker).py", "name with spaces.py"])
def test_unusual_entry_names_are_exact_python_string_data_not_injected_code(project, entry, monkeypatch):
    (project / entry).write_text("pass\n", encoding="utf-8")
    plan = prepare_streamlit_container(project, entry)
    namespace = launcher_namespace(project, plan)
    assert namespace["ENTRY_POINT"] == entry
    calls = []
    monkeypatch.setenv("PORT", "8507")
    monkeypatch.setattr(os, "chdir", lambda path: calls.append(("cwd", path)))
    monkeypatch.setattr(os, "execv", lambda executable, argv: calls.append(("exec", executable, argv)))
    namespace["main"]()
    assert calls[0] == ("cwd", project.resolve())
    assert calls[1][0:2] == ("exec", sys.executable)
    assert calls[1][2][4] == entry
    assert len(calls[1][2]) == 11
    assert calls[1][2][6] == "--server.port=8507"


def test_windows_path_separator_normalizes_to_safe_nested_entry(project):
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    plan = prepare_streamlit_container(project, "src\\main.py")
    assert plan.entry_point == "src/main.py"
    assert launcher_namespace(project, plan)["ENTRY_POINT"] == "src/main.py"


def test_entry_and_requirements_symlinks_are_rejected(project, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    (project / "linked.py").symlink_to(outside)
    with pytest.raises(ValueError, match="without symbolic links"):
        prepare_streamlit_container(project, "linked.py")
    (project / "requirements.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="without symbolic links"):
        prepare_streamlit_container(project, "app.py")


def test_project_folder_symlink_is_rejected(project, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    with pytest.raises(ValueError, match="actual project folder"):
        prepare_streamlit_container(alias, "app.py")


def test_pyproject_declared_dependencies_are_preserved_without_executing_project(project):
    (project / "pyproject.toml").write_text(
        "[project]\nname='fixture'\ndependencies=[\"requests>=2.31\", \"pandas>=2; python_version >= '3.10'\"]\n",
        encoding="utf-8",
    )
    before = snapshot(project)
    plan = prepare_streamlit_container(project, "app.py")
    assert "requests>=2.31\npandas>=2; python_version >= '3.10'\nstreamlit\n" in plan.files["requirements-vercel.txt"]
    assert any("optional, dynamic, or tool-specific" in note for note in plan.notes)
    assert snapshot(project) == before


@pytest.mark.parametrize("manifest", [
    "[project\n", "[project]\ndependencies='requests'\n", "[project]\ndependencies=[1]\n",
    "[project]\ndependencies=['']\n", "[project]\ndependencies=['--index-url https://example.invalid']\n",
    '[project]\ndependencies=["requests\\n--extra-index-url=anything"]\n',
])
def test_invalid_or_multiline_manifest_dependencies_are_rejected(project, manifest):
    (project / "pyproject.toml").write_text(manifest, encoding="utf-8")
    before = snapshot(project)
    with pytest.raises(ValueError):
        prepare_streamlit_container(project, "app.py")
    assert snapshot(project) == before


def test_missing_dependencies_get_streamlit_and_a_review_note(project):
    plan = prepare_streamlit_container(project, "app.py")
    assert plan.files["requirements-vercel.txt"].splitlines()[-1] == "streamlit"
    assert any("Add the app's other Python dependencies" in note for note in plan.notes)


def test_install_rechecks_project_entry_and_all_conflicts_after_review(project, tmp_path):
    plan = prepare_streamlit_container(project, "app.py")
    other = tmp_path / "other"
    other.mkdir()
    (other / "app.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="different project"):
        install_streamlit_container(other, plan)
    (project / "vercel.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Existing deployment"):
        install_streamlit_container(project, plan)
    assert not any((project / name).exists() for name in GENERATED_FILES)
    (project / "vercel.json").unlink()
    (project / "app.py").unlink()
    (project / "app.py").symlink_to(other / "app.py")
    with pytest.raises(ValueError, match="without symbolic links"):
        install_streamlit_container(project, plan)
    assert not any((project / name).exists() for name in GENERATED_FILES)


def test_invalid_plan_cannot_add_unreviewed_paths(project):
    plan = prepare_streamlit_container(project, "app.py")
    invalid = replace(plan, files={**plan.files, "../outside.py": "pass"})
    with pytest.raises(ValueError, match="plan is invalid"):
        install_streamlit_container(project, invalid)
    invalid = replace(plan, files={**plan.files, "studio_vercel_start.py": "invalid\x00data"})
    with pytest.raises(ValueError, match="plan is invalid"):
        install_streamlit_container(project, invalid)
    assert not any((project / name).exists() for name in GENERATED_FILES)


def test_late_creation_of_generated_file_preserves_it_and_rolls_back_ours(project, monkeypatch):
    plan = prepare_streamlit_container(project, "app.py")
    real_open = os.open

    def conflicting_open(path, flags, *args, **kwargs):
        if Path(path).name == ".dockerignore":
            Path(path).write_text("Concurrent user's ignore file", encoding="utf-8")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", conflicting_open)
    with pytest.raises(FileExistsError):
        install_streamlit_container(project, plan)
    assert (project / ".dockerignore").read_text(encoding="utf-8") == "Concurrent user's ignore file"
    assert not any((project / name).exists() for name in GENERATED_FILES - {".dockerignore"})
    assert (project / "app.py").is_file()


def test_partial_write_failure_rolls_back_only_new_files(project, monkeypatch):
    plan = prepare_streamlit_container(project, "app.py")
    before = snapshot(project)
    calls = 0
    real_fsync = os.fsync

    def fail_second_write(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Disk write failed")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_second_write)
    with pytest.raises(OSError, match="Disk write failed"):
        install_streamlit_container(project, plan)
    assert snapshot(project) == before


@pytest.mark.parametrize("write_number", [1, 4])
def test_late_non_generated_conflict_rolls_back_the_reviewed_bundle(project, monkeypatch, write_number):
    plan = prepare_streamlit_container(project, "app.py")
    real_fsync = os.fsync
    calls = 0

    def create_conflict_while_writing(descriptor):
        nonlocal calls
        calls += 1
        if calls == write_number:
            (project / "vercel.json").write_text('{"version": 2}', encoding="utf-8")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", create_conflict_while_writing)
    with pytest.raises(ValueError, match="Existing deployment files were left unchanged: vercel.json"):
        install_streamlit_container(project, plan)
    assert json.loads((project / "vercel.json").read_text(encoding="utf-8")) == {"version": 2}
    assert not any((project / name).exists() for name in GENERATED_FILES)
    assert (project / "app.py").is_file()


def test_rollback_keeps_a_concurrent_replacement_file(project, monkeypatch):
    plan = prepare_streamlit_container(project, "app.py")
    replacement = project / "user_replacement.txt"
    replacement.write_text("User replacement must survive", encoding="utf-8")
    calls = 0
    real_fsync = os.fsync

    def fail_after_replacement(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            os.replace(replacement, project / "Dockerfile.vercel")
            raise OSError("Later deployment write failed")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_after_replacement)
    with pytest.raises(OSError, match="Later deployment"):
        install_streamlit_container(project, plan)
    assert (project / "Dockerfile.vercel").read_text(encoding="utf-8") == "User replacement must survive"
    assert not any((project / name).exists() for name in GENERATED_FILES - {"Dockerfile.vercel"})


@pytest.mark.parametrize("port", ["", "0", "-1", "65536", "not a port", "8501;command", "8501.5"])
def test_launcher_rejects_invalid_port_before_changing_directory_or_exec(project, monkeypatch, port):
    namespace = launcher_namespace(project, prepare_streamlit_container(project, "app.py"))
    monkeypatch.setenv("PORT", port)
    monkeypatch.setattr(os, "chdir", lambda path: pytest.fail("Invalid PORT reached chdir"))
    monkeypatch.setattr(os, "execv", lambda *args: pytest.fail("Invalid PORT reached exec"))
    with pytest.raises(SystemExit, match="PORT must be an integer between 1 and 65535"):
        namespace["main"]()


def test_launcher_defaults_to_port_80_and_preserves_streamlit_security_settings(project, monkeypatch):
    namespace = launcher_namespace(project, prepare_streamlit_container(project, "app.py"))
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(os, "chdir", lambda path: None)
    calls = []
    monkeypatch.setattr(os, "execv", lambda *args: calls.append(args))
    namespace["main"]()
    executable, argv = calls[0]
    assert executable == sys.executable == argv[0]
    assert argv[1:5] == ["-m", "streamlit", "run", "app.py"]
    assert "--server.port=80" in argv
    assert "--server.address=0.0.0.0" in argv
    assert not any("enableCORS" in value or "enableXsrfProtection" in value for value in argv)


@pytest.mark.skipif(
    os.environ.get("HPS_RUN_CONTAINER_TESTS") != "1" and os.environ.get("HPS_RUN_WEBENGINE_TESTS") != "1",
    reason="Set HPS_RUN_CONTAINER_TESTS=1 to run a generated launcher against local Streamlit",
)
def test_generated_launcher_starts_real_streamlit_on_assigned_port_and_stops(project):
    pytest.importorskip("streamlit")
    plan = prepare_streamlit_container(project, "app.py")
    install_streamlit_container(project, plan)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = os.environ.copy()
    environment["PORT"] = str(port)
    output = project / "local_launch_output.txt"
    process = None
    with output.open("wb") as log:
        try:
            process = subprocess.Popen(
                [sys.executable, str(project / "studio_vercel_start.py")],
                cwd=project.parent, env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
            response = b""
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=.5) as page:
                        response = page.read()
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(.1)
            assert response == b"ok", output.read_text(encoding="utf-8", errors="replace")
            assert process.poll() is None
            process.terminate()
            process.wait(timeout=10)
            assert process.returncode in {0, -15, 1}
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
