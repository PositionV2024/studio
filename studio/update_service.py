"""Read and download a reviewed Studio release from the user's GitHub repository.

Published stable releases can contain just the complete Windows application ZIP.
GitHub's asset metadata supplies its byte size and SHA-256; older releases with a
``studio-update.json`` descriptor remain supported. The installer separately
validates the complete archive and its internal version before replacing any
application files. Checking or downloading never executes a package or changes
an installation.

The transport deliberately uses verified stdlib HTTPS without environment
credentials, proxies, cookies, Git, or GitHub CLI. Studio's connected GitHub
account is required for every update operation. Its token is sent only to
``api.github.com`` and is never forwarded to the release asset CDN.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import unquote, urlsplit


GITHUB_API_HOST = "api.github.com"
GITHUB_API_VERSION = "2026-03-10"
MANIFEST_NAME = "studio-update.json"
APPLICATION_ID = "hoyo-project-studio"
MAX_RELEASE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_RELEASE_NOTES = 32 * 1024
SOCKET_TIMEOUT = 15.0
METADATA_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 600.0
MAX_REDIRECTS = 4
CHUNK_BYTES = 64 * 1024
ASSET_HOSTS = frozenset({
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "objects-origin.githubusercontent.com",
    "github-releases.githubusercontent.com",
})
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\Z")
_SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_]{20,2048}\Z")
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³"),
}
_DELIVERED_PACKAGE_NAME = "hoyo_project_studio_preview_tabs_code_editor.zip"


class UpdateError(RuntimeError):
    """A safe, actionable diagnostic; never raw response bodies or signed URLs."""

    def __init__(self, message: str, *, cancelled: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.cancelled = cancelled
        self.status = status


@dataclass(frozen=True)
class UpdateRelease:
    repository: str
    version: str
    tag: str
    release_url: str
    notes: str
    published_at: str
    asset_id: int = 0
    asset_name: str = ""
    asset_size: int = 0
    sha256: str = ""

    @property
    def is_downloadable(self) -> bool:
        return (type(self.asset_id) is int and self.asset_id > 0 and
                _safe_package_name(self.asset_name) and
                type(self.asset_size) is int and 0 < self.asset_size <= MAX_PACKAGE_BYTES and
                isinstance(self.sha256, str) and _SHA256.fullmatch(self.sha256) is not None)


@dataclass(frozen=True)
class UpdateCheck:
    current_version: str
    release: UpdateRelease | None
    message: str = ""

    @property
    def is_newer(self) -> bool:
        return self.release is not None and compare_versions(self.release.version, self.current_version) > 0

    @property
    def download_available(self) -> bool:
        return self.is_newer and self.release.is_downloadable

    @property
    def status(self) -> str:
        if self.release is None:
            return "none"
        comparison = compare_versions(self.release.version, self.current_version)
        if comparison > 0:
            return "available" if self.release.is_downloadable else "unavailable"
        return "ahead" if comparison < 0 else "current"


def normalize_repository(value: str) -> str:
    """Accept ``owner/repo`` or an HTTPS GitHub repository URL, without secrets."""
    invalid = "Enter your Studio application's GitHub repository as owner/repository or https://github.com/owner/repository."
    if not isinstance(value, str) or len(value) > 512:
        raise UpdateError(invalid)
    value = value.strip()
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value) or "\\" in value:
        raise UpdateError(invalid)
    if "://" in value:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or parsed.hostname != "github.com" or
                    parsed.username is not None or parsed.password is not None or
                    parsed.port is not None or parsed.query or parsed.fragment):
                raise ValueError
            value = parsed.path.removeprefix("/").removesuffix("/")
        except ValueError:
            raise UpdateError(invalid) from None
    if value.endswith(".git"):
        value = value[:-4]
    parts = value.split("/")
    if (len(parts) != 2 or not _OWNER.fullmatch(parts[0]) or
            not _REPOSITORY.fullmatch(parts[1]) or parts[1] in {".", ".."}):
        raise UpdateError(invalid)
    return "/".join(parts)


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse a stable numeric version; prereleases and ambiguous versions fail."""
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise UpdateError("Studio updates need a stable package version such as 0.4.13 (major.minor.patch).")
    return tuple(int(component) for component in match.groups())


def compare_versions(left: str, right: str) -> int:
    left_parts, right_parts = parse_version(left), parse_version(right)
    return (left_parts > right_parts) - (left_parts < right_parts)


def _cancel(cancelled) -> None:
    if cancelled is not None and cancelled.is_set():
        raise UpdateError("The update operation was cancelled.", cancelled=True)


def _http_error(status: int, headers) -> UpdateError:
    if status == 429 or (status == 403 and (
            headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After") is not None)):
        message = "GitHub's request limit has been reached. Wait a little, then check again."
    elif status == 401:
        message = "GitHub did not accept your sign-in. Reconnect your account and check for updates again."
    elif status == 403:
        message = "GitHub denied access. Give your token Contents: read access to the update repository and check any organization approval requirements."
    elif status == 404:
        message = "GitHub could not find an accessible repository or release. Check your connection and account access, then try again."
    elif 300 <= status < 400:
        message = "GitHub redirected the repository request. Check the repository's current owner and name, then try again."
    elif status >= 500:
        message = "GitHub is temporarily unavailable. Check for updates again later."
    else:
        message = f"GitHub could not complete the update request (HTTP {status}). Try again."
    return UpdateError(message, status=status)


def _json_object(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise UpdateError("GitHub returned invalid update metadata. The release owner should regenerate the Studio update files.") from None
    if not isinstance(value, dict):
        raise UpdateError("GitHub returned unexpected update metadata. The release owner should regenerate the Studio update files.")
    return value


class _GitHubTransport:
    """A bounded cancellable GET transport with no ambient authentication."""

    def __init__(self, *, session=None, cancelled=None) -> None:
        self.session = session
        self.cancelled = cancelled
        self._token = None
        _cancel(cancelled)
        if session is None:
            raise UpdateError("Connect your GitHub account in Studio before checking or downloading updates.")
        try:
            token = session.get_token()
        except Exception:
            raise UpdateError("Connect your GitHub account in Studio before checking or downloading updates.") from None
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise UpdateError("Reconnect your GitHub account in Studio before checking or downloading updates.")
        self._token = token

    def __repr__(self) -> str:
        return "<StudioUpdateTransport api.github.com>"

    def _check(self, deadline: float | None = None) -> None:
        _cancel(self.cancelled)
        try:
            current = self.session.get_token()
        except Exception:
            current = None
        if (not isinstance(current, str) or not _TOKEN.fullmatch(current) or
                not hmac.compare_digest(current, self._token)):
            raise UpdateError("Your GitHub sign-in changed. Check for updates again.", cancelled=True)
        if deadline is not None and time.monotonic() >= deadline:
            raise UpdateError("The update request timed out. Check your connection and try again.")

    def redact(self, value: str) -> str:
        return value.replace(self._token, "[redacted]") if self._token else value

    def _url(self, value: str, *, cdn: bool) -> tuple[str, str]:
        invalid = "GitHub returned an unsupported update download address. Check again or ask the release owner to republish the package."
        if (not isinstance(value, str) or len(value) > 16_384 or "\\" in value or
                any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise UpdateError(invalid)
        try:
            parsed = urlsplit(value)
            decoded = unquote(value, errors="strict")
            host = parsed.hostname
            if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or
                    parsed.port not in {None, 443} or parsed.fragment or
                    any(ord(char) < 32 or ord(char) == 127 for char in decoded) or
                    not parsed.path.startswith("/") or parsed.path.startswith("//") or
                    host not in (ASSET_HOSTS if cdn else {GITHUB_API_HOST})):
                raise ValueError
            if self._token and (self._token in value or self._token in decoded):
                raise ValueError
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
        except (ValueError, UnicodeError):
            raise UpdateError(invalid) from None
        return host, path

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

    def _connect(self, connection, deadline: float) -> None:
        # A socket timeout does not bound DNS resolution. The resolver thread
        # has no headers and cannot send an HTTP request after cancellation.
        connected, abandoned = threading.Event(), threading.Event()
        failures = []
        connect_deadline = min(deadline, time.monotonic() + SOCKET_TIMEOUT)

        def connect_only():
            try:
                connection.connect()
            except Exception as exc:
                failures.append(exc)
            finally:
                if abandoned.is_set():
                    self._abort(connection)
                connected.set()

        threading.Thread(target=connect_only, name="studio-update-connect", daemon=True).start()
        try:
            while not connected.wait(0.05):
                self._check(connect_deadline)
            self._check(connect_deadline)
            if failures:
                raise failures[0]
        finally:
            if not connected.is_set():
                abandoned.set()
                self._abort(connection)

    def read(self, path: str, *, binary: bool = False, maximum: int = MAX_RELEASE_BYTES,
             expected_size: int | None = None, sink=None, progress=None,
             timeout: float = METADATA_TIMEOUT) -> bytes:
        # API paths are constructed by this module, never taken from release
        # URL fields. A CDN response may not redirect back to an authenticated API.
        url = "https://" + GITHUB_API_HOST + path
        deadline = time.monotonic() + timeout
        for hop in range(MAX_REDIRECTS + 1):
            self._check(deadline)
            host, target = self._url(url, cdn=hop > 0)
            headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                       "Accept-Encoding": "identity", "User-Agent": "HoYo-Project-Studio-Updater"}
            if hop == 0:
                headers["X-GitHub-Api-Version"] = GITHUB_API_VERSION
                headers["Authorization"] = "Bearer " + self._token
            connection = response = owned_socket = watcher = None
            complete, timed_out = threading.Event(), threading.Event()
            try:
                connection = http.client.HTTPSConnection(host, port=443, timeout=SOCKET_TIMEOUT,
                                                         context=ssl.create_default_context())
                connection.set_debuglevel(0)

                def watch():
                    while not complete.wait(0.05):
                        try:
                            self._check(deadline)
                        except UpdateError:
                            if time.monotonic() >= deadline:
                                timed_out.set()
                            self._abort(connection, owned_socket)
                            return

                watcher = threading.Thread(target=watch, name="studio-update-cancel", daemon=True)
                watcher.start()
                self._connect(connection, deadline)
                owned_socket = connection.sock
                connection.auto_open = 0
                self._check(deadline)
                connection.request("GET", target, body=None, headers=headers)
                response = connection.getresponse()
                self._check(deadline)
                if response.status in {301, 302, 303, 307, 308} and binary:
                    if hop >= MAX_REDIRECTS:
                        raise UpdateError("GitHub redirected this update too many times. Check for updates again.")
                    url = response.headers.get("Location")
                    self._url(url, cdn=True)
                    continue
                if response.status != 200:
                    raise _http_error(response.status, response.headers)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise UpdateError("GitHub returned an unsupported update encoding. Try again.")
                declared = response.headers.get("Content-Length")
                length = None
                if declared is not None:
                    if not re.fullmatch(r"[0-9]{1,20}", declared):
                        raise UpdateError("GitHub returned an invalid update size. Try again.")
                    length = int(declared)
                    if length > maximum or (expected_size is not None and length != expected_size):
                        raise UpdateError("The update size does not match the release metadata. Check for updates again.")
                received = 0
                output = sink if sink is not None else io.BytesIO()
                total = expected_size if expected_size is not None else length or 0
                if progress is not None:
                    progress(0, total)
                while True:
                    self._check(deadline)
                    chunk = response.read(min(CHUNK_BYTES, maximum + 1 - received))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > maximum or (expected_size is not None and received > expected_size):
                        raise UpdateError("The downloaded update is larger than its release metadata allows.")
                    output.write(chunk)
                    if progress is not None:
                        progress(received, total)
                self._check(deadline)
                if ((expected_size is not None and received != expected_size) or
                        (length is not None and received != length)):
                    raise UpdateError("The update download was incomplete. Check your connection and try again.")
                return output.getvalue() if sink is None else b""
            except UpdateError:
                raise
            except ssl.SSLCertVerificationError:
                self._check(deadline)
                raise UpdateError("Studio could not verify GitHub's secure connection. Check your computer's date and trusted certificates.") from None
            except (TimeoutError, socket.timeout):
                self._check(deadline)
                raise UpdateError("The update request timed out. Check your connection and try again.") from None
            except (OSError, http.client.HTTPException):
                self._check(deadline)
                if timed_out.is_set():
                    raise UpdateError("The update request timed out. Check your connection and try again.") from None
                raise UpdateError("Studio could not transfer the update. Check your connection and available disk space, then try again.") from None
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
        raise UpdateError("Studio could not resolve the update download.")


def _safe_package_name(name) -> bool:
    """Accept a regular ZIP filename without imposing an application name.

    The name is used as one local Windows filename, never as a URL or path.
    Compatibility comes from the verified archive's contents and version.
    """
    if (not isinstance(name, str) or not 5 <= len(name) <= 184
            or not name.lower().endswith(".zip") or name != name.strip()
            or not name[:-4].strip(" .")
            or any(not character.isprintable() or character in '<>:"/\\|?*' for character in name)):
        return False
    if name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
        return False
    try:
        # Keep the component within Windows and common filesystem limits.
        return len(name.encode("utf-16-le")) <= 368 and len(name.encode("utf-8")) <= 240
    except UnicodeError:
        return False


def _asset(assets: list, name: str, *, maximum: int) -> dict:
    matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matching) != 1:
        raise UpdateError("GitHub returned missing or duplicate release files. Check for updates again after the release upload is complete.")
    asset = matching[0]
    if (type(asset.get("id")) is not int or asset["id"] < 1 or
            type(asset.get("size")) is not int or not 0 < asset["size"] <= maximum or
            asset.get("state") != "uploaded"):
        raise UpdateError("The Studio release asset is incomplete or too large. Wait for the upload to finish, then check again.")
    return asset


def _select_zip_asset(assets: list, version: str) -> dict | None:
    # The versioned distribution name is authoritative, followed by the full
    # ZIP supplied to Studio users. Never select an arbitrary first ZIP when
    # more than one generic download is available.
    candidates = [asset for asset in assets if isinstance(asset, dict) and
                  _safe_package_name(asset.get("name"))]
    selected = None
    for preferred in (f"HoYoProjectStudio-{version}-windows-x64.zip", _DELIVERED_PACKAGE_NAME):
        if any(asset.get("name") == preferred for asset in candidates):
            selected = _asset(assets, preferred, maximum=MAX_PACKAGE_BYTES)
            break
    if selected is None:
        if not candidates:
            return None
        if len(candidates) != 1:
            raise UpdateError("This release contains several ZIP files. Attach one complete Studio application ZIP so the update can be identified.")
        selected = _asset(assets, candidates[0]["name"], maximum=MAX_PACKAGE_BYTES)
    if (sum(isinstance(asset, dict) and asset.get("id") == selected["id"] for asset in assets) != 1 or
            sum(isinstance(asset, dict) and isinstance(asset.get("name"), str) and
                asset["name"].lower() == selected["name"].lower() for asset in assets) != 1):
        raise UpdateError("GitHub returned conflicting update download identities. Check for updates again.")
    return selected


def _github_asset_digest(asset: dict) -> str | None:
    digest = asset.get("digest")
    if digest is None:
        return None
    if not isinstance(digest, str) or not digest.startswith("sha256:") or not _SHA256.fullmatch(digest[7:]):
        raise UpdateError("GitHub has not provided a valid SHA-256 checksum for this Windows download. Please check again later.")
    return digest[7:].lower()


def _asset_digest(asset: dict, expected: str) -> None:
    digest = _github_asset_digest(asset)
    if digest is not None:
        if not hmac.compare_digest(digest, expected.lower()):
            raise UpdateError("GitHub's asset checksum does not match the update metadata. Check again or ask the release owner to republish the package.")


def _package_metadata(manifest: dict, tag: str) -> tuple[str, str, int, str]:
    if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or
            manifest.get("application") != APPLICATION_ID or manifest.get("platform") != "windows-x64" or
            manifest.get("layout") != "compact-v1"):
        raise UpdateError("This release is not a compatible HoYo Project Studio Windows update. Ask the release owner to use Studio's release packaging tool.")
    version = manifest.get("version")
    parse_version(version)
    if tag != "v" + version:
        raise UpdateError("The GitHub release tag and package version disagree. Publish the package with the matching vMAJOR.MINOR.PATCH tag.")
    package = manifest.get("package")
    if not isinstance(package, dict):
        raise UpdateError("studio-update.json is missing the Windows package details.")
    name, size, digest = package.get("name"), package.get("size"), package.get("sha256")
    if (not _safe_package_name(name) or
            type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES or
            not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
        raise UpdateError("studio-update.json has an invalid package name, size or SHA-256 checksum. Regenerate the release files.")
    return version, name, size, digest.lower()


def _release_metadata(release: dict, repository: str, transport: _GitHubTransport) -> UpdateRelease:
    """Validate release identity and display text without resolving its assets."""
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise UpdateError("This Studio release is not a published stable release. Publish a stable Studio release first.")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("The Studio release needs a version tag such as v0.4.13.")
    version = tag[1:]
    parse_version(version)
    published_at = release.get("published_at")
    try:
        if not isinstance(published_at, str) or len(published_at) > 40 or not published_at.endswith("Z"):
            raise ValueError
        datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        raise UpdateError("GitHub returned an invalid release publication date. Check for updates again.") from None
    notes = release.get("body") or ""
    if not isinstance(notes, str):
        raise UpdateError("GitHub returned invalid release notes. Check for updates again.")
    notes = transport.redact(notes)
    notes = "".join(char for char in notes[:MAX_RELEASE_NOTES] if char.isprintable() or char in "\n\r\t")
    transport._check()
    return UpdateRelease(repository, version, tag, f"https://github.com/{repository}/releases/tag/{tag}",
                         notes, published_at)


def _release_package(result: UpdateRelease, release: dict,
                     transport: _GitHubTransport) -> tuple[UpdateRelease, str]:
    """Apply the same complete-package policy for updates and file comparisons."""
    repository, version, tag = result.repository, result.version, result.tag
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) > 1000:
        raise UpdateError("GitHub returned an invalid release asset list. Check for updates again.")
    unavailable = f"Version {version} is published, but its Windows download is not ready yet. Please check again later."
    if any(isinstance(asset, dict) and asset.get("name") == MANIFEST_NAME for asset in assets):
        # Honor an explicitly supplied legacy descriptor in full. A broken or
        # contradictory descriptor must never be bypassed by choosing a ZIP.
        metadata_asset = _asset(assets, MANIFEST_NAME, maximum=MAX_MANIFEST_BYTES)
        raw = transport.read(f"/repos/{repository}/releases/assets/{metadata_asset['id']}", binary=True,
                             maximum=MAX_MANIFEST_BYTES, expected_size=metadata_asset["size"])
        _asset_digest(metadata_asset, hashlib.sha256(raw).hexdigest())
        _, name, size, digest = _package_metadata(_json_object(raw), tag)
        if not any(isinstance(asset, dict) and asset.get("name") == name for asset in assets):
            transport._check()
            return result, unavailable
        package_asset = _asset(assets, name, maximum=MAX_PACKAGE_BYTES)
        if package_asset["id"] == metadata_asset["id"] or package_asset["size"] != size:
            raise UpdateError("The Windows package does not match its update metadata. Ask the release owner to regenerate both files.")
        _asset_digest(package_asset, digest)
    else:
        package_asset = _select_zip_asset(assets, version)
        if package_asset is None:
            transport._check()
            return result, unavailable
        name, size = package_asset["name"], package_asset["size"]
        digest = _github_asset_digest(package_asset)
        if digest is None:
            transport._check()
            return result, f"Version {version} is published, but GitHub has not provided a verified Windows download yet. Please check again later."
    transport._check()
    return replace(result, asset_id=package_asset["id"], asset_name=name, asset_size=size, sha256=digest), ""


def _reviewed_release_identity(release: UpdateRelease) -> UpdateRelease:
    if not isinstance(release, UpdateRelease):
        raise UpdateError("The reviewed release is invalid. Check for updates again.")
    repository = normalize_repository(release.repository)
    parse_version(release.version)
    if release.tag != "v" + release.version:
        raise UpdateError("The reviewed release version and tag disagree. Check for updates again.")
    return replace(release, repository=repository)


def _reviewed_package(release: UpdateRelease) -> UpdateRelease:
    release = _reviewed_release_identity(release)
    if not release.is_downloadable:
        raise UpdateError("There is no verified update available to download. Check for updates again.")
    _package_metadata({"schema_version": 1, "application": APPLICATION_ID, "platform": "windows-x64",
                       "layout": "compact-v1", "version": release.version,
                       "package": {"name": release.asset_name, "size": release.asset_size,
                                   "sha256": release.sha256}}, release.tag)
    return release


def check_for_update(repository: str, current_version: str, *, session=None, cancelled=None) -> UpdateCheck:
    """Check the latest stable release with Studio's connected GitHub account.

    Current and older online versions are compared without fetching their ZIP or
    descriptor. ``resolve_release_package`` can separately resolve those files
    for a desktop comparison without offering a downgrade.
    """
    transport = _GitHubTransport(session=session, cancelled=cancelled)
    repository = normalize_repository(repository)
    parse_version(current_version)
    try:
        release = _json_object(transport.read(f"/repos/{repository}/releases/latest"))
    except UpdateError as error:
        if error.status != 404:
            raise
        # A missing latest release also means a wrong or inaccessible repo.
        # Verify the actual repository before reporting a neutral empty state;
        # authentication, rate-limit and network errors must stay errors.
        identity = _json_object(transport.read(f"/repos/{repository}"))
        full_name = identity.get("full_name")
        if (type(identity.get("id")) is not int or identity["id"] < 1 or
                not isinstance(full_name, str) or full_name.lower() != repository.lower()):
            raise UpdateError("GitHub could not verify the update repository. Check your account access and try again.")
        transport._check()
        return UpdateCheck(current_version, None, "No published update is available yet.")
    result = _release_metadata(release, repository, transport)
    if compare_versions(result.version, current_version) <= 0:
        return UpdateCheck(current_version, result)
    result, message = _release_package(result, release, transport)
    return UpdateCheck(current_version, result, message)


def resolve_release_package(release: UpdateRelease, *, session=None, cancelled=None) -> UpdateRelease:
    """Resolve the exact reviewed stable release's package for file comparison.

    The online version can equal or precede the desktop version. Its exact tag
    is requested; this operation never substitutes a different latest release
    or relaxes the legacy descriptor, checksum, or archive selection policies.
    A previously resolved immutable asset identity can be reused; downloading
    still rechecks that identity immediately before fetching its contents.
    """
    transport = _GitHubTransport(session=session, cancelled=cancelled)
    try:
        release = _reviewed_release_identity(release)
        if (release.asset_id, release.asset_name, release.asset_size, release.sha256) != (0, "", 0, ""):
            result = _reviewed_package(release)
        else:
            data = _json_object(transport.read(f"/repos/{release.repository}/releases/tags/{release.tag}"))
            result = _release_metadata(data, release.repository, transport)
            if result.tag != release.tag or result.published_at != release.published_at:
                raise UpdateError("The GitHub release changed after your review. Check for updates again.")
            result, message = _release_package(result, data, transport)
            if not result.is_downloadable:
                raise UpdateError(message)
        transport._check()
        return result
    except UpdateError as error:
        if error.cancelled:
            raise
        raise UpdateError("File comparison is unavailable. " + str(error), status=error.status) from None


def verify_cached_download(release: UpdateRelease, archive: str | Path, *, session=None, cancelled=None) -> bool:
    """Recheck a cached regular ZIP against the reviewed size and SHA-256.

    This bounded, cancellable read neither contacts GitHub nor modifies files.
    Missing, unreadable, changed, or non-regular files return False. A sign-out
    or cancelled operation raises UpdateError instead of returning cache data.
    The installer/comparator must still validate the complete archive contents.
    """
    transport = _GitHubTransport(session=session, cancelled=cancelled)
    release = _reviewed_package(release)
    try:
        source = Path(archive)
        before = source.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_size != release.asset_size or
                getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            transport._check()
            return False
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_size != release.asset_size or
                    (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
                transport._check()
                return False
            received, digest = 0, hashlib.sha256()
            while True:
                transport._check()
                chunk = stream.read(min(CHUNK_BYTES, release.asset_size + 1 - received))
                if not chunk:
                    break
                received += len(chunk)
                if received > release.asset_size:
                    transport._check()
                    return False
                digest.update(chunk)
        transport._check()
        return received == release.asset_size and hmac.compare_digest(digest.hexdigest(), release.sha256.lower())
    except OSError:
        transport._check()
        return False


def download_update(release: UpdateRelease, destination_dir: str | Path, *, session=None,
                    cancelled=None, progress: Callable[[int, int], None] | None = None) -> Path:
    """Download the reviewed immutable asset ID into a new private subdirectory.

    Metadata is rechecked immediately before downloading. The exact reviewed
    size and SHA-256 remain authoritative if the release changes during transfer.
    Partial or mismatched files are removed, and existing downloads are untouched.
    """
    transport = _GitHubTransport(session=session, cancelled=cancelled)
    release = _reviewed_package(release)
    repository = release.repository
    path = f"/repos/{repository}/releases/assets/{release.asset_id}"
    fresh = _json_object(transport.read(path))
    asset = _asset([fresh], release.asset_name, maximum=MAX_PACKAGE_BYTES)
    if asset["id"] != release.asset_id or asset["size"] != release.asset_size:
        raise UpdateError("The GitHub release changed after your review. Check for updates again before downloading.")
    _asset_digest(asset, release.sha256)
    transport._check()
    directory = None
    try:
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="studio-download-", dir=destination))
        partial = directory / "package.zip.part"
        digest = hashlib.sha256()
        with partial.open("xb") as stream:
            class HashingWriter:
                def write(self, chunk):
                    stream.write(chunk)
                    digest.update(chunk)

            transport.read(path, binary=True, maximum=MAX_PACKAGE_BYTES, expected_size=release.asset_size,
                           sink=HashingWriter(), progress=progress, timeout=DOWNLOAD_TIMEOUT)
            stream.flush()
            os.fsync(stream.fileno())
        transport._check()
        if not hmac.compare_digest(digest.hexdigest(), release.sha256.lower()):
            raise UpdateError("The update failed its SHA-256 check. Nothing was installed. Check for updates and download again.")
        complete = directory / release.asset_name
        partial.replace(complete)
        transport._check()
        return complete
    except UpdateError:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    except OSError:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise UpdateError("Studio could not save the update. Check available disk space and folder permissions, then try again.") from None
    except Exception:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise
