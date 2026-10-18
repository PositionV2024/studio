"""Read the repositories owned by Studio's connected GitHub account.

The list is limited by the connected token's repository access. It is a
destination picker, not authorization to upload: publishing still reviews the
selected repository and its permissions before sending any files.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from typing import Callable
from urllib.parse import urlencode

from .github_connection import GitHubError, GitHubSession, _SessionGitHubClient


PAGE_SIZE = 100
MAX_PAGES = 100
MAX_LIST_SECONDS = 180.0
_MAX_ID = (1 << 63) - 1
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_INVALID_RESPONSE = "GitHub returned unexpected repository details. Refresh the repository list and try again."
_CHANGED_LIST = "Your GitHub repository list changed while it was loading. Refresh the list and try again."


@dataclass(frozen=True)
class GitHubRepository:
    id: int
    full_name: str
    private: bool
    archived: bool = False
    disabled: bool = False
    can_push: bool | None = None

    @property
    def can_publish(self) -> bool:
        # An omitted push permission is not proof of write access. The publish
        # review checks permissions again for the user's selected destination.
        return not self.archived and not self.disabled and self.can_push is not False

    @property
    def unavailable_reason(self) -> str:
        if self.disabled:
            return "Disabled on GitHub."
        if self.archived:
            return "Archived. Unarchive this repository on GitHub to upload files."
        if self.can_push is False:
            return "Your connected account does not have write access to this repository."
        return ""


@dataclass(frozen=True)
class RepositoryList:
    account_id: int
    account_login: str
    repositories: tuple[GitHubRepository, ...]


def _positive_id(value) -> bool:
    return type(value) is int and 0 < value <= _MAX_ID


def _check_cancelled(cancelled: threading.Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise GitHubError("Loading GitHub repositories was cancelled.", cancelled=True)


def _check_state(client: _SessionGitHubClient, cancelled: threading.Event | None, deadline: float) -> None:
    _check_cancelled(cancelled)
    # Use the same generation guard as each session-bound HTTP request, also
    # while validating pages and after callbacks. This adds no network request
    # and does not retain or copy a credential into the result.
    client._session._check_generation(client._generation)
    if time.monotonic() >= deadline:
        raise GitHubError("Loading your GitHub repository list took too long. Refresh the list and try again.")


def _account_identity(account: dict) -> tuple[int, str]:
    if not isinstance(account, dict):
        raise GitHubError("GitHub could not verify your account. Connect again.")
    account_id, login = account.get("id"), account.get("login")
    if not _positive_id(account_id) or not isinstance(login, str) or not _LOGIN.fullmatch(login):
        raise GitHubError("GitHub could not verify your account. Connect again.")
    return account_id, login


def _repository(entry, account_id: int, account_login: str) -> tuple[GitHubRepository, bool]:
    if not isinstance(entry, dict):
        raise GitHubError(_INVALID_RESPONSE)
    repository_id = entry.get("id")
    name, full_name = entry.get("name"), entry.get("full_name")
    owner = entry.get("owner")
    if not _positive_id(repository_id) or not isinstance(owner, dict):
        raise GitHubError(_INVALID_RESPONSE)
    owner_id, owner_login, owner_type = owner.get("id"), owner.get("login"), owner.get("type")
    if (
        not _positive_id(owner_id)
        or not isinstance(owner_login, str)
        or not _LOGIN.fullmatch(owner_login)
        or not isinstance(owner_type, str)
        or owner_type not in {"User", "Organization"}
        or not isinstance(name, str)
        or not _NAME.fullmatch(name)
        or name in {".", ".."}
        or name.endswith(".")
        or not isinstance(full_name, str)
        or len(full_name) > 140
        or full_name.casefold() != f"{owner_login}/{name}".casefold()
    ):
        raise GitHubError(_INVALID_RESPONSE)
    private = entry.get("private")
    archived, disabled = entry.get("archived", False), entry.get("disabled", False)
    if any(type(value) is not bool for value in (private, archived, disabled)):
        raise GitHubError(_INVALID_RESPONSE)
    permissions = entry.get("permissions", {})
    if not isinstance(permissions, dict):
        raise GitHubError(_INVALID_RESPONSE)
    can_push = permissions.get("push")
    if "push" in permissions and type(can_push) is not bool:
        raise GitHubError(_INVALID_RESPONSE)

    # Never trust affiliation=owner alone: include a personal repository only
    # when both stable numeric ownership and the login identify this account.
    same_id = owner_id == account_id
    same_login = owner_login.casefold() == account_login.casefold()
    if same_id != same_login or (same_id and owner_type != "User"):
        raise GitHubError(_CHANGED_LIST)
    repository = GitHubRepository(repository_id, full_name, private, archived, disabled, can_push)
    return repository, same_id


def _same_repository(left: GitHubRepository, right: GitHubRepository) -> bool:
    return (
        left.id == right.id
        and left.full_name.casefold() == right.full_name.casefold()
        and left.private == right.private
        and left.archived == right.archived
        and left.disabled == right.disabled
        and left.can_push == right.can_push
    )


def list_owned_repositories(
    session: GitHubSession,
    *,
    cancelled: threading.Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> RepositoryList:
    """Return a complete, bounded list of accessible personal repositories.

    Runs synchronously for use in a worker. Only authenticated GET requests are
    made through Studio's existing session client. Repositories that cannot
    accept uploads stay in the result with an explanatory unavailable_reason.
    Any failed page, changed account, or exhausted bound fails the entire list;
    an incomplete result is never presented as the user's complete list.
    """
    _check_cancelled(cancelled)
    if session is None or not session.has_token:
        raise GitHubError("Connect your GitHub account in Studio to choose one of your repositories.")
    client = session.client(cancelled=cancelled)
    deadline = time.monotonic() + MAX_LIST_SECONDS

    def report(message: str) -> None:
        _check_state(client, cancelled, deadline)
        if progress is not None:
            progress(message)
        _check_state(client, cancelled, deadline)

    report("Verifying your connected GitHub account…")
    account_id, account_login = _account_identity(client.get_authenticated_user())
    _check_state(client, cancelled, deadline)
    by_id: dict[int, GitHubRepository] = {}
    names: dict[str, int] = {}
    owned_ids: set[int] = set()
    full_pages: set[tuple[tuple[int, str], ...]] = set()

    for page_number in range(1, MAX_PAGES + 1):
        report(f"Loading your GitHub repositories (page {page_number})…")
        # GitHub disallows combining `type` with affiliation or visibility.
        # Advance bounded numeric pages instead of following a server URL.
        query = urlencode({
            "affiliation": "owner",
            "visibility": "all",
            "sort": "full_name",
            "direction": "asc",
            "per_page": PAGE_SIZE,
            "page": page_number,
        })
        payload = client.request("GET", "/user/repos?" + query)
        _check_state(client, cancelled, deadline)
        if not isinstance(payload, list) or len(payload) > PAGE_SIZE:
            raise GitHubError(_INVALID_RESPONSE)
        page_identity = []
        for entry in payload:
            _check_state(client, cancelled, deadline)
            repository, owned = _repository(entry, account_id, account_login)
            # Track identities from every owner so a contradictory duplicate
            # cannot be hidden by filtering organization/collaborator entries.
            page_identity.append((repository.id, repository.full_name.casefold()))
            key = repository.full_name.casefold()
            if key in names and names[key] != repository.id:
                raise GitHubError(_CHANGED_LIST)
            previous = by_id.get(repository.id)
            if previous is not None and not _same_repository(previous, repository):
                raise GitHubError(_CHANGED_LIST)
            if previous is None:
                by_id[repository.id] = repository
                names[key] = repository.id
            if owned:
                owned_ids.add(repository.id)
        if len(payload) < PAGE_SIZE:
            repositories = tuple(sorted(
                (by_id[repository_id] for repository_id in owned_ids),
                key=lambda item: (item.full_name.casefold(), item.id),
            ))
            report(f"Loaded {len(repositories)} of your GitHub repositories.")
            return RepositoryList(account_id, account_login, repositories)
        signature = tuple(page_identity)
        if signature in full_pages:
            raise GitHubError(_CHANGED_LIST)
        full_pages.add(signature)

    raise GitHubError(
        "Your GitHub repository list is too large to load completely. "
        "Choose fewer repositories for your token, reconnect and refresh the list."
    )
