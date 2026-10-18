"""Reviewed GitHub publishing through HTTPS, without Git or GitHub CLI.

The source project is only read. Review creates an immutable local snapshot
and makes GET requests only. Publishing writes that exact snapshot to a fresh
GitHub branch, preserving the project's index, remotes, and local history.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence
from urllib.parse import quote, urlparse

from .github_connection import GitHubError, get_session
from .publish_local import ignored_project_paths, read_origin_url


MAX_FILE_BYTES = 95_000_000
MAX_TOTAL_BYTES = 500_000_000
MAX_FILE_COUNT = 10_000
MAX_DIFF_FILE_BYTES = 1_000_000
MAX_DIFF_BYTES = 2_000_000
MAX_REVIEW_DOWNLOAD_BYTES = 20_000_000
MAX_ATTRIBUTE_BYTES = 128_000
GITHUB_CLI_URL = "https://cli.github.com/"
GIT_DOWNLOAD_URL = "https://git-scm.com/downloads"
VERCEL_IMPORT_URL = "https://vercel.com/new"
STREAMLIT_DEPLOY_URL = "https://share.streamlit.io/"
VERCEL_PYTHON_DOCS = "https://vercel.com/docs/functions/runtimes/python"
VERCEL_CONTAINER_DOCS = "https://vercel.com/docs/functions/runtimes/container"
STREAMLIT_DEPLOY_DOCS = "https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy"

_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".nox", ".idea", ".vscode", ".vercel", ".next", ".nuxt", ".svelte-kit",
    ".aws", ".ssh", ".azure", ".gcloud", ".ipynb_checkpoints", "htmlcov",
}
_EXCLUDED_NAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "credentials.json", "credentials", "secrets.json", "secrets.yaml",
    "secrets.yml", "secrets.toml", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "launcher_error.log", "completion_diagnostic.txt", ".ds_store", "thumbs.db",
}
_PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
_GENERATED_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".swp"}
_REPO_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_PRIVATE_KEY = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_CONTENT_SHA = re.compile(r"^[0-9a-f]{64}$")
_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


class PublishError(RuntimeError):
    pass


class PublishCancelled(PublishError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


class CommandRunner:
    """Run installed native executables without a shell, off the GUI thread."""

    def __init__(
        self,
        log: Callable[[str], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> None:
        self.log = log or (lambda _message: None)
        self.cancelled = cancelled or threading.Event()

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise PublishCancelled("Publishing was cancelled. Completed GitHub actions are not undone.")

    @staticmethod
    def executable(name: str) -> str:
        found = shutil.which(name)
        if not found:
            label = "GitHub CLI (gh)" if name == "gh" else "Git"
            raise PublishError(f"Install {label}, then reopen this publishing window so Studio can find it.")
        if Path(found).suffix.lower() in {".cmd", ".bat"}:
            raise PublishError(f"Install the native {name}.exe application; a command-script wrapper is not supported.")
        return found

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        timeout: float = 120,
        check: bool = True,
        live_output: bool = False,
    ) -> CommandResult:
        self.check_cancelled()
        command = [self.executable(str(args[0])), *(str(value) for value in args[1:])]
        environment = dict(os.environ)
        # Use the account managed by gh, not inherited repository/index options.
        for key in tuple(environment):
            if key.startswith("GIT_") or key in {"GH_REPO", "GH_HOST", "GH_DEBUG"}:
                environment.pop(key, None)
        environment.update({
            "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
            "NO_COLOR": "1", "CLICOLOR": "0",
        })
        if list(args[:3]) != ["gh", "auth", "login"]:
            environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        # Explicit Sign in uses gh's ordinary credential setup. Pointing its
        # Git configuration at the null device would break that setup on some
        # installations; isolated publishing commands never change that config.
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(
                command, cwd=cwd, env=environment, shell=False,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs,
            )
        except OSError as exc:
            raise PublishError(f"Could not start {args[0]}: {exc}") from exc
        chunks: queue.Queue[bytes | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            try:
                while chunk := process.stdout.read1(4096):
                    chunks.put(chunk)
            finally:
                chunks.put(None)

        reader = threading.Thread(target=read_output, name="studio-publish-output", daemon=True)
        reader.start()
        output = bytearray()
        timed_out = False
        try:
            if input_bytes is not None and process.stdin is not None:
                process.stdin.write(input_bytes)
                process.stdin.close()
            deadline = time.monotonic() + timeout
            complete = False
            while not complete:
                if self.cancelled.is_set() or time.monotonic() > deadline:
                    timed_out = not self.cancelled.is_set()
                    self._stop_process(process)
                    break
                try:
                    chunk = chunks.get(timeout=0.05)
                except queue.Empty:
                    continue
                if chunk is None:
                    complete = True
                else:
                    # Bound command output, including a large Git diff.
                    if len(output) < 2_000_000:
                        output.extend(chunk[:2_000_000 - len(output)])
                    if live_output:
                        self.log(chunk.decode("utf-8", errors="replace"))
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._stop_process(process)
            raise PublishError(f"{args[0]} could not complete: {exc}") from exc
        finally:
            reader.join(timeout=1)
            if process.stdout:
                process.stdout.close()
        self.check_cancelled()
        if timed_out:
            raise PublishError(f"{args[0]} timed out after {timeout:g} seconds. Check your connection and retry.")
        result = CommandResult(process.returncode or 0, output.decode("utf-8", errors="replace"))
        if check and result.returncode:
            detail = result.output.strip()[-12_000:] or f"Exit code {result.returncode}"
            raise PublishError(f"{args[0]} failed:\n{detail}")
        return result

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"], shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait(timeout=3)


@dataclass(frozen=True)
class PublishFile:
    path: str
    size: int
    sha256: str
    selected: bool = True
    mode: str = "100644"


@dataclass(frozen=True)
class ReviewedChange:
    path: str
    mode: str
    sha: str
    old_sha: str = ""
    old_mode: str = ""


@dataclass(frozen=True)
class ProjectScan:
    project_path: Path
    files: tuple[PublishFile, ...]
    excluded: tuple[tuple[str, str], ...]
    github_repository: str = ""


@dataclass(frozen=True)
class PreparedPublish:
    source_path: Path
    repository: str
    account: str
    visibility: str
    new_repository: bool
    branch: str
    base_branch: str
    revision: str
    files: tuple[PublishFile, ...]
    diff: str
    change_summary: str
    working_path: Path
    _temporary: tempfile.TemporaryDirectory = field(repr=False)
    message: str = "Update project from HoYo Project Studio"
    account_id: int = 0
    repository_id: int = 0
    owner_id: int = 0
    owner_type: str = "User"
    default_branch: str = "main"
    base_revision: str = ""
    base_tree: str = ""
    empty_repository: bool = False
    bootstrap_file: str = ""
    changes: tuple[ReviewedChange, ...] = ()
    expected_tree: str = ""

    @property
    def repository_url(self) -> str:
        return f"https://github.com/{self.repository}"

    def close(self) -> None:
        self._temporary.cleanup()


@dataclass(frozen=True)
class PublishResult:
    repository: str
    branch: str
    revision: str
    repository_url: str
    review_url: str
    new_repository: bool


@dataclass(frozen=True)
class DeploymentCompatibility:
    supported: bool
    label: str
    explanation: str


def normalize_github_repository(value: str, *, account: str = "") -> str:
    value = value.strip()
    clone_url = False
    if value.startswith("git@github.com:"):
        clone_url = True
        value = value[len("git@github.com:"):]
    elif "://" in value:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
            raise PublishError("Use a GitHub repository URL such as https://github.com/owner/repository.")
        if parsed.query or parsed.fragment:
            raise PublishError("Use the repository URL without query parameters or a fragment.")
        value = parsed.path.strip("/")
        clone_url = True
    value = value.strip("/")
    # A bare owner/name may come directly from the repository picker. Preserve
    # that exact API identity; only clone URLs have a removable .git suffix.
    if clone_url:
        value = value.removesuffix(".git")
    parts = value.split("/")
    if len(parts) == 1 and account:
        parts.insert(0, account)
    if len(parts) != 2 or not _REPO_PART.fullmatch(parts[0]) or not _REPOSITORY_NAME.fullmatch(parts[1]):
        raise PublishError("Enter a GitHub destination as owner/repository (or a repository name for your account).")
    if any(part.endswith(".") or part in {".", ".."} for part in parts):
        raise PublishError("The GitHub repository name is invalid.")
    return "/".join(parts)


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or not path.parts or path.as_posix() != value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PublishError(f"This file path cannot be published: {value!r}")
    return path


def _excluded_reason(path: PurePosixPath) -> str:
    lowered = tuple(part.lower() for part in path.parts)
    name = lowered[-1]
    if any(part in _EXCLUDED_DIRS for part in lowered):
        return "Local tools, credentials, environment or generated cache"
    if ".studio" in lowered:
        if lowered != (".studio", "published.json"):
            return "Local Studio settings; only published.json is shared"
    if name in _EXCLUDED_NAMES or name.startswith(".env."):
        return "Credentials or local environment settings"
    if Path(name).suffix in _PRIVATE_SUFFIXES:
        return "Private key or certificate store"
    if Path(name).suffix in _GENERATED_SUFFIXES:
        return "Generated or temporary file"
    return ""


def _regular_file(root: Path, relative: str) -> Path:
    parts = _safe_relative(relative).parts
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise PublishError(f"Symbolic links are not published: {relative}")
    try:
        if not stat.S_ISREG(candidate.stat().st_mode):
            raise PublishError(f"This is not a regular file: {relative}")
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise PublishError(f"This file is unavailable: {relative}") from exc
    return candidate


def _git(runner: CommandRunner, arguments: Sequence[str], *, cwd: Path, literal_paths: bool = True, **kwargs) -> CommandResult:
    # Empty hooks and helpers prevent inherited executable hooks/filters and
    # keep GitHub authentication inside the installed official gh helper.
    return runner.run([
        "git", *(["--literal-pathspecs"] if literal_paths else []), "-c", "core.hooksPath=" + os.devnull,
        "-c", "commit.gpgsign=false", "-c", "credential.helper=",
        "-c", "credential.https://github.com.helper=!gh auth git-credential",
        "-c", "protocol.file.allow=never", *arguments,
    ], cwd=cwd, **kwargs)


def scan_project(project_path: Path, runner: CommandRunner | None = None) -> ProjectScan:
    runner = runner or CommandRunner()
    root = project_path.expanduser().resolve()
    if not root.is_dir():
        raise PublishError("The selected project folder is unavailable.")
    runner.log("Reading project files…")
    candidates: list[str] = []
    excluded: list[tuple[str, str]] = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        runner.check_cancelled()
        parent = Path(directory)
        kept = []
        for name in sorted(dirs):
            relative = (parent / name).relative_to(root).as_posix()
            reason = _excluded_reason(PurePosixPath(relative))
            # Keep .studio open just long enough to inspect published.json.
            if relative == ".studio":
                reason = ""
            if (parent / name).is_symlink():
                reason = "Symbolic link"
            if reason:
                excluded.append((relative + "/", reason))
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            relative = (parent / name).relative_to(root).as_posix()
            reason = _excluded_reason(PurePosixPath(relative))
            if not reason:
                try:
                    _safe_relative(relative)
                    source = _regular_file(root, relative)
                    if source.stat().st_size > MAX_FILE_BYTES:
                        reason = "Too large for this publisher (95 MB maximum per file)"
                except PublishError as exc:
                    reason = str(exc)
            if reason:
                excluded.append((relative, reason))
            else:
                candidates.append(relative)
            if len(candidates) > MAX_FILE_COUNT:
                raise PublishError("This folder has more than 10,000 source files. Select a smaller project folder.")
    try:
        ignored = ignored_project_paths(root, candidates, runner.check_cancelled)
    except (ValueError, OSError) as exc:
        raise PublishError("Could not read the project's .gitignore rules: " + str(exc)) from exc
    files: list[PublishFile] = []
    for relative in sorted(candidates, key=str.casefold):
        runner.check_cancelled()
        # Published design settings are an explicit, visible deployment input.
        if relative in ignored and relative != ".studio/published.json":
            excluded.append((relative, "Excluded by .gitignore"))
            continue
        source = _regular_file(root, relative)
        data, mode = _read_regular_bytes(root, relative, MAX_FILE_BYTES)
        if _PRIVATE_KEY.search(data):
            excluded.append((relative, "Contains a private key"))
            continue
        files.append(PublishFile(relative, len(data), hashlib.sha256(data).hexdigest(), len(data) <= 20_000_000, mode))
    remote = ""
    try:
        remote = normalize_github_repository(read_origin_url(root))
    except PublishError:
        pass
    runner.log(f"Found {len(files)} publishable files; {len(excluded)} files or folders excluded.")
    return ProjectScan(root, tuple(files), tuple(sorted(excluded)), remote)


def login_to_github(runner: CommandRunner, project_path: Path) -> None:
    """Compatibility entry point; the dialog now connects its token directly."""
    del project_path
    _authenticated_account(_client_for(runner, None), runner)
    runner.log("GitHub is connected. You can now review your publication.")


def _api_error(exc: GitHubError, *, creating: bool = False, workflow: bool = False) -> PublishError:
    if exc.cancelled:
        return PublishCancelled("Publishing was cancelled. Completed GitHub actions are not undone.")
    if exc.status == 401:
        return PublishError("GitHub sign-in expired or the token is invalid. Sign in with GitHub again, then review the publication.")
    permissions = "Select this repository in the token settings and allow Contents: Read and write."
    if creating:
        permissions += " Creating a repository also requires Administration: Read and write or Repository creation: Write, and permission to create repositories for its owner."
    if workflow:
        permissions += " Publishing .github/workflows files also requires Workflows: Read and write (workflow scope for a classic token)."
    if exc.status == 403:
        return PublishError("GitHub denied this request. " + permissions + " An organization may also require token approval or SSO authorization. If GitHub has limited requests, wait and review again.")
    if exc.status == 404:
        return PublishError("The GitHub repository or resource is unavailable to this account. Check owner/repository and token access. " + permissions)
    if exc.status in {409, 422}:
        return PublishError("GitHub could not apply this request. The repository or branch may already exist, or repository rules may reject it. Check the destination and review again. " + permissions)
    if exc.status is not None and exc.status >= 500:
        return PublishError("GitHub is temporarily unavailable. Check the destination before retrying a publication.")
    # The client handles detailed network diagnostics. Never echo server bodies,
    # request data, authorization headers, or a supplied token into the UI log.
    return PublishError("Could not complete the GitHub request. Check your connection, reconnect to GitHub if needed, and review again.")


def _client_for(runner: CommandRunner, client):
    runner.check_cancelled()
    if client is not None:
        return client
    try:
        return get_session().client(cancelled=runner.cancelled, log=runner.log)
    except GitHubError as exc:
        if exc.cancelled:
            raise _api_error(exc) from exc
        raise PublishError("Sign in with GitHub in Studio first. No Git or GitHub CLI installation is needed.") from exc


def _request(client, runner: CommandRunner, method: str, path: str, data=None, *, missing: bool = False, creating: bool = False, workflow: bool = False):
    runner.check_cancelled()
    try:
        return client.request(method, path, data)
    except GitHubError as exc:
        if missing and exc.status == 404 and not exc.cancelled:
            return None
        raise _api_error(exc, creating=creating, workflow=workflow) from exc


def _authenticated_account(client, runner: CommandRunner) -> dict:
    runner.check_cancelled()
    try:
        account = client.get_authenticated_user()
    except GitHubError as exc:
        raise _api_error(exc) from exc
    if not isinstance(account, dict) or not _REPO_PART.fullmatch(str(account.get("login", ""))) or type(account.get("id")) is not int or account["id"] <= 0:
        raise PublishError("GitHub did not return a valid signed-in account. Reconnect to GitHub and review again.")
    return account


def _sha(value, label: str = "object") -> str:
    if not isinstance(value, str) or not _GIT_SHA.fullmatch(value):
        raise PublishError(f"GitHub returned an invalid {label}. Review the publication again.")
    return value


def _branch_name(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 240 or value.startswith(("/", "-")) or value.endswith(("/", ".")) or ".." in value or "@{" in value or "//" in value or value == "@" or any(ord(c) < 33 or ord(c) == 127 or c in "~^:?*[\\" for c in value) or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/")):
        raise PublishError("GitHub returned an unsupported branch name. Review the repository on GitHub.")
    return value


def _repo_path(destination: str) -> str:
    return "/repos/" + quote(destination, safe="/")


def _read_regular_bytes(root: Path, relative: str, limit: int) -> tuple[bytes, str]:
    source = _regular_file(root, relative)
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise PublishError(f"This file is not a supported regular file or exceeds the publishing limit: {relative}")
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_mode) != (after.st_size, after.st_mtime_ns, after.st_mode):
            raise PublishError(f"{relative} changed while it was being read. Refresh files and review again.")
        return data, "100755" if os.name != "nt" and before.st_mode & stat.S_IXUSR else "100644"
    except OSError as exc:
        raise PublishError(f"This file is unavailable: {relative}") from exc


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _copy_reviewed_file(source_root: Path, destination_root: Path, item: PublishFile) -> bytes:
    if _excluded_reason(_safe_relative(item.path)):
        raise PublishError(f"This local or private file cannot be published: {item.path}")
    data, mode = _read_regular_bytes(source_root, item.path, MAX_FILE_BYTES)
    if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256 or mode != item.mode:
        raise PublishError(f"{item.path} changed since the file list was loaded. Refresh files and review again.")
    if _PRIVATE_KEY.search(data):
        raise PublishError(f"This file contains a private key and cannot be published: {item.path}")
    if data.startswith(_LFS_POINTER):
        raise PublishError(f"{item.path} is a Git LFS pointer. Studio cannot upload its external LFS object; leave it unselected.")
    destination = destination_root.joinpath(*_safe_relative(item.path).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    destination.chmod(0o755 if item.mode == "100755" else 0o644)
    return data


def _validate_files(files: Sequence[PublishFile]) -> None:
    if not files:
        raise PublishError("Select at least one file to publish.")
    if len(files) > MAX_FILE_COUNT:
        raise PublishError("Select no more than 10,000 files to publish.")
    paths: set[str] = set()
    for item in files:
        _safe_relative(item.path)
        if item.path in paths:
            raise PublishError("A file was selected more than once. Refresh the file list.")
        if type(item.size) is not int or not 0 <= item.size <= MAX_FILE_BYTES or not isinstance(item.sha256, str) or not _CONTENT_SHA.fullmatch(item.sha256) or item.mode not in {"100644", "100755"}:
            raise PublishError("The selected file list is invalid or exceeds the 95 MB per-file limit. Refresh files and review again.")
        paths.add(item.path)
    for path in paths:
        if any(parent.as_posix() in paths for parent in PurePosixPath(path).parents if parent.parts):
            raise PublishError("The selected files contain conflicting file and folder paths. Refresh the file list.")
    if sum(item.size for item in files) > MAX_TOTAL_BYTES:
        raise PublishError("The selected files exceed 500 MB. Select fewer files.")


def _repository_metadata(client, runner: CommandRunner, destination: str, *, missing: bool = False) -> dict | None:
    metadata = _request(client, runner, "GET", _repo_path(destination), missing=missing)
    if metadata is None:
        return None
    if not isinstance(metadata, dict) or type(metadata.get("id")) is not int or metadata["id"] <= 0 or type(metadata.get("private")) is not bool:
        raise PublishError("GitHub returned unexpected repository details. Review the destination and retry.")
    if normalize_github_repository(str(metadata.get("full_name", ""))).lower() != destination.lower():
        raise PublishError("The GitHub destination changed. Review its new repository address before publishing.")
    if metadata.get("archived") or metadata.get("disabled"):
        raise PublishError("This repository is archived or disabled and cannot accept a publication.")
    permissions = metadata.get("permissions", {})
    if isinstance(permissions, dict) and permissions.get("push") is False:
        raise PublishError("This GitHub account cannot write to the repository. Use an account with repository access and a token with Contents: Read and write.")
    _branch_name(metadata.get("default_branch", "main"))
    return metadata


def _owner_details(client, runner: CommandRunner, destination: str, account: dict) -> tuple[int, str]:
    owner = destination.split("/", 1)[0]
    if owner.lower() == account["login"].lower():
        return account["id"], "User"
    metadata = _request(client, runner, "GET", "/users/" + quote(owner, safe=""))
    if not isinstance(metadata, dict) or str(metadata.get("login", "")).lower() != owner.lower() or type(metadata.get("id")) is not int or metadata["id"] <= 0:
        raise PublishError("GitHub returned unexpected destination-owner details. Review again.")
    if metadata.get("type") != "Organization":
        raise PublishError("Create a repository in your own GitHub account or an organization where you have permission. Another user's account cannot be selected for a new repository.")
    return metadata["id"], "Organization"


def _default_head(client, runner: CommandRunner, destination: str, branch: str) -> str:
    path = _repo_path(destination)
    runner.check_cancelled()
    try:
        reference = client.request("GET", path + "/git/ref/heads/" + quote(branch, safe=""))
    except GitHubError as exc:
        if exc.status not in {404, 409} or exc.cancelled:
            raise _api_error(exc) from exc
        # A missing default ref is not evidence that a repository is empty.
        # Verify there are no branches, using an independently successful GET.
        branches = _request(client, runner, "GET", path + "/branches?per_page=1")
        if branches == []:
            return ""
        raise PublishError("GitHub's default branch is unavailable but the repository is not empty. Review its default branch on GitHub and retry.") from exc
    if not isinstance(reference, dict) or reference.get("ref") != "refs/heads/" + branch or not isinstance(reference.get("object"), dict) or reference["object"].get("type") != "commit":
        raise PublishError("GitHub returned unexpected default-branch details. Review again.")
    return _sha(reference["object"].get("sha"), "default-branch commit")


def _read_tree(client, runner: CommandRunner, destination: str, revision: str) -> tuple[str, dict[str, dict]]:
    prefix = _repo_path(destination)
    commit = _request(client, runner, "GET", prefix + "/git/commits/" + revision)
    if not isinstance(commit, dict) or commit.get("sha") != revision or not isinstance(commit.get("tree"), dict):
        raise PublishError("GitHub returned unexpected commit details. Review again.")
    tree_sha = _sha(commit["tree"].get("sha"), "commit tree")
    response = _request(client, runner, "GET", prefix + "/git/trees/" + tree_sha + "?recursive=1")
    if not isinstance(response, dict) or response.get("sha") != tree_sha or not isinstance(response.get("tree"), list):
        raise PublishError("GitHub returned unexpected repository tree details. Review again.")
    if response.get("truncated") is not False or len(response["tree"]) > 100_000:
        raise PublishError("GitHub's repository tree was truncated or is too large to review safely. Publish a smaller repository or use GitHub's website.")
    entries: dict[str, dict] = {}
    for entry in response["tree"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise PublishError("GitHub returned an invalid repository tree entry.")
        path = entry["path"]
        # Remote names can be legal on GitHub but unavailable on Windows. Only
        # selected paths are interpreted locally; other tree entries stay remote.
        if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")) or path in entries:
            raise PublishError("GitHub returned conflicting repository paths. Review the repository on GitHub.")
        _sha(entry.get("sha"), "tree entry")
        pair = (entry.get("type"), entry.get("mode"))
        if pair not in {("blob", "100644"), ("blob", "100755"), ("blob", "120000"), ("tree", "040000"), ("commit", "160000")}:
            raise PublishError("GitHub returned an unsupported repository tree entry.")
        if entry["type"] == "blob" and (type(entry.get("size")) is not int or entry["size"] < 0):
            raise PublishError("GitHub returned an invalid file size.")
        entries[path] = entry
    for path in entries:
        parent = path.rpartition("/")[0]
        if parent and (parent not in entries or entries[parent]["type"] != "tree"):
            raise PublishError("GitHub returned an incomplete or conflicting directory tree. Review the repository on GitHub.")
    return tree_sha, entries


def _check_remote_path(path: str, entries: dict[str, dict]) -> None:
    existing = entries.get(path)
    if existing and (existing["type"] != "blob" or existing["mode"] not in {"100644", "100755"}):
        raise PublishError(f"The GitHub path {path} is a directory, symbolic link, or submodule. Resolve that conflict on GitHub before publishing this file.")
    for parent in PurePosixPath(path).parents:
        ancestor = entries.get(parent.as_posix())
        if ancestor and ancestor["type"] != "tree":
            raise PublishError(f"The GitHub destination contains a file, symbolic link, or submodule above {path}. Resolve that conflict first.")


def _tree_sha(entries: dict[str, dict], changes: Sequence[ReviewedChange] = ()) -> str:
    """Compute the exact Git tree SHA locally, including untouched remote files."""
    nodes: dict[str, dict[str, tuple[str, str]]] = {"": {}}
    for path, entry in entries.items():
        parent, _, name = path.rpartition("/")
        nodes.setdefault(parent, {})[name] = (entry["mode"], entry["sha"])
        if entry["type"] == "tree":
            nodes.setdefault(path, {})
    for change in changes:
        parts = change.path.split("/")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index - 1])
            path = "/".join(parts[:index])
            nodes.setdefault(parent, {})[parts[index - 1]] = ("040000", "")
            nodes.setdefault(path, {})
        nodes.setdefault("/".join(parts[:-1]), {})[parts[-1]] = (change.mode, change.sha)
    hashes: dict[str, str] = {}
    for path in sorted(nodes, key=lambda value: (value.count("/"), len(value)), reverse=True):
        content = bytearray()
        for name, (mode, sha) in sorted(nodes[path].items(), key=lambda item: (item[0] + ("/" if item[1][0] == "040000" else "")).encode("utf-8")):
            content.extend(mode.lstrip("0").encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(sha))
        hashes[path] = hashlib.sha1(b"tree " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
        if path:
            parent, _, name = path.rpartition("/")
            nodes[parent][name] = ("040000", hashes[path])
    return hashes[""]


def _remote_blob(client, runner: CommandRunner, destination: str, entry: dict, *, limit: int) -> bytes:
    if entry["size"] > limit:
        raise PublishError("A GitHub file exceeds the permitted review download size.")
    response = _request(client, runner, "GET", _repo_path(destination) + "/git/blobs/" + entry["sha"])
    if not isinstance(response, dict) or response.get("encoding") != "base64" or response.get("sha") != entry["sha"] or response.get("size") != entry["size"] or not isinstance(response.get("content"), str):
        raise PublishError("GitHub returned unexpected file content. Review again.")
    encoded = "".join(response["content"].split())
    if len(encoded) > ((limit + 2) // 3) * 4:
        raise PublishError("GitHub returned file content larger than the review limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise PublishError("GitHub returned invalid file content. Review again.") from exc
    if len(data) != entry["size"] or _git_blob_sha(data) != entry["sha"]:
        raise PublishError("GitHub file content did not match its reviewed hash. Review again.")
    return data


def _attributes_for(paths: Sequence[str]) -> set[str]:
    result = {".gitattributes"}
    for path in paths:
        for parent in PurePosixPath(path).parents:
            if parent.parts:
                result.add((parent / ".gitattributes").as_posix())
    return result


def _check_lfs_rules(root: Path, paths: Sequence[str], entries: dict[str, dict], client, runner: CommandRunner, destination: str) -> None:
    # Attribute macros, nested overrides, and server LFS storage need a dedicated
    # LFS implementation. Fail visibly for an affected subtree instead of
    # silently committing ordinary blobs under active LFS rules.
    for relative in sorted(_attributes_for(paths)):
        runner.check_cancelled()
        source = root.joinpath(*PurePosixPath(relative).parts)
        contents: list[bytes] = []
        if source.exists() or source.is_symlink():
            contents.append(_read_regular_bytes(root, relative, MAX_ATTRIBUTE_BYTES)[0])
        remote = entries.get(relative)
        if remote:
            if remote["type"] != "blob" or remote["mode"] not in {"100644", "100755"}:
                raise PublishError(f"GitHub has unsupported Git attributes at {relative}. Review that file on GitHub.")
            contents.append(_remote_blob(client, runner, destination, remote, limit=MAX_ATTRIBUTE_BYTES))
        for data in contents:
            for line in data.splitlines():
                if line.lstrip().startswith(b"#"):
                    continue
                if re.search(rb"(?:^|\s)filter=lfs(?:\s|$)", line):
                    raise PublishError(f"Git LFS rules are present in {relative}. Studio does not upload LFS objects. Publish ordinary files from a folder without these rules, or manage this repository with Git LFS.")


def _text_lines(data: bytes) -> list[str] | None:
    if b"\0" in data or len(data) > MAX_DIFF_FILE_BYTES:
        return None
    try:
        result = data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None
    if len(result) > 30_000:
        return None
    return result


def _file_diff(item: PublishFile, data: bytes, old: dict | None, previous: bytes | None, new_sha: str, remaining: int) -> str:
    label = json.dumps(item.path, ensure_ascii=False)
    headers = [f"diff --git {label} {label}\n"]
    if old is None:
        headers.append(f"new file mode {item.mode}\n")
    elif old["mode"] != item.mode:
        headers.extend([f"old mode {old['mode']}\n", f"new mode {item.mode}\n"])
    headers.append(f"index {old['sha'] if old else '0' * 40}..{new_sha}\n")
    if old and old["sha"] == new_sha:
        return "".join(headers)
    old_lines = [] if old is None else _text_lines(previous) if previous is not None else None
    new_lines = _text_lines(data)
    if old_lines is None or new_lines is None:
        headers.append(f"Binary, large, or non-UTF-8 content: {old['size'] if old else 0} -> {item.size} bytes.\nNew SHA256: {item.sha256}\n")
        return "".join(headers)
    count = sum(len(line.encode("utf-8")) for line in headers)
    for line in difflib.unified_diff(old_lines, new_lines, fromfile="a/" + item.path if old else "/dev/null", tofile="b/" + item.path, n=3):
        if count + len(line.encode("utf-8")) > max(remaining, 0):
            headers.append("\n[Text preview limit reached. File size and exact SHA256 are listed in the change summary.]\n")
            break
        headers.append(line)
        count += len(line.encode("utf-8"))
        if line and not line.endswith("\n"):
            headers.append("\n\\ No newline at end of file\n")
    return "".join(headers)


def _plan_payload(plan: PreparedPublish) -> dict:
    return {
        "format": 1, "repository": plan.repository, "account": plan.account,
        "account_id": plan.account_id, "visibility": plan.visibility,
        "new_repository": plan.new_repository, "branch": plan.branch,
        "base_branch": plan.base_branch, "default_branch": plan.default_branch,
        "repository_id": plan.repository_id, "owner_id": plan.owner_id,
        "owner_type": plan.owner_type, "base_revision": plan.base_revision,
        "base_tree": plan.base_tree, "empty_repository": plan.empty_repository,
        "bootstrap_file": plan.bootstrap_file, "message": plan.message,
        "expected_tree": plan.expected_tree,
        "files": [asdict(item) for item in plan.files],
        "changes": [asdict(change) for change in plan.changes],
        "diff_sha256": hashlib.sha256(plan.diff.encode("utf-8")).hexdigest(),
        "summary_sha256": hashlib.sha256(plan.change_summary.encode("utf-8")).hexdigest(),
    }


def _manifest_bytes(plan: PreparedPublish) -> bytes:
    return json.dumps(_plan_payload(plan), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _verify_snapshot(plan: PreparedPublish, runner: CommandRunner) -> None:
    try:
        manifest = _manifest_bytes(plan)
        stored = _regular_file(plan.working_path.parent, "review.json").read_bytes()
        if manifest != stored or hashlib.sha256(manifest).hexdigest() != plan.revision:
            raise PublishError("The reviewed snapshot changed. Prepare and review it again.")
        found: set[str] = set()
        for directory, dirs, names in os.walk(plan.working_path, followlinks=False):
            runner.check_cancelled()
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise PublishError("The reviewed snapshot changed. Prepare and review it again.")
            for name in names:
                found.add((Path(directory) / name).relative_to(plan.working_path).as_posix())
        if found != {item.path for item in plan.files}:
            raise PublishError("The reviewed snapshot changed. Prepare and review it again.")
        for item in plan.files:
            runner.check_cancelled()
            data, mode = _read_regular_bytes(plan.working_path, item.path, MAX_FILE_BYTES)
            if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256 or mode != item.mode:
                raise PublishError("The reviewed snapshot changed. Prepare and review it again.")
    except OSError as exc:
        raise PublishError("The reviewed snapshot is unavailable. Prepare and review it again.") from exc


def prepare_github_publish(
    project_path: Path,
    files: Sequence[PublishFile],
    repository: str,
    *,
    new_repository: bool,
    visibility: str = "private",
    message: str = "Update project from HoYo Project Studio",
    runner: CommandRunner | None = None,
    client=None,
) -> PreparedPublish:
    runner = runner or CommandRunner()
    runner.check_cancelled()
    root = project_path.expanduser().resolve()
    if not root.is_dir():
        raise PublishError("The selected project folder is unavailable.")
    files = tuple(sorted(files, key=lambda item: item.path))
    _validate_files(files)
    if visibility not in {"private", "public"}:
        raise PublishError("Choose Private or Public repository visibility.")
    if not message.strip() or len(message) > 1000 or "\0" in message:
        raise PublishError("Enter a commit message of 1–1,000 characters.")
    client = _client_for(runner, client)
    runner.log("Checking the signed-in GitHub account…")
    account = _authenticated_account(client, runner)
    destination = normalize_github_repository(repository, account=account["login"])
    temporary = tempfile.TemporaryDirectory(prefix="hoyo-publish-")
    working = Path(temporary.name) / "files"
    working.mkdir()
    try:
        runner.log("Saving the selected files into the review snapshot…")
        for item in files:
            runner.check_cancelled()
            _copy_reviewed_file(root, working, item)
        repository_id = 0
        owner_id, owner_type = account["id"], "User"
        default_branch = "main"
        base_revision = base_tree = base_branch = ""
        entries: dict[str, dict] = {}
        if new_repository:
            owner_id, owner_type = _owner_details(client, runner, destination, account)
            if _repository_metadata(client, runner, destination, missing=True) is not None:
                raise PublishError("That GitHub repository already exists. Choose Existing repository and review it before publishing.")
            branch = "main"
        else:
            runner.log("Reading the GitHub repository and its default branch…")
            metadata = _repository_metadata(client, runner, destination)
            assert metadata is not None
            repository_id = metadata["id"]
            default_branch = _branch_name(metadata.get("default_branch", "main"))
            base_revision = _default_head(client, runner, destination, default_branch)
            branch = "studio/update-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            if base_revision:
                base_branch = default_branch
                base_tree, entries = _read_tree(client, runner, destination, base_revision)
                if _tree_sha(entries) != base_tree:
                    raise PublishError("GitHub's repository tree did not match its commit hash. Review again.")
            owner = metadata.get("owner", {})
            if isinstance(owner, dict) and type(owner.get("id")) is int:
                owner_id = owner["id"]
                owner_type = str(owner.get("type", "User"))
        for item in files:
            _check_remote_path(item.path, entries)
        _check_lfs_rules(root, [item.path for item in files], entries, client, runner, destination)
        empty = not base_revision
        bootstrap_file = ""
        if empty:
            seeds = [item for item in files if item.mode == "100644" and item.size <= MAX_DIFF_FILE_BYTES and not item.path.startswith(".github/workflows/")]
            if not seeds:
                raise PublishError("To initialize an empty GitHub repository, also select an ordinary non-executable file of 1 MB or less, such as README.md. Workflow files cannot be used as the initial file.")
            bootstrap_file = min(seeds, key=lambda item: (item.size, item.path)).path
        runner.log("Preparing the exact file changes for review…")
        changes: list[ReviewedChange] = []
        previews: list[str] = []
        summaries: list[str] = []
        preview_bytes = downloaded = 0
        blob_cache: dict[str, bytes] = {}
        for item in files:
            runner.check_cancelled()
            data, _mode = _read_regular_bytes(working, item.path, MAX_FILE_BYTES)
            new_sha = _git_blob_sha(data)
            old = entries.get(item.path)
            # Windows does not preserve Unix executable permission bits in a
            # checkout. Keep the repository's mode for an existing file there.
            target_mode = old["mode"] if old and os.name == "nt" else item.mode
            if old and old["sha"] == new_sha and old["mode"] == target_mode:
                continue
            previous = None
            if old and old["sha"] != new_sha and old["size"] <= MAX_DIFF_FILE_BYTES:
                if old["sha"] not in blob_cache:
                    # Tiny files must still be checked for LFS pointers even
                    # when the text-preview download budget is exhausted.
                    if downloaded + old["size"] <= MAX_REVIEW_DOWNLOAD_BYTES or old["size"] <= 1024:
                        blob_cache[old["sha"]] = _remote_blob(client, runner, destination, old, limit=MAX_DIFF_FILE_BYTES)
                        downloaded += old["size"]
                previous = blob_cache.get(old["sha"])
                if previous is not None and previous.startswith(_LFS_POINTER):
                    raise PublishError(f"GitHub stores {item.path} in Git LFS. Studio cannot replace its LFS object; leave it unselected.")
            changes.append(ReviewedChange(item.path, target_mode, new_sha, old["sha"] if old else "", old["mode"] if old else ""))
            status = "Modified" if old else "Added"
            summaries.append(f"{status}: {item.path}  |  {old['size'] if old else 0} → {item.size} bytes  |  mode {target_mode}\n  SHA256 {item.sha256}")
            if preview_bytes < MAX_DIFF_BYTES:
                preview = _file_diff(replace(item, mode=target_mode), data, old, previous, new_sha, MAX_DIFF_BYTES - preview_bytes)
                previews.append(preview)
                preview_bytes += len(preview.encode("utf-8"))
        if not changes:
            raise PublishError("The selected files already match GitHub. There are no changes to publish.")
        if preview_bytes >= MAX_DIFF_BYTES:
            previews.append("\n[The text preview is limited to 2 MB. Every changed file, exact SHA256, size and mode appears in the change summary.]\n")
        summary = f"{len(changes)} file(s) changed; {len(files) - len(changes)} selected file(s) already match. Files not selected remain on GitHub.\n\n" + "\n".join(summaries)
        if empty:
            initial_branch = branch if new_repository else default_branch
            summary = (
                f"Empty repository: initialize {initial_branch} with the reviewed file {bootstrap_file}, "
                f"then publish the complete snapshot to {branch}. If interrupted after initialization, "
                "the initial file commit may remain; review the repository before retrying.\n\n" + summary
            )
        plan = PreparedPublish(
            root, destination, account["login"], visibility if new_repository else ("private" if metadata["private"] else "public"),
            new_repository, branch, base_branch, "", files, "\n".join(previews), summary, working, temporary,
            message=message.strip(), account_id=account["id"], repository_id=repository_id,
            owner_id=owner_id, owner_type=owner_type, default_branch=default_branch,
            base_revision=base_revision, base_tree=base_tree, empty_repository=empty,
            bootstrap_file=bootstrap_file, changes=tuple(changes),
            expected_tree=_tree_sha(entries, changes),
        )
        manifest = _manifest_bytes(plan)
        plan = replace(plan, revision=hashlib.sha256(manifest).hexdigest())
        (working.parent / "review.json").write_bytes(manifest)
        runner.log("Ready for review. Nothing has been published to GitHub yet.")
        return plan
    except Exception:
        temporary.cleanup()
        raise


def _confirm_account(client, runner: CommandRunner, plan: PreparedPublish) -> dict:
    account = _authenticated_account(client, runner)
    if account["login"] != plan.account or account["id"] != plan.account_id:
        raise PublishError("The signed-in GitHub account changed. Review the publication again before publishing.")
    return account


def _confirm_existing(client, runner: CommandRunner, plan: PreparedPublish) -> None:
    metadata = _repository_metadata(client, runner, plan.repository)
    assert metadata is not None
    if metadata["id"] != plan.repository_id:
        raise PublishError("The GitHub destination changed or the repository was recreated. Review the publication again.")
    if ("private" if metadata["private"] else "public") != plan.visibility:
        raise PublishError("The GitHub repository visibility changed. Review again before sharing the selected files.")
    if metadata.get("default_branch", "main") != plan.default_branch:
        raise PublishError("The GitHub default branch changed. Review the publication again.")
    current = _default_head(client, runner, plan.repository, plan.default_branch)
    if current != plan.base_revision:
        raise PublishError("The GitHub default branch changed since review. Review the latest files again before publishing.")


def _branch_absent(client, runner: CommandRunner, destination: str, branch: str) -> None:
    reference = _request(client, runner, "GET", _repo_path(destination) + "/git/ref/heads/" + quote(branch, safe=""), missing=True)
    if reference is not None:
        raise PublishError("The prepared update branch already exists. Review again to create a fresh branch.")


def _validate_created_repository(response, plan: PreparedPublish) -> int:
    if not isinstance(response, dict) or normalize_github_repository(str(response.get("full_name", ""))).lower() != plan.repository.lower() or type(response.get("id")) is not int or response["id"] <= 0 or response.get("private") is not (plan.visibility == "private"):
        raise PublishError("GitHub did not confirm the reviewed repository name and visibility. The repository may have been created; inspect it before retrying.")
    owner = response.get("owner", {})
    if not isinstance(owner, dict) or owner.get("id") != plan.owner_id:
        raise PublishError("GitHub did not confirm the reviewed repository owner. Inspect the repository before retrying.")
    if response.get("default_branch", "main") != plan.branch:
        raise PublishError("Your GitHub account or organization selected a different default branch for the new repository. No project files were uploaded. Choose Existing repository and review the newly created empty repository to use its actual default branch.")
    return response["id"]


def _bootstrap_empty(client, runner: CommandRunner, plan: PreparedPublish, *, repository_id: int) -> tuple[str, str]:
    branch = plan.branch if plan.new_repository else plan.default_branch
    item = next(item for item in plan.files if item.path == plan.bootstrap_file)
    data, mode = _read_regular_bytes(plan.working_path, item.path, MAX_FILE_BYTES)
    if mode != item.mode or hashlib.sha256(data).hexdigest() != item.sha256:
        raise PublishError("The reviewed snapshot changed. Prepare and review it again.")
    runner.log(f"Initializing the empty repository with {item.path}…")
    response = _request(client, runner, "PUT", _repo_path(plan.repository) + "/contents/" + quote(item.path, safe="/"), {
        "message": plan.message + "\n\nInitialize the empty repository with a reviewed file.",
        "content": base64.b64encode(data).decode("ascii"), "branch": branch,
    })
    if not isinstance(response, dict) or not isinstance(response.get("commit"), dict) or not isinstance(response.get("content"), dict):
        raise PublishError("GitHub did not confirm the initial file commit. Check the repository before retrying.")
    revision = _sha(response["commit"].get("sha"), "initial commit")
    if response["content"].get("sha") != _git_blob_sha(data) or response["content"].get("path") != item.path:
        raise PublishError("GitHub's initial file did not match the reviewed snapshot. Check the repository before retrying.")
    tree_sha, entries = _read_tree(client, runner, plan.repository, revision)
    actual_files = {path: entry for path, entry in entries.items() if entry["type"] != "tree"}
    if set(actual_files) != {item.path} or actual_files[item.path]["sha"] != _git_blob_sha(data) or actual_files[item.path]["mode"] != item.mode:
        raise PublishError("GitHub's initial commit contains unexpected files or modes. No complete snapshot was published; check the repository before retrying.")
    metadata = _repository_metadata(client, runner, plan.repository)
    if metadata is None or metadata["id"] != repository_id or ("private" if metadata["private"] else "public") != plan.visibility:
        raise PublishError("The GitHub destination or visibility changed during initialization. Review again.")
    if _default_head(client, runner, plan.repository, branch) != revision:
        raise PublishError("The initial GitHub branch changed during setup. Review it again before publishing.")
    return revision, tree_sha


def publish_to_github(plan: PreparedPublish, runner: CommandRunner | None = None, *, client=None) -> PublishResult:
    runner = runner or CommandRunner()
    client = _client_for(runner, client)
    runner.log("Confirming the reviewed snapshot and GitHub account…")
    _verify_snapshot(plan, runner)
    account = _confirm_account(client, runner, plan)
    if plan.new_repository:
        owner_id, owner_type = _owner_details(client, runner, plan.repository, account)
        if (owner_id, owner_type) != (plan.owner_id, plan.owner_type):
            raise PublishError("The GitHub destination owner changed. Review the publication again.")
        if _repository_metadata(client, runner, plan.repository, missing=True) is not None:
            raise PublishError("That GitHub repository already exists. Choose Existing repository and review it; this publication was stopped.")
    else:
        _confirm_existing(client, runner, plan)
        if not plan.empty_repository:
            _branch_absent(client, runner, plan.repository, plan.branch)
    created = creation_attempted = bootstrap_attempted = branch_attempted = False
    wrote_objects = False
    repository_id = plan.repository_id
    base_revision, base_tree = plan.base_revision, plan.base_tree
    workflow = any(change.path.startswith(".github/workflows/") for change in plan.changes)
    try:
        if plan.new_repository:
            runner.log(f"Creating {plan.visibility} repository {plan.repository}…")
            creation_attempted = True
            endpoint = "/user/repos" if plan.owner_type == "User" else "/orgs/" + quote(plan.repository.split("/", 1)[0], safe="") + "/repos"
            response = _request(client, runner, "POST", endpoint, {
                "name": plan.repository.split("/", 1)[1],
                "private": plan.visibility == "private", "auto_init": False,
            }, creating=True)
            created = True
            repository_id = _validate_created_repository(response, plan)
            # Confirm that creation did not initialize unreviewed files and that
            # another writer has not used the new destination in the meantime.
            branches = _request(client, runner, "GET", _repo_path(plan.repository) + "/branches?per_page=1")
            if branches != []:
                raise PublishError("The newly created repository is no longer empty. Review its files before publishing.")
        if plan.empty_repository:
            bootstrap_attempted = True
            base_revision, base_tree = _bootstrap_empty(client, runner, plan, repository_id=repository_id)
        prefix = _repo_path(plan.repository)
        tree_entries = []
        uploaded: set[str] = set()
        changes_by_path = {change.path: change for change in plan.changes}
        seed = next((item for item in plan.files if item.path == plan.bootstrap_file), None)
        seed_sha = _git_blob_sha(_read_regular_bytes(plan.working_path, seed.path, MAX_FILE_BYTES)[0]) if seed else ""
        for index, item in enumerate(plan.files, 1):
            change = changes_by_path.get(item.path)
            if change is None:
                continue
            runner.check_cancelled()
            runner.log(f"Uploading reviewed file {index}/{len(plan.files)}: {item.path}")
            data, mode = _read_regular_bytes(plan.working_path, item.path, MAX_FILE_BYTES)
            if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256 or mode != item.mode or _git_blob_sha(data) != change.sha:
                raise PublishError("The reviewed snapshot changed. Prepare and review it again.")
            if change.sha not in uploaded and change.sha not in {change.old_sha, seed_sha}:
                wrote_objects = True
                blob = _request(client, runner, "POST", prefix + "/git/blobs", {
                    "content": base64.b64encode(data).decode("ascii"), "encoding": "base64",
                }, workflow=workflow)
                if not isinstance(blob, dict) or blob.get("sha") != change.sha:
                    raise PublishError("GitHub did not confirm the exact reviewed file hash. The update branch was not published.")
                uploaded.add(change.sha)
            tree_entries.append({"path": item.path, "mode": change.mode, "type": "blob", "sha": change.sha})
        wrote_objects = True
        tree = _request(client, runner, "POST", prefix + "/git/trees", {"base_tree": base_tree, "tree": tree_entries}, workflow=workflow)
        if not isinstance(tree, dict):
            raise PublishError("GitHub did not confirm the new repository tree.")
        tree_sha = _sha(tree.get("sha"), "new tree")
        if tree_sha != plan.expected_tree:
            raise PublishError("GitHub's new tree did not match the exact reviewed snapshot and preserved files. No final branch update was sent.")
        # No unselected path is supplied to the tree endpoint. base_tree retains
        # everything else, and the new commit keeps the reviewed parent history.
        commit = _request(client, runner, "POST", prefix + "/git/commits", {
            "message": plan.message, "tree": tree_sha, "parents": [base_revision],
        }, workflow=workflow)
        if not isinstance(commit, dict):
            raise PublishError("GitHub did not confirm the new commit.")
        revision = _sha(commit.get("sha"), "new commit")
        if not isinstance(commit.get("tree"), dict) or commit["tree"].get("sha") != tree_sha:
            raise PublishError("GitHub's new commit did not match the reviewed tree.")
        parents = commit.get("parents")
        if not isinstance(parents, list) or [parent.get("sha") for parent in parents if isinstance(parent, dict)] != [base_revision]:
            raise PublishError("GitHub's new commit did not preserve the reviewed parent history.")
        _verify_snapshot(plan, runner)
        _confirm_account(client, runner, plan)
        if plan.empty_repository:
            metadata = _repository_metadata(client, runner, plan.repository)
            if metadata is None or metadata["id"] != repository_id or ("private" if metadata["private"] else "public") != plan.visibility:
                raise PublishError("The GitHub destination or visibility changed during upload. Review again.")
            initial_branch = plan.branch if plan.new_repository else plan.default_branch
            if _default_head(client, runner, plan.repository, initial_branch) != base_revision:
                raise PublishError("The initial GitHub branch changed during upload. Review the latest files before publishing.")
            if not plan.new_repository:
                _branch_absent(client, runner, plan.repository, plan.branch)
        else:
            _confirm_existing(client, runner, plan)
            _branch_absent(client, runner, plan.repository, plan.branch)
        runner.log(f"Publishing the reviewed snapshot to {plan.repository}, branch {plan.branch}…")
        branch_attempted = True
        if plan.new_repository:
            response = _request(client, runner, "PATCH", prefix + "/git/refs/heads/" + quote(plan.branch, safe=""), {"sha": revision, "force": False}, workflow=workflow)
        else:
            response = _request(client, runner, "POST", prefix + "/git/refs", {"ref": "refs/heads/" + plan.branch, "sha": revision}, workflow=workflow)
        if not isinstance(response, dict) or response.get("ref") != "refs/heads/" + plan.branch or not isinstance(response.get("object"), dict) or response["object"].get("sha") != revision:
            raise PublishError("GitHub did not confirm the final branch revision. Check the branch before retrying.")
    except PublishError as exc:
        notes: list[str] = []
        if created:
            notes.append(f"The repository was created at {plan.repository_url}, but the publication did not finish. Choose Existing repository after checking it.")
        elif creation_attempted:
            notes.append(f"GitHub repository creation failed or was interrupted. Check {plan.repository_url} before retrying; it may already exist.")
        if bootstrap_attempted:
            notes.append(f"The initial reviewed file ({plan.bootstrap_file}) may already have been committed. Check the repository before retrying.")
        if branch_attempted:
            notes.append(f"The update branch {plan.branch} may already have been published. Check that branch before retrying.")
        elif wrote_objects:
            notes.append("No final branch update was confirmed. Uploaded Git objects may remain." + (" The initial file commit may remain on the previously empty default branch." if bootstrap_attempted else " Studio did not change an existing default branch."))
        if notes:
            kind = PublishCancelled if isinstance(exc, PublishCancelled) else PublishError
            raise kind("\n".join(notes) + "\n\n" + str(exc)) from exc
        raise
    review_url = plan.repository_url + "/tree/" + quote(plan.branch, safe="")
    if plan.base_branch:
        review_url = plan.repository_url + "/compare/" + quote(plan.base_branch, safe="") + "..." + quote(plan.branch, safe="") + "?expand=1"
    runner.log("Published successfully: " + review_url)
    return PublishResult(plan.repository, plan.branch, revision, plan.repository_url, review_url, plan.new_repository)


def deployment_root(project_path: Path, relative: str) -> Path:
    value = relative.strip().replace("\\", "/") or "."
    if value != ".":
        _safe_relative(value)
    root = project_path.expanduser().resolve()
    target = root / value
    cursor = root
    for part in PurePosixPath(value).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PublishError("Select a frontend folder inside this project, without symbolic links.")
    try:
        target.resolve().relative_to(root)
    except ValueError as exc:
        raise PublishError("The frontend folder must be inside the selected project.") from exc
    if not target.is_dir():
        raise PublishError("The selected frontend folder does not exist in this project.")
    return target


def detect_vercel_compatibility(project_path: Path, relative: str = ".") -> DeploymentCompatibility:
    try:
        root = deployment_root(project_path, relative)
    except PublishError as exc:
        return DeploymentCompatibility(False, "Choose a frontend folder", str(exc))
    if any((root / name).is_file() for name in ("Dockerfile.vercel", "Containerfile.vercel")):
        return DeploymentCompatibility(True, "Container Runtime (Beta)", (
            "A Vercel container configuration was found. Publish its files to GitHub, then import this repository "
            "and root directory in Vercel. Container Runtime and WebSockets are beta features. Instances can scale "
            "down and connections can reconnect; a Streamlit session may reset. Review the deployment logs after Deploy."
        ))
    manifest_path = root / ".studio" / "project.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if isinstance(manifest, dict) and str(manifest.get("project_type", "")).lower() == "streamlit":
        return DeploymentCompatibility(False, "Streamlit app", (
            "Prepare a Dockerfile.vercel to run this Streamlit app with Vercel Container Runtime (Beta). "
            "Review and add the deployment files below, publish them to GitHub, then import the repository in Vercel. "
            "Container scaling and connection limits can reset a Streamlit session. Streamlit Community Cloud is also available."
        ))
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        package = {}
    if isinstance(package, dict):
        runtime = package.get("dependencies")
        development = package.get("devDependencies")
        deps = {**(runtime if isinstance(runtime, dict) else {}),
                **(development if isinstance(development, dict) else {})}
        frameworks = (("next", "Next.js"), ("@sveltejs/kit", "SvelteKit"), ("nuxt", "Nuxt"),
                      ("astro", "Astro"), ("vite", "Vite"), ("react-scripts", "Create React App"),
                      ("@remix-run/react", "Remix"))
        for key, label in frameworks:
            if key in deps:
                return DeploymentCompatibility(True, label + " frontend", (
                    f"A {label} project was found. Import its GitHub repository in Vercel, select this root directory, "
                    "and review the detected build command and environment variables before Deploy."
                ))
    if (root / "index.html").is_file():
        return DeploymentCompatibility(True, "Static website", (
            "An index.html entry point was found. In Vercel choose Other as the framework preset and use this root "
            "directory. Set the build command only if this website needs a build."
        ))
    return DeploymentCompatibility(False, "No supported frontend found", (
        "Choose a folder containing a static index.html or a supported frontend package.json. "
        "This workflow does not adapt arbitrary Python scripts into Vercel Functions."
    ))


def github_entry_url(repository: str, branch: str, entry_point: str) -> str:
    destination = normalize_github_repository(repository)
    _safe_relative(entry_point.replace("\\", "/"))
    if not branch.strip() or any(ord(c) < 32 for c in branch):
        raise PublishError("Enter the GitHub branch containing your app.")
    return f"https://github.com/{destination}/blob/{quote(branch, safe='')}/{quote(entry_point.replace(chr(92), '/'), safe='/')}"
