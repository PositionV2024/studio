"""Offline HTTPS and native credential-boundary tests; no real account tokens."""
from __future__ import annotations

import ctypes
import http.client
import json
import socket
import ssl
import threading
import traceback
from email.message import Message
from urllib.parse import parse_qs, urlsplit

import pytest

from studio import github_connection as github
from studio.github_connection import GitHubClient, GitHubError, GitHubSession, WindowsCredentialStore


TOKEN = "offline_test_token_for_studio_validation"
OTHER_TOKEN = "another_offline_test_token_for_studio"
ACCOUNT = {"login": "clarence", "id": 123, "name": "Clarence", "html_url": "https://github.com/clarence"}


class FakeSocket:
    def __init__(self):
        self.stopped = threading.Event()

    def shutdown(self, _how):
        self.stopped.set()


class FakeResponse:
    def __init__(self, payload=ACCOUNT, *, status=200, headers=None, raw=None):
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)
        self.body = json.dumps(payload).encode("utf-8") if raw is None else raw
        self.offset = 0
        self.read_calls = 0
        self.closed = False

    def read(self, size):
        self.read_calls += 1
        output = self.body[self.offset:self.offset + size]
        self.offset += len(output)
        return output

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.socket = FakeSocket()
        self.sock = None
        self.calls = []
        self.closed = False
        self.debug = None
        self.connect_action = None
        self.response_action = None
        self.upload_sizes = []

    def set_debuglevel(self, value):
        self.debug = value

    def connect(self):
        if self.connect_action:
            self.connect_action()
        self.sock = self.socket

    def request(self, method, path, *, body, headers):
        if self.sock is None:
            raise http.client.NotConnected()
        if body is not None and not isinstance(body, bytes):
            pieces = []
            for part in body:
                self.upload_sizes.append(len(part))
                pieces.append(part)
            body = b"".join(pieces)
        self.calls.append((method, path, body, dict(headers)))

    def getresponse(self):
        if self.response_action:
            return self.response_action()
        return self.response

    def close(self):
        self.closed = True
        self.sock = None


@pytest.fixture
def transport(monkeypatch):
    created = []
    ready = []

    def install(response=None):
        connection = FakeConnection(response)
        ready.append(connection)
        return connection

    def connection_factory(*args, **kwargs):
        created.append((args, kwargs))
        assert ready, "A test attempted an unexpected network connection"
        return ready.pop(0)

    monkeypatch.setattr(github.http.client, "HTTPSConnection", connection_factory)
    install.created = created
    return install


def test_authentication_uses_fixed_verified_https_without_ambient_credentials(transport, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", OTHER_TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", OTHER_TOKEN)
    monkeypatch.setenv("HTTPS_PROXY", "https://untrusted.example.invalid")
    connection = transport(FakeResponse({**ACCOUNT, "private_extra": "not needed"}))
    messages = []
    client = GitHubClient("  " + TOKEN + "\n", log=messages.append)
    assert client.get_authenticated_user() == ACCOUNT
    args, kwargs = transport.created[0]
    assert args == ("api.github.com",)
    assert kwargs["port"] == 443 and kwargs["timeout"] > 0
    assert kwargs["context"].check_hostname
    assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
    method, path, body, headers = connection.calls[0]
    assert (method, path, body) == ("GET", "/user", None)
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert "Cookie" not in headers and "Proxy-Authorization" not in headers
    assert connection.debug == 0 and connection.auto_open == 0
    assert TOKEN not in repr(client) + "\n".join(messages)
    assert messages == []  # Higher-level upload stages remain visible.
    assert OTHER_TOKEN not in repr(connection.calls)
    assert connection.closed and connection.response.closed


def test_post_json_and_empty_success_are_supported(transport):
    connection = transport(FakeResponse(status=204, raw=b""))
    assert GitHubClient(TOKEN).request("post", "/repos/clarence/project/git/refs", {"name": "日本語"}) == {}
    assert json.loads(connection.calls[0][2]) == {"name": "日本語"}
    assert connection.calls[0][3]["Content-Type"].startswith("application/json")


def test_large_upload_has_content_length_and_bounded_cancellable_pieces(transport, monkeypatch):
    monkeypatch.setattr(github, "UPLOAD_CHUNK_BYTES", 32)
    connection = transport(FakeResponse({"sha": "a" * 40}))
    data = {"content": "x" * 200, "encoding": "base64"}
    GitHubClient(TOKEN).request("POST", "/repos/clarence/project/git/blobs", data)
    sent = connection.calls[0]
    assert len(connection.upload_sizes) > 1 and max(connection.upload_sizes) <= 32
    assert json.loads(sent[2]) == data
    assert int(sent[3]["Content-Length"]) == len(sent[2])
    assert "Transfer-Encoding" not in sent[3]


def test_cancel_stops_large_upload_between_pieces(transport, monkeypatch):
    monkeypatch.setattr(github, "UPLOAD_CHUNK_BYTES", 32)
    connection = transport()
    cancelled = threading.Event()
    received = []

    def receive(_method, _path, *, body, headers):
        for part in body:
            received.append(bytes(part))
            cancelled.set()

    connection.request = receive
    with pytest.raises(GitHubError) as caught:
        GitHubClient(TOKEN, cancelled=cancelled).request("POST", "/user/repos", {"description": "x" * 200})
    assert caught.value.cancelled and len(received) == 1
    assert connection.closed


@pytest.mark.parametrize("bad_token", ["", "password", "token with spaces " * 2, "x" * 2049, TOKEN + "\r\nInjected: value", None])
def test_invalid_tokens_do_not_enter_the_transport(bad_token, transport):
    with pytest.raises(GitHubError):
        GitHubClient(bad_token)
    assert transport.created == []


@pytest.mark.parametrize("path", [
    "https://github.com/user", "https://evil.example.invalid/user", "//evil.example.invalid/user",
    "/user#fragment", "/user\r\nAuthorization: value", "/user%0Ainjected", "/user\\escape",
    "/user?access_token=anything", "/user?AuThOrIzAtIoN=value", "/user?token=" + TOKEN,
    "/repos/" + TOKEN, "/" + "%6F" + TOKEN[1:],
])
def test_credentials_cannot_be_redirected_or_put_in_request_urls(path, transport):
    with pytest.raises(GitHubError) as caught:
        GitHubClient(TOKEN).request("GET", path)
    assert TOKEN not in str(caught.value)
    assert not transport.created


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 409, 410, 422, 429, 500])
def test_http_errors_never_disclose_the_response_body_or_follow_redirects(status, transport):
    connection = transport(FakeResponse({"message": TOKEN}, status=status, headers={"Location": "https://evil.invalid/" + TOKEN}))
    with pytest.raises(GitHubError) as caught:
        GitHubClient(TOKEN).request("GET", "/user")
    assert caught.value.status == status
    assert TOKEN not in str(caught.value) + repr(caught.value)
    assert len(transport.created) == 1 and len(connection.calls) == 1
    assert connection.response.read_calls == 0


def test_secondary_rate_limit_is_actionable_without_echoing_header_values(transport):
    transport(FakeResponse(status=403, headers={"Retry-After": TOKEN}))
    with pytest.raises(GitHubError, match="request limit") as caught:
        GitHubClient(TOKEN).request("GET", "/user")
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("headers, raw", [({"Content-Length": "100"}, b"{}"), ({}, b"x" * 100)])
def test_response_size_is_bounded_before_or_during_read(transport, monkeypatch, headers, raw):
    monkeypatch.setattr(github, "MAX_RESPONSE_BYTES", 20)
    connection = transport(FakeResponse(headers=headers, raw=raw))
    with pytest.raises(GitHubError, match="too large"):
        GitHubClient(TOKEN).request("GET", "/user")
    assert connection.response.offset <= 21
    if headers:
        assert connection.response.read_calls == 0


def test_oversized_request_is_rejected_before_connecting(transport, monkeypatch):
    monkeypatch.setattr(github, "MAX_REQUEST_BYTES", 20)
    with pytest.raises(GitHubError, match="too large"):
        GitHubClient(TOKEN).request("POST", "/repos/clarence/project/git/blobs", {"content": "x" * 50})
    assert transport.created == []


@pytest.mark.parametrize("payload", [b"<html>" + TOKEN.encode() + b"</html>", b"true", b"null", b"\xff"])
def test_invalid_api_payloads_have_safe_errors(transport, payload):
    transport(FakeResponse(raw=payload))
    with pytest.raises(GitHubError) as caught:
        GitHubClient(TOKEN).request("GET", "/user")
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("payload", [[], {"login": "clarence"}, {"login": "<b>bad</b>", "id": 123}, {"login": "clarence", "id": True}, {"login": "clarence", "id": 0}])
def test_account_identity_requires_a_real_login_and_numeric_id(transport, payload):
    transport(FakeResponse(payload))
    with pytest.raises(GitHubError, match="verify"):
        GitHubClient(TOKEN).get_authenticated_user()


def test_profile_text_cannot_echo_token_and_profile_url_is_constructed(transport):
    transport(FakeResponse({**ACCOUNT, "name": TOKEN + "\x00 name", "html_url": "https://evil.invalid/" + TOKEN}))
    result = GitHubClient(TOKEN).get_authenticated_user()
    assert result["name"] == "[redacted] name"
    assert result["html_url"] == ACCOUNT["html_url"]


@pytest.mark.parametrize("failure", [OSError(TOKEN), ssl.SSLCertVerificationError(TOKEN), socket.timeout(TOKEN)])
def test_transport_exception_messages_and_chains_are_not_exposed(transport, failure):
    connection = transport()

    def fail():
        raise failure

    connection.connect_action = fail
    with pytest.raises(GitHubError) as caught:
        GitHubClient(TOKEN).request("GET", "/user")
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert connection.closed


def test_cancel_before_request_does_not_connect(transport):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(GitHubError) as caught:
        GitHubClient(TOKEN, cancelled=cancelled).request("POST", "/user/repos", {"name": "example"})
    assert caught.value.cancelled and not transport.created


def test_cancel_during_connection_does_not_send_the_request(transport):
    cancelled = threading.Event()
    connection = transport()
    connection.connect_action = cancelled.set
    with pytest.raises(GitHubError) as caught:
        GitHubClient(TOKEN, cancelled=cancelled).request("POST", "/user/repos", {"name": "example"})
    assert caught.value.cancelled and connection.calls == []
    assert connection.closed


def test_cancel_during_stalled_dns_returns_and_late_connection_sends_nothing(transport):
    cancelled, entered, release = threading.Event(), threading.Event(), threading.Event()
    connection = transport()

    def stalled_dns():
        entered.set()
        assert release.wait(2)

    connection.connect_action = stalled_dns
    errors = []

    def request():
        try:
            GitHubClient(TOKEN, cancelled=cancelled).request("POST", "/user/repos", {"name": "example"})
        except GitHubError as exc:
            errors.append(exc)

    worker = threading.Thread(target=request)
    worker.start()
    assert entered.wait(2)
    cancelled.set()
    worker.join(0.5)
    try:
        assert not worker.is_alive(), "Cancel remained blocked by a stalled DNS lookup"
        assert errors and errors[0].cancelled
        assert connection.calls == []
    finally:
        release.set()
        worker.join(2)
    assert connection.socket.stopped.wait(2)
    assert connection.calls == [] and connection.closed


def test_stalled_dns_has_a_deadline_and_late_socket_is_closed(transport, monkeypatch):
    monkeypatch.setattr(github, "SOCKET_TIMEOUT", 0.08)
    release = threading.Event()
    connection = transport()
    connection.connect_action = lambda: release.wait(2)
    try:
        with pytest.raises(GitHubError, match="timed out"):
            GitHubClient(TOKEN).request("GET", "/user")
        assert connection.calls == []
    finally:
        release.set()
    assert connection.socket.stopped.wait(2)
    assert connection.calls == []


def test_cancellation_interrupts_an_owned_inflight_socket(transport):
    cancelled = threading.Event()
    connection = transport()

    def waiting_response():
        cancelled.set()
        assert connection.socket.stopped.wait(2), "Cancellation did not interrupt the owned socket"
        raise OSError(TOKEN)

    connection.response_action = waiting_response
    with pytest.raises(GitHubError) as caught:
        GitHubClient(TOKEN, cancelled=cancelled).request("GET", "/user")
    assert caught.value.cancelled
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert connection.closed and len(connection.calls) == 1


def test_deadline_interrupts_an_owned_inflight_socket(transport, monkeypatch):
    monkeypatch.setattr(github, "REQUEST_TIMEOUT", 0.08)
    connection = transport()

    def waiting_response():
        assert connection.socket.stopped.wait(2), "Deadline did not interrupt the owned socket"
        raise OSError(TOKEN)

    connection.response_action = waiting_response
    with pytest.raises(GitHubError, match="timed out"):
        GitHubClient(TOKEN).request("GET", "/user")
    assert connection.closed


class MemoryStore:
    available = True

    def __init__(self, saved=None):
        self.saved = saved
        self.calls = []
        self.fail_save = False
        self.fail_delete = False
        self.on_save = None

    def load(self):
        self.calls.append("load")
        return self.saved

    def save(self, token, login):
        self.calls.append(("save", token, login))
        if self.fail_save:
            raise OSError(TOKEN)
        self.saved = token
        if self.on_save:
            self.on_save()

    def delete(self):
        self.calls.append("delete")
        if self.fail_delete:
            raise OSError(TOKEN)
        self.saved = None


def session_with_account(store=None, *, hook=None):
    created = []

    class Client:
        def __init__(self, token, **kwargs):
            created.append((token, kwargs))

        def get_authenticated_user(self):
            if hook:
                hook()
            return dict(ACCOUNT)

    return GitHubSession(store=store or MemoryStore(), client_factory=Client), created


def test_restored_credential_is_unverified_until_account_check():
    store = MemoryStore(TOKEN)
    session, clients = session_with_account(store)
    assert clients == [] and store.calls == ["load"]
    assert session.has_token and session.account is None and session.remembered
    assert session.validate() == ACCOUNT
    assert session.account == ACCOUNT and len(clients) == 1
    assert store.calls == ["load"]
    account_copy = session.account
    account_copy["login"] = "modified"
    assert session.account["login"] == "clarence"
    assert TOKEN not in repr(session)


def test_connect_validates_before_saving_and_removes_old_saved_account():
    store = MemoryStore(OTHER_TOKEN)

    def validate():
        assert store.saved == OTHER_TOKEN and store.calls == ["load"]

    session, clients = session_with_account(store, hook=validate)
    assert session.connect(TOKEN, remember=True) == ACCOUNT
    assert store.calls == ["load", "delete", ("save", TOKEN, "clarence")]
    assert session.has_token and session.remembered and session.get_token() == TOKEN
    assert session.storage_warning == "" and store.saved == TOKEN
    assert clients[0][0] == TOKEN


def test_session_only_connection_removes_a_previously_remembered_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = MemoryStore(OTHER_TOKEN)
    session, _ = session_with_account(store)
    session.connect(TOKEN, remember=False)
    assert store.saved is None and store.calls == ["load", "delete"]
    assert session.has_token and not session.remembered
    assert list(tmp_path.iterdir()) == []


def test_failed_validation_never_replaces_the_previous_connection():
    store = MemoryStore(OTHER_TOKEN)

    def rejected():
        raise GitHubError("Token rejected", status=401)

    session, _ = session_with_account(store, hook=rejected)
    with pytest.raises(GitHubError):
        session.connect(TOKEN, remember=True)
    assert store.saved == OTHER_TOKEN and store.calls == ["load"]
    assert session.get_token() == OTHER_TOKEN


def test_save_failure_clearly_falls_back_to_session_without_plaintext_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = MemoryStore(OTHER_TOKEN)
    store.fail_save = True
    session, _ = session_with_account(store)
    assert session.connect(TOKEN, remember=True) == ACCOUNT
    assert session.has_token and not session.remembered and store.saved is None
    assert "this session only" in session.storage_warning
    assert TOKEN not in session.storage_warning
    assert list(tmp_path.iterdir()) == []


def test_non_windows_connection_is_session_only():
    store = MemoryStore()
    store.available = False
    session, _ = session_with_account(store)
    session.connect(TOKEN, remember=True)
    assert session.has_token and not session.storage_available and not session.remembered
    assert "this session only" in session.storage_warning
    assert store.calls == ["load"]


def test_cannot_silently_retain_an_old_saved_account_after_delete_failure():
    store = MemoryStore(OTHER_TOKEN)
    store.fail_delete = True
    session, _ = session_with_account(store)
    with pytest.raises(GitHubError, match="previous saved") as caught:
        session.connect(TOKEN, remember=False)
    assert TOKEN not in str(caught.value)
    assert store.saved == OTHER_TOKEN
    assert not any(isinstance(call, tuple) and call[0] == "save" for call in store.calls)


def test_cancelled_validation_cannot_persist_the_new_token():
    cancelled = threading.Event()
    store = MemoryStore()
    session, _ = session_with_account(store, hook=cancelled.set)
    with pytest.raises(GitHubError) as caught:
        session.connect(TOKEN, remember=True, cancelled=cancelled)
    assert caught.value.cancelled
    assert store.calls == ["load"] and not session.has_token


def test_cancellation_at_the_native_save_boundary_removes_the_credential():
    cancelled = threading.Event()
    store = MemoryStore()
    store.on_save = cancelled.set
    session, _ = session_with_account(store)
    with pytest.raises(GitHubError) as caught:
        session.connect(TOKEN, remember=True, cancelled=cancelled)
    assert caught.value.cancelled
    assert store.saved is None and not session.has_token and not session.remembered


def test_disconnect_during_validation_invalidates_the_inflight_connection():
    entered, release = threading.Event(), threading.Event()
    store = MemoryStore()

    def block():
        entered.set()
        assert release.wait(2)

    session, _ = session_with_account(store, hook=block)
    errors = []

    def connect():
        try:
            session.connect(TOKEN, remember=True)
        except GitHubError as exc:
            errors.append(exc)

    worker = threading.Thread(target=connect)
    worker.start()
    assert entered.wait(2)
    session.disconnect()
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert errors and errors[0].cancelled
    assert not session.has_token and session.account is None and store.saved is None
    assert not any(isinstance(call, tuple) and call[0] == "save" for call in store.calls)


def test_validate_invalid_token_clears_stale_identity_and_saved_credential():
    store = MemoryStore(TOKEN)
    session, _ = session_with_account(store)
    session.validate()

    class Rejected:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_authenticated_user(self):
            raise GitHubError("Expired", status=401)

    session._client_factory = Rejected
    with pytest.raises(GitHubError):
        session.validate()
    assert session.account is None and not session.has_token and not session.remembered
    assert store.saved is None


def test_session_client_updates_restored_account_with_one_verification(transport):
    connection = transport()
    session = GitHubSession(store=MemoryStore(TOKEN))
    assert session.account is None
    assert session.client().get_authenticated_user() == ACCOUNT
    assert session.account == ACCOUNT and len(connection.calls) == 1


def test_session_client_invalid_credentials_clear_account_and_storage(transport):
    transport()
    store = MemoryStore(TOKEN)
    session = GitHubSession(store=store)
    client = session.client()
    client.get_authenticated_user()
    transport(FakeResponse(status=401))
    with pytest.raises(GitHubError) as caught:
        client.request("GET", "/repos/clarence/project")
    assert caught.value.status == 401
    assert not session.has_token and session.account is None and store.saved is None


def test_session_client_cannot_send_after_disconnect(transport):
    session = GitHubSession(store=MemoryStore(TOKEN))
    client = session.client()
    session.disconnect()
    with pytest.raises(GitHubError) as caught:
        client.request("POST", "/user/repos", {"name": "example"})
    assert caught.value.cancelled and transport.created == []


def test_disconnect_clears_memory_even_when_native_delete_fails():
    store = MemoryStore(TOKEN)
    session, _ = session_with_account(store)
    session.validate()
    store.fail_delete = True
    with pytest.raises(GitHubError, match="Windows Credential Manager"):
        session.disconnect()
    assert not session.has_token and session.account is None
    with pytest.raises(GitHubError, match="Connect"):
        session.client()


class NativeFunction:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        return self.implementation(*args)


class FakeWinCred:
    """The real ctypes structs/pointers are read by this fake Win32 boundary."""

    def __init__(self):
        self.saved = None
        self.error = 1168
        self.fail_write = False
        self.fail_read = False
        self.fail_delete = False
        self.freed = 0
        self.read_allocations = []
        self.write_pointer = None
        self.write_blob = None
        self.CredReadW = NativeFunction(self.read)
        self.CredWriteW = NativeFunction(self.write)
        self.CredDeleteW = NativeFunction(self.delete)
        self.CredFree = NativeFunction(self.free)

    def write(self, address, flags):
        credential = ctypes.cast(address, ctypes.POINTER(github._CREDENTIALW)).contents
        assert flags == 0
        assert credential.TargetName == WindowsCredentialStore.TARGET
        assert credential.Type == 1 and credential.Persist == 2
        assert credential.AttributeCount == 0 and not credential.Attributes
        assert credential.UserName == "clarence"
        self.write_pointer = credential.CredentialBlob
        self.write_blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        if self.fail_write:
            self.error = 5
            return 0
        self.saved = (self.write_blob, credential.UserName)
        return 1

    def read(self, target, kind, flags, output):
        assert (target, kind, flags) == (WindowsCredentialStore.TARGET, 1, 0)
        if self.fail_read or self.saved is None:
            self.error = 5 if self.fail_read else 1168
            return 0
        data, login = self.saved
        blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        credential = github._CREDENTIALW()
        credential.Type = 1
        credential.TargetName = target
        credential.CredentialBlobSize = len(data)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.UserName = login
        self.read_allocations.append((credential, blob))
        destination = ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(github._CREDENTIALW)))
        destination[0] = ctypes.pointer(credential)
        return 1

    def delete(self, target, kind, flags):
        assert (target, kind, flags) == (WindowsCredentialStore.TARGET, 1, 0)
        if self.fail_delete:
            self.error = 5
            return 0
        if self.saved is None:
            self.error = 1168
            return 0
        self.saved = None
        return 1

    def free(self, address):
        assert address
        credential = ctypes.cast(address, ctypes.POINTER(github._CREDENTIALW)).contents
        assert ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize) == b"\0" * credential.CredentialBlobSize
        self.freed += 1


def native_store():
    api = FakeWinCred()
    return WindowsCredentialStore(_api=api, _last_error=lambda: api.error), api


def test_windows_credential_struct_uses_windows_integer_widths():
    assert ctypes.sizeof(github._FILETIME) == 8
    assert github._CREDENTIALW.Type.offset == 4
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(github._CREDENTIALW) == 80
        assert github._CREDENTIALW.CredentialBlobSize.offset == 32
        assert github._CREDENTIALW.CredentialBlob.offset == 40


def test_windows_credential_round_trip_uses_fixed_target_and_local_user_persistence():
    store, api = native_store()
    assert store.available and store.load() is None
    store.save(TOKEN, "clarence")
    assert api.saved == (TOKEN.encode(), "clarence")
    assert api.CredWriteW.restype is ctypes.c_int32
    assert store.load() == TOKEN
    assert api.freed == 1
    assert bytes(api.read_allocations[0][1]) == b"\0" * len(TOKEN)
    store.delete()
    assert store.load() is None
    store.delete()  # No credential is already a successful disconnect.


def test_unreadable_windows_blob_is_freed_and_never_echoed():
    store, api = native_store()
    api.saved = (b"\xff" + TOKEN.encode(), "clarence")
    with pytest.raises(GitHubError, match="could not be read") as caught:
        store.load()
    assert TOKEN not in str(caught.value)
    assert api.freed == 1


@pytest.mark.parametrize("operation", ["save", "load", "delete"])
def test_native_credential_failures_have_safe_messages(operation):
    store, api = native_store()
    api.fail_write = api.fail_read = api.fail_delete = True
    with pytest.raises(GitHubError) as caught:
        if operation == "save":
            store.save(TOKEN, "clarence")
        else:
            getattr(store, operation)()
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("repository", ["clarence/project", "https://github.com/clarence/project.git", "git@github.com:clarence/project.git"])
def test_token_setup_prefills_only_existing_repository_permissions(repository):
    url = urlsplit(github.token_setup_url(repository))
    parameters = parse_qs(url.query)
    assert (url.scheme, url.netloc, url.path) == ("https", "github.com", "/settings/personal-access-tokens/new")
    assert parameters["target_name"] == ["clarence"]
    assert parameters["contents"] == ["write"] and parameters["metadata"] == ["read"]
    assert parameters["expires_in"] == ["30"]
    assert "administration" not in parameters and "workflows" not in parameters
    assert "repository" not in parameters and "repo" not in parameters


def test_new_repository_and_workflows_permissions_are_explicit_opt_ins():
    parameters = parse_qs(urlsplit(github.token_setup_url("clarence/project", True, workflows=True)).query)
    assert parameters["administration"] == ["write"]
    assert parameters["workflows"] == ["write"]
    assert "pull_requests" not in parameters


@pytest.mark.parametrize("repository", ["https://evil.invalid/name/repo", "https://github.com/owner/repo?token=secret", "https://token@github.com/owner/repo", "owner/../../elsewhere", "owner&administration=write/repo"])
def test_token_setup_does_not_echo_untrusted_repository_details(repository):
    parameters = parse_qs(urlsplit(github.token_setup_url(repository)).query)
    assert "target_name" not in parameters
    assert "administration" not in parameters


def test_shared_session_factory_returns_one_instance_without_network(monkeypatch):
    monkeypatch.setattr(github, "_session", None)
    created = []

    def make_session():
        created.append(True)
        return GitHubSession(store=MemoryStore())

    monkeypatch.setattr(github, "GitHubSession", make_session)
    first, second = github.get_session(), github.get_session()
    assert first is second and len(created) == 1
