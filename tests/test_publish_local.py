"""Publishing metadata must remain usable with no Git/gh on PATH."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from studio import publish_local
from studio.publish_local import ignored_project_paths, read_origin_url


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_native_tools(monkeypatch):
    monkeypatch.setenv("PATH", "")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Local publishing metadata must never start an executable")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.mark.parametrize("patterns, candidates, expected", [
    ("*.cache\n", ["a.cache", "deep/a.cache", "a.py"], {"a.cache", "deep/a.cache"}),
    ("/root.txt\n", ["root.txt", "nested/root.txt"], {"root.txt"}),
    ("src/generated.txt\n", ["src/generated.txt", "other/src/generated.txt"], {"src/generated.txt"}),
    ("cache/\n", ["cache/a.py", "nested/cache/b.py", "other/cache"], {"cache/a.py", "nested/cache/b.py"}),
    ("cache\n", ["cache/a.py", "nested/cache", "cached"], {"cache/a.py", "nested/cache"}),
    ("*.txt\n!keep.txt\n", ["secret.txt", "keep.txt", "sub/keep.txt"], {"secret.txt"}),
    ("*.txt\n!keep.txt\nkeep.txt\n", ["keep.txt", "other.txt"], {"keep.txt", "other.txt"}),
    ("private/\n!private/keep.txt\n", ["private/keep.txt", "visible.txt"], {"private/keep.txt"}),
    ("private/*\n!private/keep.txt\n", ["private/keep.txt", "private/hidden.txt"], {"private/hidden.txt"}),
    ("/*\n!/src\n/src/*\n!/src/keep\n", ["root.txt", "src/a.py", "src/keep/a.py"], {"root.txt", "src/a.py"}),
    ("**/private.txt\n", ["private.txt", "a/private.txt", "a/b/private.txt", "safe.txt"], {"private.txt", "a/private.txt", "a/b/private.txt"}),
    ("a/**/secret.txt\n", ["a/secret.txt", "a/b/secret.txt", "a/b/c/secret.txt", "x/a/secret.txt"], {"a/secret.txt", "a/b/secret.txt", "a/b/c/secret.txt"}),
    ("a/**\n!a/keep.txt\n", ["a/keep.txt", "a/private.txt", "a/b/keep.txt"], {"a/private.txt", "a/b/keep.txt"}),
    ("ab**cd\n", ["abcd", "ab123cd", "ab/cd"], {"abcd", "ab123cd"}),
    ("data?.txt\n", ["data1.txt", "data12.txt", "data/.txt"], {"data1.txt"}),
    ("[a-c][0-3].txt\n", ["a0.txt", "b3.txt", "d0.txt", "a4.txt"], {"a0.txt", "b3.txt"}),
    ("[z-a].txt\n", ["z.txt", "a.txt", "m.txt"], {"z.txt"}),
    ("[!a-c].txt\n", ["a.txt", "d.txt", "1.txt"], {"d.txt", "1.txt"}),
    ("[^a-c].txt\n", ["a.txt", "d.txt"], {"d.txt"}),
    ("[]a-].txt\n", ["].txt", "a.txt", "-.txt", "b.txt"], {"].txt", "a.txt", "-.txt"}),
    ("[[:digit:]][[:alpha:]].txt\n", ["1a.txt", "2Z.txt", "a1.txt"], {"1a.txt", "2Z.txt"}),
    ("[a\\-c].txt\n", ["a.txt", "-.txt", "b.txt", "c.txt"], {"a.txt", "-.txt", "c.txt"}),
    ("# comment\n\\#private\n\\!private\n", ["comment", "#private", "!private"], {"#private", "!private"}),
    ("trailing   \nwith\\ \n", ["trailing", "trailing ", "with ", "with"], {"trailing", "with "}),
    ("\\*.txt\n\\?.txt\n\\[x].txt\n", ["*.txt", "?.txt", "[x].txt", "a.txt"], {"*.txt", "?.txt", "[x].txt"}),
    ("\\a.txt\n", ["a.txt", "b.txt"], {"a.txt"}),
    ("broken\\\n[x\n", ["broken", "broken\\", "[x"], set()),
    ("\ufeff*.tmp\r\n!keep.tmp\r\n", ["a.tmp", "keep.tmp"], {"a.tmp"}),
    ("?.txt\n", ["a.txt", "é.txt"], {"a.txt"}),
    ("??.txt\n", ["a.txt", "é.txt"], {"é.txt"}),
])
def test_ignore_patterns_without_git(tmp_path, patterns, candidates, expected):
    write(tmp_path, ".gitignore", patterns)
    assert ignored_project_paths(tmp_path, candidates) == expected
    assert not (tmp_path / ".git").exists()


def test_nested_rules_override_ancestors_and_are_relative_to_their_directory(tmp_path):
    write(tmp_path, ".gitignore", "*.secret\n")
    write(tmp_path, "nested/.gitignore", "!keep.secret\n/root.txt\nchild/private.txt\n")
    write(tmp_path, "nested/child/.gitignore", "!private.txt\n*.cache\n")
    candidates = ["root.txt", "keep.secret", "nested/keep.secret", "nested/root.txt",
                  "nested/deeper/root.txt", "nested/child/private.txt", "nested/child/a.cache",
                  "sibling/a.cache", "sibling/keep.secret"]
    assert ignored_project_paths(tmp_path, candidates) == {
        "keep.secret", "nested/root.txt", "nested/child/a.cache", "sibling/keep.secret",
    }


def test_ignored_parent_cannot_be_reincluded_by_nested_ignore(tmp_path):
    write(tmp_path, ".gitignore", "private/\n!private/keep.txt\n")
    write(tmp_path, "private/.gitignore", "!*\n")
    assert ignored_project_paths(tmp_path, ["private/keep.txt", "private/deep/a.py"]) == {
        "private/keep.txt", "private/deep/a.py",
    }


def test_ignored_subtree_does_not_read_its_ignore_file(tmp_path, monkeypatch):
    write(tmp_path, ".gitignore", "private/\n")
    nested = write(tmp_path, "private/.gitignore", "!*\n")
    original = publish_local._read_regular

    def guarded(path, limit):
        assert path != nested
        return original(path, limit)

    monkeypatch.setattr(publish_local, "_read_regular", guarded)
    assert ignored_project_paths(tmp_path, ["private/a.txt"]) == {"private/a.txt"}


def test_symlinked_ignore_and_directory_are_not_followed(tmp_path):
    outside = write(tmp_path, "outside/rules", "*\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".gitignore").symlink_to(outside)
    (project / "linked").symlink_to(outside.parent, target_is_directory=True)
    assert ignored_project_paths(project, ["safe.txt", "linked/a.txt"]) == {"linked/a.txt"}


def test_rules_and_directory_state_are_read_once(tmp_path, monkeypatch):
    write(tmp_path, ".gitignore", "*.tmp\n")
    write(tmp_path, "nested/.gitignore", "!keep.tmp\n")
    original = publish_local._read_regular
    reads = []

    def track(path, limit):
        reads.append(path)
        return original(path, limit)

    monkeypatch.setattr(publish_local, "_read_regular", track)
    assert ignored_project_paths(tmp_path, ["nested/a.tmp", "nested/keep.tmp", "nested/b.py"]) == {"nested/a.tmp"}
    assert reads == [tmp_path / ".gitignore", tmp_path / "nested/.gitignore"]


@pytest.mark.parametrize("candidate", ["../outside.txt", "/outside.txt", "a//b.txt", "./a.txt", "a/../b.txt", "a\0b"])
def test_ignore_rejects_unsafe_paths(tmp_path, candidate):
    with pytest.raises(ValueError, match="unsafe relative path"):
        ignored_project_paths(tmp_path, [candidate])


def test_cancellation_is_propagated_even_without_ignore_files(tmp_path):
    class Cancelled(RuntimeError):
        pass

    def cancel():
        raise Cancelled("cancelled")

    with pytest.raises(Cancelled):
        ignored_project_paths(tmp_path, ["a.py"], cancel)


def test_oversized_ignore_stops_instead_of_publishing_excluded_files(tmp_path, monkeypatch):
    monkeypatch.setattr(publish_local, "_MAX_IGNORE_BYTES", 10)
    write(tmp_path, ".gitignore", "private-material.txt\n")
    with pytest.raises(ValueError, match="too large"):
        ignored_project_paths(tmp_path, ["private-material.txt"])


def test_unreadable_ignore_stops_scan(tmp_path, monkeypatch):
    def unreadable(_path, _limit):
        raise PermissionError("denied")

    monkeypatch.setattr(publish_local, "_read_regular", unreadable)
    with pytest.raises(ValueError, match=r"Could not read \.gitignore rules"):
        ignored_project_paths(tmp_path, ["a.py"])


@pytest.mark.parametrize("config, expected", [
    ('[core]\nrepositoryformatversion = 0\nfilemode # Boolean with a comment\n[remote "origin"]\nurl = https://github.com/clarence/project.git\n', "https://github.com/clarence/project.git"),
    ('[REMOTE "origin"]\nURL = "git@github.com:clarence/project.git" # note\n', "git@github.com:clarence/project.git"),
    ('[remote "origin" ]\nURL = https://github.com/clarence/project.git\n', "https://github.com/clarence/project.git"),
    ('[remote.origin]\nurl = https://github.com/clarence/project.git\n', "https://github.com/clarence/project.git"),
    ('[remote "Origin"]\nurl = https://github.com/wrong/repo.git\n', ""),
    ('[remote "upstream"]\nurl = https://github.com/upstream/project.git\n', ""),
    ('[remote "origin"]\nurl = https://github.com/first/repo.git\nurl = https://github.com/second/repo.git\n', "https://github.com/first/repo.git"),
    ('[remote "origin"]\nurl = https://github.com/first/repo.git\nurl =\nurl = https://github.com/second/repo.git\n', "https://github.com/second/repo.git"),
    ('[remote "origin"] url = https://github.com/clarence/project.git ; note\n', "https://github.com/clarence/project.git"),
    ('[remote "origin"]\nurl = https://github.com/clarence/\\\nproject.git\n', "https://github.com/clarence/project.git"),
    ('[remote "origin"]\nurl = "https://github.com/clarence/project.git\\q"\n', ""),
    ('[remote "origin"]\nurl = "https://github.com/clarence/project.git\\n"\n', ""),
    ('[remote "origin"]\nurl = "unclosed\n', ""),
])
def test_literal_origin_without_git(tmp_path, config, expected):
    write(tmp_path, ".git/config", config)
    assert read_origin_url(tmp_path) == expected


def test_git_config_include_and_executable_settings_are_not_evaluated(tmp_path):
    external = write(tmp_path, "external-config", '[remote "origin"]\nurl = https://github.com/wrong/repo.git\n')
    write(tmp_path, ".git/config", f'[include]\npath = "{external}"\n[credential]\nhelper = !touch forbidden\n'
          '[url "https://github.com/wrong/"]\ninsteadOf = https://github.com/clarence/\n'
          '[remote "origin"]\nurl = https://github.com/clarence/project.git\n')
    assert read_origin_url(tmp_path) == "https://github.com/clarence/project.git"
    assert not (tmp_path / "forbidden").exists()
    write(tmp_path, ".git/config", f'[include]\npath = "{external}"\n')
    assert read_origin_url(tmp_path) == ""


def test_worktree_gitdir_and_commondir_are_read_without_git(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    write(project, ".git", "gitdir: ../main/.git/worktrees/project\n")
    write(tmp_path, "main/.git/worktrees/project/commondir", "../..\n")
    write(tmp_path, "main/.git/config", '[remote "origin"]\nurl = https://github.com/clarence/project.git\n')
    assert read_origin_url(project) == "https://github.com/clarence/project.git"


def test_submodule_gitdir_and_enabled_worktree_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    write(project, ".git", "gitdir: ../main/.git/modules/project\n")
    metadata = tmp_path / "main/.git/modules/project"
    write(metadata, "config", '[extensions]\nworktreeConfig = true\n[remote "origin"]\nurl = https://github.com/first/project.git\n')
    write(metadata, "config.worktree", '[remote "origin"]\nurl =\nurl = https://github.com/clarence/project.git\n')
    assert read_origin_url(project) == "https://github.com/clarence/project.git"


def test_disabled_worktree_config_does_not_override_origin(tmp_path):
    write(tmp_path, ".git/config", '[remote "origin"]\nurl = https://github.com/clarence/project.git\n')
    write(tmp_path, ".git/config.worktree", '[remote "origin"]\nurl =\nurl = https://github.com/wrong/project.git\n')
    assert read_origin_url(tmp_path) == "https://github.com/clarence/project.git"


@pytest.mark.parametrize("pointer", ["invalid", "gitdir: //network/share/repository", "gitdir: \\\\network\\share", "gitdir: ../x\n../y", "gitdir: \0"])
def test_malformed_or_network_gitdir_pointer_is_ignored(tmp_path, pointer):
    write(tmp_path, ".git", pointer)
    assert read_origin_url(tmp_path) == ""


@pytest.mark.parametrize("part", ["gitdir", "config", "commondir"])
def test_symlinked_git_metadata_is_not_followed(tmp_path, part):
    project = tmp_path / "project"
    project.mkdir()
    external = write(tmp_path, "external/config", '[remote "origin"]\nurl = https://github.com/wrong/repo.git\n')
    if part == "gitdir":
        (project / ".git").symlink_to(external.parent, target_is_directory=True)
    elif part == "config":
        (project / ".git").mkdir()
        (project / ".git/config").symlink_to(external)
    else:
        write(project, ".git/config", "[core]\nrepositoryformatversion = 0\n")
        pointer = write(tmp_path, "common-pointer", str(external.parent))
        (project / ".git/commondir").symlink_to(pointer)
    assert read_origin_url(project) == ""


def test_origin_is_best_effort_when_config_is_unreadable_or_oversized(tmp_path, monkeypatch):
    write(tmp_path, ".git/config", '[remote "origin"]\nurl = https://github.com/clarence/project.git\n')
    monkeypatch.setattr(publish_local, "_MAX_CONFIG_BYTES", 10)
    assert read_origin_url(tmp_path) == ""


def test_directory_ignore_is_rejected_instead_of_being_read(tmp_path):
    (tmp_path / ".gitignore").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        ignored_project_paths(tmp_path, ["a.py"])


def test_complex_wildcards_do_not_use_recursive_backtracking(tmp_path):
    write(tmp_path, ".gitignore", "*a" * 90 + "Z\n")
    assert ignored_project_paths(tmp_path, ["a" * 180 + "Y"]) == set()
