"""Built-in GitHub HTTPS authentication, with optional Windows secure storage.

No Git, GitHub CLI, browser cookies, environment tokens or plaintext credential
files are used.  A personal access token is sent only as an HTTPS Authorization
header to api.github.com.  An OAuth client must belong to this application;
this module deliberately does not impersonate another application's client.
"""
from __future__ import annotations

import ctypes
import http.client
import json
import os
import re
import socket
import ssl
import threading
import time
from typing import Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit


GITHUB_API_HOST = "api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"
GITHUB_TOKENS_URL = "https://github.com/settings/personal-access-tokens"
SOCKET_TIMEOUT = 15.0
REQUEST_TIMEOUT = 120.0
# A 95 MB project file becomes about 127 MB when a Git blob uses base64.
MAX_RESPONSE_BYTES = 140_000_000
MAX_REQUEST_BYTES = 140_000_000
UPLOAD_CHUNK_BYTES = 65_536
MAX_TOKEN_LENGTH = 2048
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_]{20,2048}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_SECRET_QUERY_NAMES = {"access_token", "token", "authorization", "client_secret"}


class GitHubError(RuntimeError):
    """An actionable error whose message never contains server/transport data."""

    def __init__(self, message: str, *, status: int | None = None, cancelled: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.cancelled = cancelled


def _check_cancelled(cancelled) -> None:
    if cancelled is not None and cancelled.is_set():
        raise GitHubError("The GitHub operation was cancelled.", cancelled=True) from None


def _normalize_token(value: str) -> str:
    # Accept current fine-grained tokens, classic PATs and the older hex format,
    # without fixing a particular token prefix or taking an ordinary password.
    if not isinstance(value, str):
        raise GitHubError("Paste a GitHub personal access token to connect.")
    value = value.strip()
    if not _TOKEN.fullmatch(value):
        raise GitHubError("Paste the complete GitHub personal access token, without spaces or extra text.")
    return value


def _http_error(status: int, headers) -> GitHubError:
    # GitHub error bodies can echo request content.  Never include them, a
    # Location header, or a transport exception in user-facing diagnostics.
    limited = status == 429 or (status == 403 and (
        headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After") is not None
    ))
    if limited:
        message = "GitHub's request limit has been reached. Wait a little, then try again."
    elif status == 401:
        message = "GitHub did not accept this token. It may have expired or been revoked. Create a new token and connect again."
    elif status == 403:
        message = (
            "GitHub denied access. Check the token's repository selection and permissions; "
            "an organization may also require approval or SSO authorization."
        )
    elif status == 404:
        message = "GitHub could not find this resource, or the connected token does not have access to it."
    elif status in {409, 422}:
        message = "GitHub could not apply this request. Refresh the review and check the repository, branch and token permissions."
    elif 300 <= status < 400:
        message = "GitHub redirected this request. For account security, Studio did not follow the redirect. Check the repository address."
    elif status == 410:
        message = "GitHub no longer supports this request. Update Studio and try again."
    elif status >= 500:
        message = "GitHub is temporarily unavailable. Try again later."
    else:
        message = f"GitHub could not complete this request (HTTP {status}). Check the request and try again."
    return GitHubError(message, status=status)


class GitHubClient:
    """Small, bounded JSON client. Each request owns one TLS connection.

    Requests use the standard-library HTTPS transport directly, so redirect
    handlers, .netrc files, inherited proxy credentials and cookie jars cannot
    supply or redirect authentication. A cancellation watcher shuts down only
    the socket owned by this request; no write request is automatically retried.
    """

    def __init__(self, token: str, *, cancelled=None, log: Callable[[str], None] | None = None) -> None:
        self._token = _normalize_token(token)
        self.cancelled = cancelled
        self.log = log or (lambda _message: None)

    def __repr__(self) -> str:
        return "<GitHubClient api.github.com>"

    def _path(self, path: str) -> str:
        if not isinstance(path, str) or len(path) > 8192 or not path.startswith("/") or path.startswith("//"):
            raise GitHubError("Studio can only send GitHub API requests to api.github.com.")
        if any(ord(char) < 33 or ord(char) > 126 for char in path) or "\\" in path:
            raise GitHubError("The GitHub request address is invalid.")
        try:
            parsed = urlsplit(path)
            decoded = unquote(path, errors="strict")
            query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100)
        except (UnicodeError, ValueError):
            raise GitHubError("The GitHub request address is invalid.") from None
        if parsed.scheme or parsed.netloc or parsed.fragment or any(ord(char) < 32 for char in decoded):
            raise GitHubError("The GitHub request address is invalid.")
        if self._token in path or self._token in decoded or any(key.lower() in _SECRET_QUERY_NAMES for key, _ in query):
            raise GitHubError("GitHub credentials must not be included in a request address.")
        return path

    @staticmethod
    def _abort(connection, owned_socket=None) -> None:
        sock = owned_socket if owned_socket is not None else getattr(connection, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            connection.close()
        except OSError:
            pass

    def _connect_bounded(self, connection, deadline: float) -> None:
        # Python's socket timeout does not cover the operating system's DNS
        # resolver. Keep that stage off the calling worker too, so Cancel can
        # finish promptly. A late resolver/TLS result only closes its socket;
        # the helper never has the request headers or sends an HTTP request.
        connected = threading.Event()
        abandoned = threading.Event()
        failure = []
        connect_deadline = min(deadline, time.monotonic() + SOCKET_TIMEOUT)

        def connect_only() -> None:
            try:
                connection.connect()
            except Exception as exc:
                failure.append(exc)
            finally:
                if abandoned.is_set():
                    self._abort(connection)
                connected.set()

        connector = threading.Thread(target=connect_only, name="studio-github-connect", daemon=True)
        connector.start()
        try:
            while not connected.wait(0.05):
                _check_cancelled(self.cancelled)
                if time.monotonic() >= connect_deadline:
                    raise GitHubError("The GitHub connection timed out. Check your connection and try again.")
            _check_cancelled(self.cancelled)
            if time.monotonic() >= connect_deadline:
                raise GitHubError("The GitHub connection timed out. Check your connection and try again.")
            if failure:
                raise failure[0]
        finally:
            if not connected.is_set():
                abandoned.set()
                self._abort(connection)

    def request(self, method: str, path: str, data=None) -> dict | list:
        _check_cancelled(self.cancelled)
        method = method.upper() if isinstance(method, str) else ""
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            raise GitHubError("This GitHub request method is not supported.")
        path = self._path(path)
        body = None
        if data is not None:
            if not isinstance(data, (dict, list)):
                raise GitHubError("The GitHub request data is invalid.")
            try:
                body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise GitHubError("The GitHub request data is invalid.") from None
            if len(body) > MAX_REQUEST_BYTES:
                raise GitHubError("This GitHub request is too large. Publish fewer or smaller files.")
        headers = {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "HoYo-Project-Studio",
            "Accept-Encoding": "identity",
        }
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        connection = None
        response = None
        owned_socket = None
        complete = threading.Event()
        deadline_reached = threading.Event()
        watcher = None
        deadline = time.monotonic() + REQUEST_TIMEOUT
        try:
            connection = http.client.HTTPSConnection(
                GITHUB_API_HOST, port=443, timeout=SOCKET_TIMEOUT,
                context=ssl.create_default_context(),
            )
            # Explicitly prevent debug-level HTTP output from exposing headers.
            connection.set_debuglevel(0)

            def watch() -> None:
                while not complete.wait(0.05):
                    if self.cancelled is not None and self.cancelled.is_set():
                        self._abort(connection, owned_socket)
                        return
                    if time.monotonic() >= deadline:
                        deadline_reached.set()
                        self._abort(connection, owned_socket)
                        return

            watcher = threading.Thread(target=watch, name="studio-github-cancel", daemon=True)
            watcher.start()
            # Connect separately so a cancellation during DNS/TLS setup cannot
            # cause an HTTP write after that setup finally completes. Prevent
            # http.client from reopening a socket closed by the watcher.
            self._connect_bounded(connection, deadline)
            owned_socket = connection.sock
            connection.auto_open = 0
            _check_cancelled(self.cancelled)
            if deadline_reached.is_set() or time.monotonic() >= deadline:
                raise GitHubError("The GitHub request timed out. Check your connection and try again.")
            upload_body = body
            if body is not None and len(body) > UPLOAD_CHUNK_BYTES:
                # HTTPConnection sends a bytes body with one sendall call;
                # its timeout would otherwise reject a steadily progressing
                # large upload after SOCKET_TIMEOUT seconds. An iterable with
                # Content-Length sends ordinary HTTP bytes in bounded pieces.
                headers["Content-Length"] = str(len(body))

                def upload_chunks():
                    view = memoryview(body)
                    for offset in range(0, len(body), UPLOAD_CHUNK_BYTES):
                        _check_cancelled(self.cancelled)
                        if deadline_reached.is_set() or time.monotonic() >= deadline:
                            raise GitHubError("The GitHub request timed out. Check your connection and try again.")
                        yield view[offset:offset + UPLOAD_CHUNK_BYTES]

                upload_body = upload_chunks()
            connection.request(method, path, body=upload_body, headers=headers)
            response = connection.getresponse()
            _check_cancelled(self.cancelled)
            if deadline_reached.is_set():
                raise GitHubError("The GitHub request timed out. Check your connection and try again.")
            if not 200 <= response.status < 300:
                raise _http_error(response.status, response.headers)
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared_length = int(length)
                except (TypeError, ValueError):
                    raise GitHubError("GitHub returned an invalid response. Try again.") from None
                if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
                    raise GitHubError("The GitHub response is too large for this operation. Publish fewer or smaller files.")
            chunks = bytearray()
            while True:
                _check_cancelled(self.cancelled)
                if deadline_reached.is_set() or time.monotonic() >= deadline:
                    raise GitHubError("The GitHub request timed out. Check your connection and try again.")
                chunk = response.read(min(65_536, MAX_RESPONSE_BYTES + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise GitHubError("The GitHub response is too large for this operation. Publish fewer or smaller files.")
            _check_cancelled(self.cancelled)
            if not chunks:
                payload = {}
            else:
                try:
                    payload = json.loads(chunks)
                except (ValueError, UnicodeError):
                    raise GitHubError("GitHub returned an invalid response. Try again.") from None
                if not isinstance(payload, (dict, list)):
                    raise GitHubError("GitHub returned an unexpected response. Try again.")
            _check_cancelled(self.cancelled)
            if deadline_reached.is_set() or time.monotonic() >= deadline:
                raise GitHubError("The GitHub request timed out. Check your connection and try again.")
            # The publishing layer reports meaningful stages. Per-request
            # chatter would bury those stages; transport details also stay
            # private (no URLs, headers, payloads or exceptions in the log).
            return payload
        except GitHubError:
            raise
        except ssl.SSLCertVerificationError:
            _check_cancelled(self.cancelled)
            raise GitHubError("Studio could not verify GitHub's secure connection. Check your computer's date and trusted certificates.") from None
        except (TimeoutError, socket.timeout):
            _check_cancelled(self.cancelled)
            raise GitHubError("The GitHub request timed out. Check your connection and try again.") from None
        except (OSError, http.client.HTTPException):
            _check_cancelled(self.cancelled)
            if deadline_reached.is_set():
                raise GitHubError("The GitHub request timed out. Check your connection and try again.") from None
            raise GitHubError("Studio could not complete the GitHub request. Check your connection and try again.") from None
        finally:
            complete.set()
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
            if connection is not None:
                self._abort(connection, owned_socket)
            if watcher is not None:
                watcher.join(timeout=0.2)

    def get_authenticated_user(self) -> dict:
        account = self.request("GET", "/user")
        if not isinstance(account, dict):
            raise GitHubError("GitHub could not verify this account. Connect again.")
        login, account_id = account.get("login"), account.get("id")
        if not isinstance(login, str) or not _LOGIN.fullmatch(login) or login == self._token or type(account_id) is not int or account_id < 1:
            raise GitHubError("GitHub could not verify this account. Connect again.")
        name = account.get("name")
        name = name if isinstance(name, str) else ""
        name = "".join(char for char in name.replace(self._token, "[redacted]")[:256] if char.isprintable())
        return {"login": login, "id": account_id, "name": name, "html_url": "https://github.com/" + login}


# Explicit Windows-width integer fields keep the ABI correct on Windows and
# make tests of the same native layout meaningful on non-Windows hosts.
class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME), ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32), ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialStore:
    """One application-owned generic credential in Windows Credential Manager.

    The token blob is UTF-8, kept local to this Windows user and computer. No
    fallback credential file is ever created. Tests may inject a fake Win32 API
    to exercise the real ctypes call arguments without an actual credential.
    """

    TARGET = "HoYoProjectStudio:github.com"
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168
    MAX_BLOB_BYTES = 5 * 512

    def __init__(self, *, _api=None, _last_error=None) -> None:
        self._lock = threading.RLock()
        self._api = _api
        self._last_error = _last_error or getattr(ctypes, "get_last_error", lambda: 0)
        if _api is None and os.name == "nt":
            try:
                self._api = ctypes.WinDLL("advapi32.dll", use_last_error=True, winmode=0x00000800)
            except OSError:
                self._api = None
        if self._api is not None:
            pointer = ctypes.POINTER(_CREDENTIALW)
            self._api.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(pointer)]
            self._api.CredReadW.restype = ctypes.c_int32
            self._api.CredWriteW.argtypes = [pointer, ctypes.c_uint32]
            self._api.CredWriteW.restype = ctypes.c_int32
            self._api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
            self._api.CredDeleteW.restype = ctypes.c_int32
            self._api.CredFree.argtypes = [ctypes.c_void_p]
            self._api.CredFree.restype = None

    @property
    def available(self) -> bool:
        return self._api is not None

    def load(self) -> str | None:
        if not self.available:
            return None
        with self._lock:
            credential = ctypes.POINTER(_CREDENTIALW)()
            if not self._api.CredReadW(self.TARGET, self.CRED_TYPE_GENERIC, 0, ctypes.byref(credential)):
                if self._last_error() == self.ERROR_NOT_FOUND:
                    return None
                raise GitHubError("Studio could not read its saved GitHub sign-in from Windows Credential Manager. Connect again.")
            try:
                if not credential:
                    raise GitHubError("The saved GitHub sign-in could not be read. Connect again.")
                value = credential.contents
                size = value.CredentialBlobSize
                if value.Type != self.CRED_TYPE_GENERIC or not value.CredentialBlob or not 0 < size <= self.MAX_BLOB_BYTES:
                    raise GitHubError("The saved GitHub sign-in could not be read. Connect again.")
                try:
                    return _normalize_token(ctypes.string_at(value.CredentialBlob, size).decode("utf-8"))
                except (UnicodeError, GitHubError):
                    raise GitHubError("The saved GitHub sign-in could not be read. Connect again.") from None
            finally:
                if credential:
                    value = credential.contents
                    if value.CredentialBlob and 0 < value.CredentialBlobSize <= self.MAX_BLOB_BYTES:
                        ctypes.memset(value.CredentialBlob, 0, value.CredentialBlobSize)
                self._api.CredFree(credential)

    def save(self, token: str, login: str) -> None:
        if not self.available:
            raise GitHubError("Remembering GitHub sign-in is available through Windows Credential Manager on Windows.")
        token_bytes = _normalize_token(token).encode("utf-8")
        if len(token_bytes) > self.MAX_BLOB_BYTES or not isinstance(login, str) or not _LOGIN.fullmatch(login):
            raise GitHubError("This GitHub sign-in cannot be saved. Connect for this session instead.")
        with self._lock:
            blob = (ctypes.c_ubyte * len(token_bytes)).from_buffer_copy(token_bytes)
            credential = _CREDENTIALW()
            credential.Type = self.CRED_TYPE_GENERIC
            credential.TargetName = self.TARGET
            credential.Comment = "HoYo Project Studio GitHub sign-in"
            credential.CredentialBlobSize = len(token_bytes)
            credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
            credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
            credential.UserName = login
            try:
                if not self._api.CredWriteW(ctypes.byref(credential), 0):
                    raise GitHubError("Windows Credential Manager could not save the GitHub sign-in.")
            finally:
                ctypes.memset(blob, 0, len(blob))

    def delete(self) -> None:
        if not self.available:
            return
        with self._lock:
            if not self._api.CredDeleteW(self.TARGET, self.CRED_TYPE_GENERIC, 0):
                if self._last_error() != self.ERROR_NOT_FOUND:
                    raise GitHubError(
                        "Windows could not remove Studio's saved GitHub sign-in. Open Windows Credential Manager, "
                        "remove HoYoProjectStudio:github.com, then try again."
                    )


class GitHubSession:
    """Shared, thread-safe connection state. Construction never uses a network.

    A restored token is deliberately unverified (account is None) until
    validate() succeeds. connect() validates before replacing credentials;
    concurrent disconnect or a newer connect invalidates an older result.
    """

    def __init__(self, *, store=None, client_factory=None) -> None:
        self._lock = threading.RLock()
        self._store = store if store is not None else WindowsCredentialStore()
        self._client_factory = client_factory
        self._token: str | None = None
        self._account: dict | None = None
        self._remembered = False
        self._storage_warning = ""
        self._generation = 0
        try:
            saved = self._store.load()
            if saved:
                self._token = _normalize_token(saved)
                self._remembered = True
        except Exception:
            # Never propagate native credential data or a plaintext fallback.
            self._storage_warning = "Studio could not restore the saved GitHub sign-in. Connect again."

    def __repr__(self) -> str:
        return "<GitHubSession connected>" if self.has_token else "<GitHubSession disconnected>"

    @property
    def has_token(self) -> bool:
        with self._lock:
            return bool(self._token)

    @property
    def account(self) -> dict | None:
        with self._lock:
            return dict(self._account) if self._account is not None else None

    @property
    def storage_available(self) -> bool:
        return bool(self._store.available)

    @property
    def remembered(self) -> bool:
        with self._lock:
            return self._remembered

    @property
    def storage_warning(self) -> str:
        with self._lock:
            return self._storage_warning

    def get_token(self) -> str:
        with self._lock:
            if not self._token:
                raise GitHubError("Connect your GitHub account in Studio before publishing.")
            return self._token

    def _new_client(self, token: str, *, cancelled=None, log=None):
        return (self._client_factory or GitHubClient)(token, cancelled=cancelled, log=log)

    def client(self, *, cancelled=None, log=None) -> _SessionGitHubClient:
        with self._lock:
            client = self._new_client(self.get_token(), cancelled=cancelled, log=log)
            return _SessionGitHubClient(self, self._generation, client)

    def _check_generation(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation or not self._token:
                raise GitHubError("The GitHub connection changed. Connect and review your changes again.", cancelled=True)

    def _forget_invalid(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._generation += 1
            self._account = None
            self._token = None
            try:
                self._store.delete()
            except Exception:
                self._storage_warning = "The expired GitHub sign-in could not be removed from Windows Credential Manager. Remove HoYoProjectStudio:github.com there, then connect again."
            else:
                self._remembered = False

    def connect(self, token: str, remember: bool = False, *, cancelled=None) -> dict:
        _check_cancelled(cancelled)
        token = _normalize_token(token)
        with self._lock:
            self._generation += 1
            generation = self._generation
        account = self._new_client(token, cancelled=cancelled).get_authenticated_user()
        _check_cancelled(cancelled)
        with self._lock:
            if generation != self._generation:
                raise GitHubError("This GitHub connection was superseded. Connect again if needed.", cancelled=True)
            _check_cancelled(cancelled)
            # Remove any older account even for a session-only connection.
            # Failing this step must not silently remember the old account.
            if self.storage_available:
                try:
                    self._store.delete()
                except Exception:
                    self._storage_warning = (
                        "Windows could not remove Studio's previous saved GitHub sign-in. Open Windows Credential Manager, "
                        "remove HoYoProjectStudio:github.com, then connect again."
                    )
                    raise GitHubError(self._storage_warning) from None
            self._remembered = False
            self._storage_warning = ""
            if remember:
                if self.storage_available:
                    _check_cancelled(cancelled)
                    try:
                        self._store.save(token, account["login"])
                        self._remembered = True
                    except Exception:
                        self._storage_warning = (
                            "Connected for this session only. Windows Credential Manager could not save the sign-in; "
                            "you will need to connect again after closing Studio."
                        )
                else:
                    self._storage_warning = (
                        "Connected for this session only. Secure sign-in storage is available on Windows; "
                        "you will need to connect again after closing Studio."
                    )
            if cancelled is not None and cancelled.is_set():
                if self._remembered:
                    try:
                        self._store.delete()
                    except Exception:
                        self._storage_warning = "The connection was cancelled, but Windows could not remove its saved sign-in. Remove HoYoProjectStudio:github.com in Windows Credential Manager."
                    else:
                        self._remembered = False
                _check_cancelled(cancelled)
            self._token = token
            self._account = dict(account)
            return dict(account)

    def validate(self, *, cancelled=None) -> dict:
        _check_cancelled(cancelled)
        with self._lock:
            token = self.get_token()
            generation = self._generation
            self._account = None
        try:
            account = self._new_client(token, cancelled=cancelled).get_authenticated_user()
            _check_cancelled(cancelled)
        except GitHubError as exc:
            with self._lock:
                if generation == self._generation:
                    self._account = None
                    if exc.status == 401:
                        self._forget_invalid(generation)
            raise
        with self._lock:
            if generation != self._generation:
                raise GitHubError("This GitHub connection was superseded. Connect again if needed.", cancelled=True)
            _check_cancelled(cancelled)
            self._account = dict(account)
            return dict(account)

    def disconnect(self) -> None:
        with self._lock:
            self._generation += 1
            self._token = None
            self._account = None
            self._storage_warning = ""
            try:
                self._store.delete()
            except Exception:
                self._storage_warning = (
                    "Disconnected from this session, but Windows could not remove the saved GitHub sign-in. "
                    "Open Windows Credential Manager and remove HoYoProjectStudio:github.com."
                )
                raise GitHubError(self._storage_warning) from None
            self._remembered = False


class _SessionGitHubClient:
    """Keep a publishing client's verified account synchronized with the UI."""

    def __init__(self, session: GitHubSession, generation: int, client: GitHubClient) -> None:
        self._session = session
        self._generation = generation
        self._client = client

    def __repr__(self) -> str:
        return "<GitHubSessionClient api.github.com>"

    def request(self, method: str, path: str, data=None) -> dict | list:
        self._session._check_generation(self._generation)
        try:
            response = self._client.request(method, path, data)
        except GitHubError as exc:
            if exc.status == 401:
                self._session._forget_invalid(self._generation)
            raise
        self._session._check_generation(self._generation)
        return response

    def get_authenticated_user(self) -> dict:
        self._session._check_generation(self._generation)
        with self._session._lock:
            self._session._account = None
        try:
            account = self._client.get_authenticated_user()
        except GitHubError as exc:
            if exc.status == 401:
                self._session._forget_invalid(self._generation)
            raise
        with self._session._lock:
            self._session._check_generation(self._generation)
            self._session._account = dict(account)
        return dict(account)


_session = None
_session_lock = threading.Lock()


def get_session() -> GitHubSession:
    """Return the one account shared by all Studio publishing windows."""
    global _session
    with _session_lock:
        if _session is None:
            _session = GitHubSession()
        return _session


def token_setup_url(repository: str = "", new_repository: bool = False, *, workflows: bool = False) -> str:
    """GitHub's official token form, prefilled with relevant permissions.

    GitHub does not document a repository-selection URL parameter. The user
    must select the existing repository in the form, or select All repositories
    when authorizing creation of a repository that does not yet exist.
    """
    owner = ""
    if isinstance(repository, str):
        candidate = repository.strip()
        if candidate.startswith("https://github.com/"):
            parsed = urlsplit(candidate)
            candidate = parsed.path.strip("/") if not parsed.query and not parsed.fragment else ""
        elif candidate.startswith("git@github.com:"):
            candidate = candidate[len("git@github.com:"):]
        if candidate.endswith(".git"):
            candidate = candidate[:-4]
        parts = candidate.split("/")
        if len(parts) == 2 and _LOGIN.fullmatch(parts[0]) and _REPOSITORY.fullmatch(parts[1]):
            owner = parts[0]
    parameters = {
        "name": "HoYo Project Studio",
        "description": "Publish reviewed project changes from HoYo Project Studio.",
        "expires_in": "30",
        "contents": "write",
        "metadata": "read",
    }
    if owner:
        parameters["target_name"] = owner
    if new_repository:
        parameters["administration"] = "write"
    if workflows:
        parameters["workflows"] = "write"
    return GITHUB_TOKEN_URL + "?" + urlencode(parameters)
