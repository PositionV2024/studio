"""Owned-repository discovery through the real, offline HTTPS/session boundary."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from email.message import Message
import json
import ssl
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from studio import github_connection as connection
from studio import github_repositories as discovery
from studio.github_connection import GitHubError, GitHubSession
from studio.github_repositories import GitHubRepository, RepositoryList, list_owned_repositories


TOKEN = "offline_repository_picker_test_token"
OTHER_TOKEN = "another_offline_repository_picker_token"
ACCOUNT = {"login": "clarence", "id": 123, "name": "Clarence"}


class MemoryStore:
    available = False

    def __init__(self, token=TOKEN):
        self.token = token
        self.deleted = 0

    def load(self):
        return self.token

    def delete(self):
        self.deleted += 1
        self.token = None


class Socket:
    def shutdown(self, _how):
        pass


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)
        self.body = json.dumps(payload).encode("utf-8")
        self.position = 0
        self.closed = False

    def read(self, size):
        data = self.body[self.position:self.position + size]
        self.position += len(data)
        return data

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls
        self.sock = None
        self.closed = False
        self.on_response = None
        self.on_connect = None

    def set_debuglevel(self, value):
        assert value == 0

    def connect(self):
        if self.on_connect:
            self.on_connect()
        self.sock = Socket()

    def request(self, method, path, *, body, headers):
        assert self.sock is not None
        self.calls.append((method, path, body, dict(headers)))

    def getresponse(self):
        if self.on_response:
            self.on_response()
        return self.response

    def close(self):
        self.closed = True
        self.sock = None


@pytest.fixture
def api(monkeypatch):
    queued = []
    calls = []
    created = []

    def add(payload, *, status=200, headers=None):
        item = Connection(Response(payload, status, headers), calls)
        queued.append(item)
        return item

    def factory(host, *, port, timeout, context):
        assert host == "api.github.com" and port == 443
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert queued, "Unexpected network request in an offline test"
        item = queued.pop(0)
        created.append(item)
        return item

    monkeypatch.setattr(connection.http.client, "HTTPSConnection", factory)
    add.calls = calls
    add.created = created
    add.queued = queued
    return add


def repository(number, **overrides):
    name = overrides.pop("name", f"repo-{number:04d}")
    owner = overrides.pop("owner", {"id": 123, "login": "clarence", "type": "User"})
    return {
        "id": number,
        "name": name,
        "full_name": f"{owner['login']}/{name}",
        "owner": owner,
        "private": False,
        **overrides,
    }


def session(token=TOKEN):
    return GitHubSession(store=MemoryStore(token))


def test_restored_account_lists_public_private_and_unavailable_owned_destinations(api):
    account = session()
    assert account.account is None
    api(ACCOUNT)
    api([
        repository(7, name="write-denied", permissions={"push": False}),
        repository(3, name="private-project", private=True, permissions={"push": True}),
        repository(1, name="public-project"),
        repository(4, name="archived-project", archived=True),
        repository(5, name="disabled-project", disabled=True),
        repository(6, name="shared-project", owner={"id": 321, "login": "coworker", "type": "User"}),
        repository(8, name="organization-project", owner={"id": 456, "login": "company", "type": "Organization"}),
    ])
    messages = []
    result = list_owned_repositories(account, progress=messages.append)

    assert isinstance(result, RepositoryList)
    assert result.account_id == 123 and result.account_login == "clarence"
    assert account.account["login"] == "clarence"
    assert [item.full_name for item in result.repositories] == [
        "clarence/archived-project", "clarence/disabled-project", "clarence/private-project",
        "clarence/public-project", "clarence/write-denied",
    ]
    archived, disabled, private, public, denied = result.repositories
    assert not archived.can_publish and "Archived" in archived.unavailable_reason
    assert not disabled.can_publish and "Disabled" in disabled.unavailable_reason
    assert private.private and private.can_publish and private.unavailable_reason == ""
    assert public.can_push is None and public.can_publish and public.unavailable_reason == ""
    assert not denied.can_publish and "write access" in denied.unavailable_reason
    assert len(api.calls) == 2 and api.calls[0][1] == "/user"
    assert messages[-1] == "Loaded 5 of your GitHub repositories."
    assert TOKEN not in repr(result) and TOKEN not in " ".join(messages)


def test_more_than_one_hundred_repositories_are_all_read_without_cli_or_ambient_tokens(api, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", OTHER_TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", OTHER_TOKEN)
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.example.invalid")

    def no_program(*_args, **_kwargs):
        pytest.fail("Repository discovery must not launch a program")

    monkeypatch.setattr(subprocess, "run", no_program)
    monkeypatch.setattr(subprocess, "Popen", no_program)
    api(ACCOUNT)
    entries = [repository(index) for index in range(1, 206)]
    for offset in (0, 100, 200):
        api(entries[offset:offset + 100])
    result = list_owned_repositories(session())
    assert len(result.repositories) == 205
    assert [item.id for item in result.repositories] == list(range(1, 206))
    assert len(api.calls) == 4
    for page, (method, path, body, headers) in enumerate(api.calls[1:], 1):
        assert method == "GET" and body is None
        parsed = urlsplit(path)
        assert parsed.path == "/user/repos" and not parsed.netloc
        assert parse_qs(parsed.query) == {
            "affiliation": ["owner"], "visibility": ["all"], "sort": ["full_name"],
            "direction": ["asc"], "per_page": ["100"], "page": [str(page)],
        }
        assert headers["Authorization"] == "Bearer " + TOKEN
        assert headers["X-GitHub-Api-Version"] == connection.GITHUB_API_VERSION
        assert TOKEN not in path and OTHER_TOKEN not in str(headers)
    assert all(item.closed and item.response.closed for item in api.created)


def test_exact_page_size_reads_an_empty_next_page_before_returning(api):
    api(ACCOUNT)
    api([repository(index) for index in range(1, 101)])
    api([])
    result = list_owned_repositories(session())
    assert len(result.repositories) == 100 and len(api.calls) == 3


@pytest.mark.parametrize("entries", [[], [repository(10, private=True)]])
def test_token_repository_selection_is_respected_without_other_account_lookups(api, entries):
    api(ACCOUNT)
    api(entries)
    result = list_owned_repositories(session())
    assert [item.id for item in result.repositories] == [item["id"] for item in entries]
    assert len(api.calls) == 2
    assert all(path.startswith(("/user?", "/user/repos?")) or path == "/user" for _, path, _, _ in api.calls)


@pytest.mark.parametrize("connected", [None, False])
def test_missing_connection_stops_before_network(api, connected):
    with pytest.raises(GitHubError, match="Connect your GitHub account"):
        list_owned_repositories(None if connected is None else session(None))
    assert api.calls == [] and api.created == []


def test_cancel_before_authentication_stops_before_network(api):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(GitHubError) as caught:
        list_owned_repositories(session(), cancelled=cancelled)
    assert caught.value.cancelled and api.calls == [] and api.created == []


@pytest.mark.parametrize("stage", ["account", "list", "later-page"])
def test_unauthorized_page_invalidates_saved_connection_and_never_returns_partial_list(api, stage):
    account = session()
    if stage != "account":
        api(ACCOUNT)
    if stage == "later-page":
        api([repository(index) for index in range(1, 101)])
    api({"message": TOKEN}, status=401)
    with pytest.raises(GitHubError) as caught:
        list_owned_repositories(account)
    assert caught.value.status == 401 and TOKEN not in str(caught.value)
    assert not account.has_token and account.account is None
    assert account._store.token is None and account._store.deleted == 1


@pytest.mark.parametrize("status, headers, expected", [
    (403, {}, "denied access"),
    (403, {"X-RateLimit-Remaining": "0"}, "request limit"),
    (429, {}, "request limit"),
    (404, {}, "does not have access"),
    (500, {}, "temporarily unavailable"),
    (302, {"Location": "https://untrusted.invalid/" + TOKEN}, "redirected"),
])
def test_api_failures_are_safe_and_do_not_become_empty_success(api, status, headers, expected):
    api(ACCOUNT)
    api({"message": TOKEN, "html_url": "https://untrusted.invalid/" + TOKEN}, status=status, headers=headers)
    with pytest.raises(GitHubError, match=expected) as caught:
        list_owned_repositories(session())
    assert caught.value.status == status and TOKEN not in str(caught.value)
    assert len(api.calls) == 2 and len(api.created) == 2


def test_transport_exception_does_not_expose_connection_data(api):
    item = api(ACCOUNT)

    def fail():
        raise OSError(TOKEN)

    item.on_connect = fail
    with pytest.raises(GitHubError, match="Check your connection") as caught:
        list_owned_repositories(session())
    assert TOKEN not in str(caught.value)
    assert api.calls == [] and item.closed


@pytest.mark.parametrize("stage", ["account", "first-page", "later-page"])
def test_disconnecting_during_a_request_rejects_result(api, stage):
    account = session()
    account_request = api(ACCOUNT)
    first_page = api([repository(index) for index in range(1, 101)] if stage == "later-page" else [])
    if stage == "later-page":
        item = api([])
    else:
        item = account_request if stage == "account" else first_page
    item.on_response = account.disconnect
    with pytest.raises(GitHubError) as caught:
        list_owned_repositories(account)
    assert caught.value.cancelled and not account.has_token


def test_same_account_token_rotation_invalidates_inflight_list(api):
    account = session()
    api(ACCOUNT)
    item = api([repository(1)])
    api(ACCOUNT)  # Explicit replacement-token sign-in, triggered while the list returns.
    item.on_response = lambda: account.connect(OTHER_TOKEN)
    with pytest.raises(GitHubError) as caught:
        list_owned_repositories(account)
    assert caught.value.cancelled and account.get_token() == OTHER_TOKEN
    assert account.account["id"] == 123
    assert api.calls[-1][3]["Authorization"] == "Bearer " + OTHER_TOKEN


@pytest.mark.parametrize("action", ["disconnect", "cancel"])
def test_final_callback_cannot_deliver_a_stale_or_cancelled_result(api, action):
    account = session()
    cancelled = threading.Event()
    api(ACCOUNT)
    api([repository(1)])

    def progress(message):
        if message.startswith("Loaded "):
            account.disconnect() if action == "disconnect" else cancelled.set()

    with pytest.raises(GitHubError) as caught:
        list_owned_repositories(account, cancelled=cancelled, progress=progress)
    assert caught.value.cancelled and len(api.calls) == 2


def test_cancellation_between_pages_does_not_send_another_request(api):
    cancelled = threading.Event()
    api(ACCOUNT)
    api([repository(index) for index in range(1, 101)])

    def progress(message):
        if "page 2" in message:
            cancelled.set()

    with pytest.raises(GitHubError) as caught:
        list_owned_repositories(session(), cancelled=cancelled, progress=progress)
    assert caught.value.cancelled and len(api.calls) == 2


@pytest.mark.parametrize("account", [
    [], {}, {"id": True, "login": "clarence"}, {"id": 0, "login": "clarence"},
    {"id": -1, "login": "clarence"}, {"id": 2 ** 63, "login": "clarence"},
    {"id": 123, "login": "bad/owner"}, {"id": 123, "login": TOKEN},
])
def test_unverified_or_malformed_account_cannot_enumerate_repositories(api, account):
    api(account)
    with pytest.raises(GitHubError, match="verify") as caught:
        list_owned_repositories(session())
    assert TOKEN not in str(caught.value) and len(api.calls) == 1


@pytest.mark.parametrize("path, value", [
    ("id", True), ("id", "12"), ("id", 0), ("id", -1), ("id", 2 ** 63),
    ("name", None), ("name", ""), ("name", "a" * 101), ("name", "."), ("name", ".."),
    ("name", "example."), ("name", "bad/name"), ("name", "bad\\name"),
    ("name", "bad?token=" + TOKEN), ("name", "bad#name"), ("name", "bad\nname"),
    ("name", "<b>name</b>"), ("name", "café"),
    ("full_name", "different/repo-0001"), ("full_name", "clarence/different"),
    ("full_name", "https://github.com/clarence/repo-0001"), ("full_name", None),
    ("private", None), ("private", 0), ("private", "false"),
    ("archived", None), ("archived", "false"), ("disabled", 1),
    ("owner", None), ("owner", []), ("owner.id", True), ("owner.id", 0),
    ("owner.login", None), ("owner.login", "bad/owner"), ("owner.login", "_owner"),
    ("owner.type", None), ("owner.type", []), ("owner.type", "unknown"),
    ("permissions", None), ("permissions", []), ("permissions.push", None),
    ("permissions.push", 1), ("permissions.push", "false"),
])
def test_malformed_repository_details_are_rejected_with_a_safe_error(api, path, value):
    entry = repository(1, permissions={"push": True})
    target = entry
    pieces = path.split(".")
    for piece in pieces[:-1]:
        target = target[piece]
    target[pieces[-1]] = value
    api(ACCOUNT)
    api([entry])
    with pytest.raises(GitHubError, match="unexpected repository details") as caught:
        list_owned_repositories(session())
    assert TOKEN not in str(caught.value) and len(api.calls) == 2


@pytest.mark.parametrize("key", ["id", "name", "full_name", "owner", "private"])
def test_missing_required_repository_details_are_not_silently_skipped(api, key):
    entry = repository(1)
    del entry[key]
    api(ACCOUNT)
    api([entry])
    with pytest.raises(GitHubError, match="unexpected repository details"):
        list_owned_repositories(session())


@pytest.mark.parametrize("payload", [{}, {"items": []}, [None], ["a/repository"], [7]])
def test_unexpected_page_shapes_are_not_an_empty_list(api, payload):
    api(ACCOUNT)
    api(payload)
    with pytest.raises(GitHubError, match="unexpected repository details"):
        list_owned_repositories(session())


@pytest.mark.parametrize("owner", [
    {"id": 999, "login": "clarence", "type": "User"},
    {"id": 123, "login": "someone-else", "type": "User"},
    {"id": 123, "login": "clarence", "type": "Organization"},
])
def test_matching_only_one_owner_identity_never_selects_a_repository(api, owner):
    api(ACCOUNT)
    api([repository(1, owner=owner)])
    with pytest.raises(GitHubError, match="changed while it was loading"):
        list_owned_repositories(session())


@pytest.mark.parametrize("name", [".github", "_example", "-example", "a_b.c-d", "a" * 100])
def test_safe_github_repository_names_are_preserved_without_rewriting(api, name):
    api(ACCOUNT)
    api([repository(1, name=name, owner={"id": 123, "login": "Clarence", "type": "User"})])
    result = list_owned_repositories(session())
    assert result.repositories[0].full_name == "Clarence/" + name


def test_consistent_duplicates_are_deduplicated_across_pages_and_case(api):
    api(ACCOUNT)
    entries = [repository(index) for index in range(1, 101)]
    duplicate = deepcopy(entries[-1])
    duplicate["full_name"] = "Clarence/REPO-0100"
    duplicate["name"] = "REPO-0100"
    api(entries)
    api([duplicate, repository(101), deepcopy(duplicate)])
    result = list_owned_repositories(session())
    assert len(result.repositories) == 101
    assert result.repositories[99].full_name == "clarence/repo-0100"


@pytest.mark.parametrize("changes", [
    {"name": "renamed"}, {"private": True}, {"archived": True},
    {"disabled": True}, {"permissions": {"push": False}},
])
def test_changed_duplicate_identity_or_permission_requires_a_fresh_list(api, changes):
    api(ACCOUNT)
    api([repository(1), repository(1, **changes)])
    with pytest.raises(GitHubError, match="changed while it was loading"):
        list_owned_repositories(session())


def test_same_name_cannot_refer_to_different_repository_ids(api):
    api(ACCOUNT)
    api([repository(1, name="project"), repository(2, name="PROJECT")])
    with pytest.raises(GitHubError, match="changed while it was loading"):
        list_owned_repositories(session())


@pytest.mark.parametrize("owner_type", ["User", "Organization"])
def test_filtering_another_owner_cannot_hide_a_conflicting_repository_identity(api, owner_type):
    api(ACCOUNT)
    api([
        repository(1),
        repository(1, owner={"id": 999, "login": "another-owner", "type": owner_type}),
    ])
    with pytest.raises(GitHubError, match="changed while it was loading"):
        list_owned_repositories(session())


def test_repeated_full_page_cannot_loop_or_appear_complete(api):
    api(ACCOUNT)
    entries = [repository(index) for index in range(1, 101)]
    api(entries)
    api(entries)
    with pytest.raises(GitHubError, match="changed while it was loading"):
        list_owned_repositories(session())
    assert len(api.calls) == 3


def test_pagination_bound_fails_instead_of_showing_a_truncated_repository_list(api, monkeypatch):
    monkeypatch.setattr(discovery, "MAX_PAGES", 2)
    api(ACCOUNT)
    api([repository(index) for index in range(1, 101)])
    api([repository(index) for index in range(101, 201)])
    with pytest.raises(GitHubError, match="too large to load completely"):
        list_owned_repositories(session())
    assert len(api.calls) == 3


def test_oversized_page_is_rejected_instead_of_truncated(api):
    api(ACCOUNT)
    api([repository(index) for index in range(1, 102)])
    with pytest.raises(GitHubError, match="unexpected repository details"):
        list_owned_repositories(session())


def test_overall_deadline_stops_before_an_additional_page(api, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(discovery.time, "monotonic", lambda: clock[0])
    api(ACCOUNT)
    api([repository(index) for index in range(1, 101)])

    def progress(message):
        if "page 2" in message:
            clock[0] = discovery.MAX_LIST_SECONDS

    with pytest.raises(GitHubError, match="took too long"):
        list_owned_repositories(session(), progress=progress)
    assert len(api.calls) == 2


def test_result_models_are_immutable_and_do_not_hold_authentication_state():
    repository_item = GitHubRepository(1, "clarence/project", False)
    result = RepositoryList(123, "clarence", (repository_item,))
    with pytest.raises(FrozenInstanceError):
        repository_item.full_name = "someone/different"
    with pytest.raises(FrozenInstanceError):
        result.account_login = "someone"
    assert set(repository_item.__dict__) == {"id", "full_name", "private", "archived", "disabled", "can_push"}
    assert set(result.__dict__) == {"account_id", "account_login", "repositories"}
