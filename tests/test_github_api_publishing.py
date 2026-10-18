"""Direct publishing's integrity, failure, and no-external-software contract."""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from studio import publishing
from studio.github_connection import GitHubError
from studio.publishing import (
    CommandRunner, PublishCancelled, PublishError, PublishFile,
    prepare_github_publish, publish_to_github, scan_project,
)
from github_api_fixture import FakeGitHub, blob_sha


def write(root, path="app.py", data=b"print('reviewed')\n", *, executable=False):
    source = root / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)
    source.chmod(0o755 if executable else 0o644)
    return source


def prepare(root, api, *, new=False, files=None, **kwargs):
    return prepare_github_publish(root, scan_project(root).files if files is None else files, api.repository, new_repository=new, client=api, **kwargs)


def mutations(api):
    return [(method, path, data) for method, path, data in api.calls if method != "GET"]


@pytest.fixture
def existing(tmp_path):
    write(tmp_path)
    api = FakeGitHub()
    api.seed({"app.py": b"print('previous')\n", "keep.txt": b"GitHub-only file\n"})
    return api


@pytest.mark.parametrize("new", [False, True])
def test_complete_flow_with_empty_path_and_all_native_processes_forbidden(tmp_path, monkeypatch, new):
    write(tmp_path)
    write(tmp_path, ".gitignore", b"ignored.txt\n")
    write(tmp_path, "ignored.txt", b"local only")
    api = FakeGitHub()
    if not new:
        api.seed({"app.py": b"before\n", "keep.txt": b"retain\n"})
    monkeypatch.setenv("PATH", "")

    def no_process(*_args, **_kwargs):
        raise AssertionError("No external software may be launched by publishing")

    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(CommandRunner, "run", no_process)
    plan = prepare(tmp_path, api, new=new)
    try:
        assert not mutations(api)
        assert "ignored.txt" not in {item.path for item in plan.files}
        result = publish_to_github(plan, client=api)
        assert api.tree_for(result.branch)["app.py"][0] == b"print('reviewed')\n"
        assert len(result.revision) == 40 and len(plan.revision) == 64
        if not new:
            assert api.tree_for(result.branch)["keep.txt"][0] == b"retain\n"
    finally:
        plan.close()


def test_review_fetches_only_changed_selected_blobs_and_never_unselected_contents(tmp_path, existing):
    write(tmp_path, "same.txt", b"same\n")
    write(tmp_path, "new.bin", b"\x00\xff\x10")
    api = existing
    api.seed({"app.py": b"before\n", "same.txt": b"same\n", "secret-but-unselected.txt": b"not downloaded"})
    plan = prepare(tmp_path, api)
    try:
        blob_reads = [path.rsplit("/", 1)[-1] for method, path, _ in api.calls if method == "GET" and "/git/blobs/" in path]
        assert blob_reads == [blob_sha(b"before\n")]
        assert "-before" in plan.diff and "+print('reviewed')" in plan.diff
        assert "Binary, large, or non-UTF-8" in plan.diff
        assert hashlib.sha256(b"\x00\xff\x10").hexdigest() in plan.diff
        assert len(plan.changes) == 2
        assert "1 selected file(s) already match" in plan.change_summary
        assert not mutations(api)
    finally:
        plan.close()


def test_existing_empty_repository_initializes_reviewed_file_and_keeps_default_seed(tmp_path):
    write(tmp_path, "a.txt", b"a")
    write(tmp_path, "nested/script.py", b"print('ready')\n")
    api = FakeGitHub(existing=True)
    api.default_branch = "trunk"
    plan = prepare(tmp_path, api)
    try:
        assert plan.empty_repository and plan.default_branch == "trunk"
        assert "initialize trunk with the reviewed file a.txt" in plan.change_summary
        assert not mutations(api)
        result = publish_to_github(plan, client=api)
        assert api.tree_for("trunk") == {"a.txt": (b"a", "100644")}
        assert api.tree_for(result.branch) == {"a.txt": (b"a", "100644"), "nested/script.py": (b"print('ready')\n", "100644")}
        assert not any(method == "PATCH" for method, _path, _data in mutations(api))
        assert api.commits[result.revision]["parents"] == [{"sha": api.branches["trunk"]}]
    finally:
        plan.close()


def test_empty_repo_initialization_never_inserts_placeholder_files(tmp_path):
    write(tmp_path, "folder/only file.py", b"pass\n")
    api = FakeGitHub()
    plan = prepare(tmp_path, api, new=True)
    try:
        result = publish_to_github(plan, client=api)
        assert set(api.tree_for(result.branch)) == {"folder/only file.py"}
        seed_calls = [data for method, path, data in mutations(api) if method == "PUT" and "/contents/" in path]
        assert len(seed_calls) == 1
        assert seed_calls[0]["branch"] == "main"
        assert all(set(api.trees[commit["tree"]["sha"]]["tree"][index]) for commit in api.commits.values() for index in range(len(api.trees[commit["tree"]["sha"]]["tree"])))
        assert set(api.blobs.values()) == {b"pass\n"}
    finally:
        plan.close()


def test_new_repository_with_different_account_default_stops_before_upload(tmp_path):
    write(tmp_path)
    api = FakeGitHub()
    api.creation_default_branch = "master"
    plan = prepare(tmp_path, api, new=True)
    try:
        with pytest.raises(PublishError, match="different default branch"):
            publish_to_github(plan, client=api)
        assert api.exists and not api.branches
        assert [method for method, _path, _data in mutations(api)] == ["POST"]
    finally:
        plan.close()


def test_organization_creation_uses_org_endpoint_and_reviewed_owner(tmp_path):
    write(tmp_path)
    api = FakeGitHub(repository="example-org/project")
    api.owner_id, api.owner_type = 999, "Organization"
    api.users["example-org"] = {"id": 999, "login": "example-org", "type": "Organization"}
    plan = prepare(tmp_path, api, new=True, visibility="public")
    try:
        assert not mutations(api)
        publish_to_github(plan, client=api)
        assert any(path == "/orgs/example-org/repos" and data["private"] is False for method, path, data in mutations(api) if method == "POST")
        assert not any(path == "/user/repos" for _method, path, _data in api.calls)
    finally:
        plan.close()


def test_cannot_create_repository_for_a_different_person(tmp_path):
    write(tmp_path)
    api = FakeGitHub(repository="another-user/project")
    api.users["another-user"] = {"id": 999, "login": "another-user", "type": "User"}
    with pytest.raises(PublishError, match="Another user's account"):
        prepare(tmp_path, api, new=True)
    assert not mutations(api)


@pytest.mark.parametrize("change,match", [
    (lambda api: setattr(api, "private", False), "visibility changed"),
    (lambda api: setattr(api, "repository_id", 777), "destination changed"),
    (lambda api: setattr(api, "default_branch", "different"), "default branch changed"),
    (lambda api: setattr(api, "account_id", 888), "account changed"),
    (lambda api: api.seed({"app.py": b"changed on GitHub\n"}), "default branch changed"),
])
def test_rechecks_all_reviewed_remote_identity_fields_before_mutation(tmp_path, existing, change, match):
    plan = prepare(tmp_path, existing)
    try:
        change(existing)
        with pytest.raises(PublishError, match=match):
            publish_to_github(plan, client=existing)
        assert not mutations(existing)
    finally:
        plan.close()


def test_head_change_during_blob_upload_prevents_final_reference_creation(tmp_path, existing):
    plan = prepare(tmp_path, existing)
    moved = []

    def move_head(api, method, path, _data):
        if method == "POST" and path.endswith("/git/blobs") and not moved:
            moved.append(api.seed({"app.py": b"new GitHub head\n", "new.txt": b"new work"}))

    existing.before_request = move_head
    try:
        with pytest.raises(PublishError, match="default branch changed"):
            publish_to_github(plan, client=existing)
        assert existing.branches == {"main": moved[0]}
        assert not any(path.endswith("/git/refs") for method, path, _data in mutations(existing))
    finally:
        plan.close()


def test_colliding_update_branch_stops_before_any_write(tmp_path, existing):
    plan = prepare(tmp_path, existing)
    try:
        existing.branches[plan.branch] = existing.branches["main"]
        with pytest.raises(PublishError, match="branch already exists"):
            publish_to_github(plan, client=existing)
        assert not mutations(existing)
    finally:
        plan.close()


def test_destination_becomes_existing_between_review_and_publish(tmp_path):
    write(tmp_path)
    api = FakeGitHub()
    plan = prepare(tmp_path, api, new=True)
    try:
        initial = api.seed({"someone-elses-file.txt": b"retain"})
        with pytest.raises(PublishError, match="already exists"):
            publish_to_github(plan, client=api)
        assert api.branches == {"main": initial}
        assert not mutations(api)
    finally:
        plan.close()


@pytest.mark.parametrize("status,match", [(401, "token is invalid"), (403, "Contents: Read and write"), (404, "token access")])
def test_read_errors_explain_connection_or_repository_permissions_without_secrets(tmp_path, existing, status, match):
    existing.fail_for[("GET", "/repos/clarence/project")] = GitHubError("server included ghp_never-log-this-secret", status=status)
    with pytest.raises(PublishError, match=match) as error:
        prepare(tmp_path, existing)
    assert "ghp_" not in str(error.value)
    assert not mutations(existing)


def test_workflow_permission_failure_keeps_default_and_reports_required_permission(tmp_path):
    write(tmp_path, ".github/workflows/check.yml", b"name: Check\non: push\n")
    api = FakeGitHub()
    original = api.seed({"keep.txt": b"retained"})
    plan = prepare(tmp_path, api)
    api.fail_for[("POST", "/repos/clarence/project/git/refs")] = GitHubError("Forbidden", status=403)
    try:
        with pytest.raises(PublishError, match="Workflows: Read and write"):
            publish_to_github(plan, client=api)
        assert api.branches == {"main": original}
    finally:
        plan.close()


def test_repository_creation_permission_failure_is_actionable(tmp_path):
    write(tmp_path)
    api = FakeGitHub()
    plan = prepare(tmp_path, api, new=True)
    api.fail_for[("POST", "/user/repos")] = GitHubError("Forbidden", status=403)
    try:
        with pytest.raises(PublishError, match="Repository creation: Write"):
            publish_to_github(plan, client=api)
        assert not api.exists
    finally:
        plan.close()


def test_denied_repo_write_access_is_caught_in_review(tmp_path, existing):
    existing.permissions = {"push": False}
    with pytest.raises(PublishError, match="account cannot write"):
        prepare(tmp_path, existing)
    assert not mutations(existing)


def test_truncated_tree_cannot_be_reviewed(tmp_path, existing):
    existing.truncated = True
    with pytest.raises(PublishError, match="tree was truncated"):
        prepare(tmp_path, existing)
    assert not mutations(existing)


@pytest.mark.parametrize("path,remote_mode", [("app.py", "120000"), ("directory", "100644"), ("app.py/inside.py", "100644")])
def test_remote_path_conflicts_are_rejected(tmp_path, path, remote_mode):
    write(tmp_path, path)
    api = FakeGitHub()
    if path == "directory":
        api.seed({"directory/keep.txt": b"keep this"})
    elif path == "app.py/inside.py":
        api.seed({"app.py": (b"link target", "120000")})
    else:
        api.seed({"app.py": (b"link target", remote_mode)})
    with pytest.raises(PublishError, match="symbolic link|directory|folder"):
        prepare(tmp_path, api)
    assert not mutations(api)


def test_remote_submodule_ancestor_is_rejected(tmp_path):
    write(tmp_path, "vendor/app.py")
    api = FakeGitHub(existing=True)
    tree = api.put_tree({"vendor": {"type": "commit", "mode": "160000", "sha": "f" * 40}})
    api.branches["main"] = api.put_commit(tree)
    with pytest.raises(PublishError, match="submodule"):
        prepare(tmp_path, api)
    assert not mutations(api)


def test_untouched_remote_symlink_and_submodule_are_preserved(tmp_path):
    write(tmp_path)
    api = FakeGitHub(existing=True)
    link_sha = api.put_blob(b"somewhere")
    tree = api.put_tree({"link": {"type": "blob", "mode": "120000", "sha": link_sha, "size": 9}, "vendor": {"type": "commit", "mode": "160000", "sha": "f" * 40}})
    original = api.put_commit(tree)
    api.branches["main"] = original
    plan = prepare(tmp_path, api)
    try:
        result = publish_to_github(plan, client=api)
        assert api.tree_for(result.branch)["link"] == (b"somewhere", "120000")
        new_tree = api.trees[api.commits[result.revision]["tree"]["sha"]]
        assert any(entry["path"] == "vendor" and entry["mode"] == "160000" and entry["sha"] == "f" * 40 for entry in new_tree["tree"])
        assert api.branches["main"] == original
    finally:
        plan.close()


def test_unix_executable_mode_only_change_reuses_existing_blob(tmp_path):
    write(tmp_path, data=b"echo hello\n", executable=True)
    api = FakeGitHub()
    api.seed({"app.py": (b"echo hello\n", "100644")})
    plan = prepare(tmp_path, api)
    try:
        assert "old mode 100644\nnew mode 100755" in plan.diff
        result = publish_to_github(plan, client=api)
        assert api.tree_for(result.branch)["app.py"][1] == "100755"
        assert not any(path.endswith("/git/blobs") for method, path, _data in mutations(api))
    finally:
        plan.close()


def test_windows_preserves_existing_unix_executable_mode(tmp_path, monkeypatch):
    write(tmp_path, data=b"echo updated\n")
    api = FakeGitHub()
    api.seed({"app.py": (b"echo hello\n", "100755")})
    # Replace only this module's os facade; globally patching os.name would make
    # pathlib attempt to construct unsupported WindowsPath instances on Linux.
    monkeypatch.setattr(publishing, "os", SimpleNamespace(**{**os.__dict__, "name": "nt"}))
    plan = prepare(tmp_path, api)
    try:
        assert plan.files[0].mode == "100644"
        assert plan.changes[0].mode == "100755"
        assert "new mode 100644" not in plan.diff
        result = publish_to_github(plan, client=api)
        assert api.tree_for(result.branch)["app.py"][1] == "100755"
    finally:
        plan.close()


@pytest.mark.parametrize("when", ["after_scan", "after_review"])
def test_source_and_snapshot_mode_changes_are_detected(tmp_path, when):
    source = write(tmp_path)
    api = FakeGitHub()
    files = scan_project(tmp_path).files
    if when == "after_scan":
        source.chmod(0o755)
        with pytest.raises(PublishError, match="changed since"):
            prepare(tmp_path, api, new=True, files=files)
    else:
        plan = prepare(tmp_path, api, new=True, files=files)
        try:
            (plan.working_path / "app.py").chmod(0o755)
            with pytest.raises(PublishError, match="snapshot changed"):
                publish_to_github(plan, client=api)
        finally:
            plan.close()
    assert not mutations(api)


@pytest.mark.parametrize("target", ["manifest", "extra_file", "diff", "message", "expected_tree"])
def test_snapshot_manifest_covers_review_text_and_all_payload_fields(tmp_path, target):
    write(tmp_path)
    api = FakeGitHub()
    plan = prepare(tmp_path, api, new=True)
    try:
        changed = plan
        if target == "manifest":
            (plan.working_path.parent / "review.json").write_text("{}")
        elif target == "extra_file":
            write(plan.working_path, "unexpected.txt", b"unreviewed")
        elif target == "expected_tree":
            changed = replace(plan, expected_tree="f" * 40)
        else:
            changed = replace(plan, **{target: "Different reviewed content"})
        with pytest.raises(PublishError, match="snapshot changed"):
            publish_to_github(changed, client=api)
        assert not mutations(api)
    finally:
        plan.close()


def test_changed_snapshot_during_upload_stops_before_creating_branch(tmp_path, existing):
    plan = prepare(tmp_path, existing)

    def tamper(_api, method, path, _data):
        if method == "POST" and path.endswith("/git/blobs"):
            write(plan.working_path, data=b"after confirmation")

    existing.before_request = tamper
    try:
        with pytest.raises(PublishError, match="snapshot changed"):
            publish_to_github(plan, client=existing)
        assert plan.branch not in existing.branches
    finally:
        plan.close()


@pytest.mark.parametrize("location", ["source", "remote", "local_attributes", "remote_attributes"])
def test_lfs_inputs_are_never_silently_uploaded_as_ordinary_git_files(tmp_path, location):
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 5000000\n"
    write(tmp_path, data=pointer if location == "source" else b"updated asset")
    api = FakeGitHub()
    api.seed({"app.py": pointer if location == "remote" else b"previous asset", **({".gitattributes": b"*.py filter=lfs diff=lfs merge=lfs -text\n"} if location == "remote_attributes" else {})})
    if location == "local_attributes":
        write(tmp_path, ".gitattributes", b"*.py filter=lfs diff=lfs merge=lfs -text\n")
    with pytest.raises(PublishError, match="LFS"):
        prepare(tmp_path, api)
    assert not mutations(api)


def test_tree_payload_cannot_drop_unselected_paths_or_change_reviewed_bytes(tmp_path, existing, monkeypatch):
    plan = prepare(tmp_path, existing)
    request = existing.request

    def substitute_tree(method, path, data=None):
        response = request(method, path, data)
        if method == "POST" and path.endswith("/git/trees"):
            response["sha"] = "f" * 40
        return response

    monkeypatch.setattr(existing, "request", substitute_tree)
    try:
        with pytest.raises(PublishError, match="exact reviewed snapshot and preserved files"):
            publish_to_github(plan, client=existing)
        assert plan.branch not in existing.branches
        assert not any(path.endswith("/git/commits") for _method, path, _data in mutations(existing))
    finally:
        plan.close()


def test_malformed_blob_response_stops_review_before_writes(tmp_path, existing, monkeypatch):
    request = existing.request

    def corrupt_blob(method, path, data=None):
        response = request(method, path, data)
        if "/git/blobs/" in path:
            response["content"] = "AAAA"
        return response

    monkeypatch.setattr(existing, "request", corrupt_blob)
    with pytest.raises(PublishError, match="did not match"):
        prepare(tmp_path, existing)
    assert not mutations(existing)


def test_cancel_before_review_makes_no_requests(tmp_path):
    write(tmp_path)
    api = FakeGitHub()
    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(PublishCancelled):
        prepare(tmp_path, api, new=True, runner=CommandRunner(cancelled=cancellation))
    assert api.calls == []


def test_cancel_after_upload_never_publishes_ref_and_retains_remote_default(tmp_path, existing):
    cancellation = threading.Event()
    runner = CommandRunner(cancelled=cancellation)
    original = existing.branches["main"]
    plan = prepare(tmp_path, existing, runner=runner)

    def cancel_after_blob(_api, method, path, _data):
        if method == "POST" and path.endswith("/git/blobs"):
            cancellation.set()

    existing.before_request = cancel_after_blob
    try:
        with pytest.raises(PublishCancelled, match="Uploaded Git objects may remain"):
            publish_to_github(plan, runner, client=existing)
        assert existing.branches == {"main": original}
    finally:
        plan.close()


def test_interrupted_initialization_reports_seed_without_claiming_default_unchanged(tmp_path):
    write(tmp_path, "seed.txt", b"seed")
    write(tmp_path, "other.txt", b"the other selected file")
    api = FakeGitHub(existing=True)
    plan = prepare(tmp_path, api)
    api.fail_for[("POST", "/repos/clarence/project/git/trees")] = GitHubError("Network lost")
    try:
        with pytest.raises(PublishError, match="initial file commit may remain") as error:
            publish_to_github(plan, client=api)
        assert "did not change an existing default branch" not in str(error.value)
        assert api.tree_for("main") == {"seed.txt": (b"seed", "100644")}
        assert plan.branch not in api.branches
    finally:
        plan.close()


@pytest.mark.parametrize("item", [
    PublishFile("a.py", -1, "a" * 64),
    PublishFile("a.py", 1, "invalid"),
    PublishFile("a.py", 1, "a" * 64, mode="120000"),
    PublishFile("a.py", publishing.MAX_FILE_BYTES + 1, "a" * 64),
])
def test_invalid_or_oversized_selection_is_rejected_before_account_access(tmp_path, item):
    api = FakeGitHub()
    with pytest.raises(PublishError, match="file list is invalid"):
        prepare(tmp_path, api, new=True, files=[item])
    assert api.calls == []


def test_bounded_text_preview_keeps_every_exact_file_hash_in_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(publishing, "MAX_DIFF_BYTES", 700)
    for index in range(4):
        write(tmp_path, f"file{index}.txt", (f"line in {index}\n" * 50).encode())
    api = FakeGitHub()
    plan = prepare(tmp_path, api, new=True)
    try:
        assert "preview" in plan.diff and "limited" in plan.diff
        for item in plan.files:
            assert item.path in plan.change_summary and item.sha256 in plan.change_summary
        assert not mutations(api)
    finally:
        plan.close()
