"""Deterministic production HTTP-boundary tests; no live account or repository."""
from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from email.message import Message
import hashlib
import http.client
import json
from pathlib import Path
import socket
import ssl
import threading
import time

import pytest

from studio import update_service as updates
from studio.update_service import (
    UpdateCheck, UpdateError, UpdateRelease, check_for_update, compare_versions,
    download_update, normalize_repository, parse_version,
)


TOKEN = "offline_studio_update_token_for_testing"
OTHER_TOKEN = "different_studio_update_token_for_testing"
REPO = "clarence/hoyo-studio"
VERSION = "0.4.13"
PACKAGE = b"PK\x03\x04test-package-content-verified-before-installer"
HASH = hashlib.sha256(PACKAGE).hexdigest()


class FakeSession:
    def __init__(self, token=TOKEN):
        self.token = token

    def get_token(self):
        if not self.token:
            raise RuntimeError("private session diagnostic " + TOKEN)
        return self.token


class FakeSocket:
    def __init__(self):
        self.stopped = threading.Event()

    def shutdown(self, _how):
        self.stopped.set()


class FakeResponse:
    def __init__(self, payload=None, *, raw=None, status=200, headers=None):
        self.body = json.dumps(payload).encode("utf-8") if raw is None else raw
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)
        self.offset = 0
        self.read_sizes = []
        self.closed = False
        self.read_hook = None

    def read(self, size):
        self.read_sizes.append(size)
        if self.read_hook is not None:
            self.read_hook()
        result = self.body[self.offset:self.offset + size]
        self.offset += len(result)
        return result

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.sock = None
        self.socket = FakeSocket()
        self.calls = []
        self.closed = False
        self.debug = None
        self.connect_action = None
        self.response_action = None

    def connect(self):
        if self.connect_action:
            self.connect_action()
        self.sock = self.socket

    def set_debuglevel(self, value):
        self.debug = value

    def request(self, method, path, *, body, headers):
        if self.sock is None:
            raise http.client.NotConnected()
        self.calls.append((method, path, body, dict(headers)))

    def getresponse(self):
        return self.response_action() if self.response_action else self.response

    def close(self):
        self.closed = True
        self.sock = None


@pytest.fixture
def transport(monkeypatch):
    ready, created = [], []

    def install(response):
        connection = FakeConnection(response)
        ready.append(connection)
        return connection

    def factory(*args, **kwargs):
        assert ready, "Unexpected live network request in an offline test"
        connection = ready.pop(0)
        created.append((connection, args, kwargs))
        return connection

    monkeypatch.setattr(updates.http.client, "HTTPSConnection", factory)
    install.created = created
    install.remaining = ready
    return install


def release_data(*, version=VERSION, manifest_changes=None, package_changes=None, release_changes=None):
    manifest = {"schema_version": 1, "application": "hoyo-project-studio", "version": version,
                "platform": "windows-x64", "layout": "compact-v1",
                "package": {"name": f"HoYoProjectStudio-{version}-windows-x64.zip", "size": len(PACKAGE), "sha256": HASH}}
    manifest.update(manifest_changes or {})
    if isinstance(manifest.get("package"), dict):
        manifest["package"].update(package_changes or {})
    raw = json.dumps(manifest).encode("utf-8")
    metadata = {"id": 11, "name": "studio-update.json", "size": len(raw), "state": "uploaded",
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
    package = {"id": 12, "name": f"HoYoProjectStudio-{version}-windows-x64.zip", "size": len(PACKAGE),
               "state": "uploaded", "digest": "sha256:" + HASH}
    release = {"id": 10, "draft": False, "prerelease": False, "tag_name": "v" + version,
               "body": "## Changes\nImproved editing and updates.", "published_at": "2026-10-18T04:39:46Z",
               "assets": [metadata, package], "html_url": "https://untrusted.invalid/ignored"}
    release.update(release_changes or {})
    return release, manifest, raw


def install_check(transport, **kwargs):
    release, manifest, raw = release_data(**kwargs)
    api = transport(FakeResponse(release))
    metadata = transport(FakeResponse(raw=raw))
    return api, metadata, release, manifest


def zip_asset(name=None, *, asset_id=12, digest="sha256:" + HASH):
    return {"id": asset_id, "name": name or f"HoYoProjectStudio-{VERSION}-windows-x64.zip",
            "size": len(PACKAGE), "state": "uploaded", "digest": digest}


def install_zip_check(transport, assets=None, **release_changes):
    release, _, _ = release_data(release_changes=release_changes)
    release["assets"] = [zip_asset()] if assets is None else assets
    api = transport(FakeResponse(release))
    return api, release


def reviewed_release():
    return UpdateRelease(REPO, VERSION, "v" + VERSION, f"https://github.com/{REPO}/releases/tag/v{VERSION}",
                         "Release notes", "2026-10-18T04:39:46Z", 12,
                         f"HoYoProjectStudio-{VERSION}-windows-x64.zip", len(PACKAGE), HASH)


def install_download(transport, *, raw=PACKAGE, changed=None, digest="default", headers=None):
    metadata = {"id": 12, "name": reviewed_release().asset_name, "size": len(PACKAGE), "state": "uploaded"}
    if digest == "default":
        metadata["digest"] = "sha256:" + HASH
    elif digest is not None:
        metadata["digest"] = digest
    metadata.update(changed or {})
    api = transport(FakeResponse(metadata))
    binary = transport(FakeResponse(raw=raw, headers=headers))
    return api, binary


@pytest.mark.parametrize("source,expected", [
    (REPO, REPO), ("  " + REPO + "\n", REPO),
    ("https://github.com/" + REPO, REPO), ("https://github.com/" + REPO + ".git/", REPO),
    ("OWNER/My.App.git", "OWNER/My.App"), ("https://GITHUB.com/OWNER/.github/", "OWNER/.github"),
])
def test_repository_normalization(source, expected):
    assert normalize_repository(source) == expected


@pytest.mark.parametrize("source", [
    "", None, "repo", "owner/repo/tree/main", "owner/..", "owner/.", "/owner/repo", "owner/repo/",
    "owner//repo", "https://github.com/owner/repo?token=private", "https://github.com/owner/repo#readme",
    "http://github.com/owner/repo", "git@github.com:owner/repo.git", "https://api.github.com/owner/repo",
    "https://github.com.evil.invalid/owner/repo", "https://user:secret@github.com/owner/repo",
    "https://github.com:443/owner/repo", "https://github.com:bad/owner/repo", "https://github.com//owner/repo",
    "owner/repo%2Fsecret", "owner\\repo", "owner/re po", "owner/repo\nSecret", "owner/日本語", "o/" + "r" * 101,
])
def test_repository_rejects_unsupported_or_credential_bearing_addresses(source):
    with pytest.raises(UpdateError) as caught:
        normalize_repository(source)
    assert "secret" not in str(caught.value).lower()


@pytest.mark.parametrize("left,right,expected", [
    ("0.4.13", "0.4.9", 1), ("0.4.13", "0.4.13", 0), ("0.4.12", "0.4.13", -1),
    ("0.10.0", "0.9.99", 1), ("1.0.0", "0.999.999", 1), ("10.0.0", "2.0.0", 1),
])
def test_versions_are_numeric_with_no_downgrade(left, right, expected):
    assert compare_versions(left, right) == expected
    assert compare_versions(right, left) == -expected


@pytest.mark.parametrize("version", ["v0.4.13", "0.04.13", "0.4", "0.4.13.0", "0.4.13rc1", "0.4.13-beta.1",
                                     "0.4.13+local", "-1.0.0", "0.4.13\n", " 0.4.13", "1e3.0.0", "１.0.0", "9" * 100, None])
def test_ambiguous_prerelease_and_oversized_versions_fail(version):
    with pytest.raises(UpdateError):
        parse_version(version)


def test_anonymous_check_uses_verified_https_and_ignores_ambient_tokens(transport, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
    api, metadata, raw_release, _ = install_check(transport)
    result = check_for_update(REPO, "0.4.9")
    assert result.current_version == "0.4.9" and result.status == "available" and result.is_newer
    assert result.release.repository == REPO and result.release.version == VERSION
    assert result.release.notes == raw_release["body"]
    assert result.release.asset_id == 12 and result.release.sha256 == HASH
    assert result.release.release_url == f"https://github.com/{REPO}/releases/tag/v{VERSION}"
    assert len(transport.created) == 2  # Checking fetches only release JSON and the small manifest.
    for connection, args, kwargs in transport.created:
        assert args == ("api.github.com",) and kwargs["port"] == 443
        assert kwargs["context"].check_hostname and kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        method, path, body, headers = connection.calls[0]
        assert method == "GET" and body is None
        assert headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert "Authorization" not in headers and "Cookie" not in headers and "Proxy-Authorization" not in headers
        assert connection.debug == 0 and connection.auto_open == 0
        assert connection.closed and connection.response.closed
    assert api.calls[0][1] == f"/repos/{REPO}/releases/latest"
    assert metadata.calls[0][1] == f"/repos/{REPO}/releases/assets/11"
    assert metadata.calls[0][3]["Accept"] == "application/octet-stream"
    with pytest.raises(FrozenInstanceError):
        result.release.version = "9.9.9"


@pytest.mark.parametrize("installed,status,is_newer", [("0.4.13", "current", False), ("0.4.14", "ahead", False)])
def test_check_reports_same_or_newer_installed_version(transport, installed, status, is_newer):
    release, _, _ = release_data()
    transport(FakeResponse(release))
    result = check_for_update(REPO, installed)
    assert result.status == status and result.is_newer is is_newer
    assert not result.download_available and not result.release.is_downloadable
    assert len(transport.created) == 1  # No need to fetch any older/current package descriptor.


def test_private_token_only_reaches_api_and_notes_redact_it(transport):
    api, metadata, _, _ = install_check(transport, release_changes={"body": TOKEN + "\x00\n<em>notes</em>"})
    result = check_for_update(REPO, "0.4.12", session=FakeSession())
    assert result.release.notes == "[redacted]\n<em>notes</em>"
    for connection in (api, metadata):
        assert connection.calls[0][3]["Authorization"] == "Bearer " + TOKEN
        assert TOKEN not in connection.calls[0][1]
    assert TOKEN not in repr(result) + repr(updates._GitHubTransport(session=FakeSession()))


@pytest.mark.parametrize("changes", [{"draft": True}, {"draft": None}, {"prerelease": True}, {"prerelease": 0},
                                     {"tag_name": "0.4.13"}, {"tag_name": "v0.4.13-beta.1"}, {"assets": None}])
def test_non_stable_or_invalid_release_stops_before_any_asset_download(transport, changes):
    release, _, _ = release_data(release_changes=changes)
    transport(FakeResponse(release))
    with pytest.raises(UpdateError):
        check_for_update(REPO, "0.4.12")
    assert len(transport.created) == 1


def test_zip_only_release_is_downloadable_using_github_checksum_without_a_manifest(transport, tmp_path):
    api, raw_release = install_zip_check(transport)
    result = check_for_update(REPO, "0.4.12")
    assert result.status == "available" and result.download_available and result.is_newer
    assert result.release.is_downloadable and result.release.sha256 == HASH
    assert result.release.asset_id == 12 and result.release.asset_size == len(PACKAGE)
    assert result.release.version == VERSION and result.release.notes == raw_release["body"]
    assert len(transport.created) == 1 and api.calls[0][1] == f"/repos/{REPO}/releases/latest"
    install_download(transport)
    assert download_update(result.release, tmp_path).read_bytes() == PACKAGE
    assert len(transport.created) == 3  # release, rechecked asset, streamed ZIP; no descriptor request.


@pytest.mark.parametrize("installed,status", [(VERSION, "current"), ("0.4.14", "ahead")])
@pytest.mark.parametrize("assets", [[], None, [{"name": "studio-update.json", "id": 0}],
                                    [{"name": "other-source.zip", "digest": "invalid"}]])
def test_current_or_older_release_compares_without_any_package_or_asset_validation(transport, installed, status, assets):
    release, _, _ = release_data(release_changes={"assets": assets})
    transport(FakeResponse(release))
    result = check_for_update(REPO, installed)
    assert result.status == status and result.release.version == VERSION
    assert not result.download_available and not result.is_newer
    assert len(transport.created) == 1


def test_newer_release_without_assets_is_a_neutral_unavailable_state(transport):
    install_zip_check(transport, [])
    result = check_for_update(REPO, "0.4.12")
    assert result.status == "unavailable" and result.is_newer and not result.download_available
    assert result.release.version == VERSION and not result.release.is_downloadable
    assert "download is not ready" in result.message and "studio-update.json" not in result.message
    assert len(transport.created) == 1


@pytest.mark.parametrize("name", [
    "hoyo_project_studio_preview_editor_replacements.zip", "Studio-replacement.zip", "Studio-patch.zip",
    "Studio-source.zip", "studio_sources.zip", "studio-src.zip", "HoYoProjectStudio-linux-x64.zip",
    "HoYoProjectStudio-macos-x64.zip", "Studio-mac-arm64.zip", "Studio-windows-arm64.zip",
    "Studio-windows-aarch64.zip", "Studio-windows-x86.zip", "Studio-win32.zip", "Studio-winarm64.zip",
    "Studio-x32.zip", "Studio-32-bit.zip", "Studio-winx86.zip", "v" + VERSION + ".zip", VERSION + ".zip",
    "HoYoProjectStudio-9.9.9-windows-x64.zip", "Source code (zip)", "../../unsafe.zip", "CON.zip", "CON .zip",
    "studio.tar.gz", "studio.exe", "studio.zip:stream", "studio.zip\n", "C:\\studio.zip",
])
def test_known_incompatible_partial_source_or_unsafe_archives_are_not_offered(transport, name):
    install_zip_check(transport, [zip_asset(name)])
    result = check_for_update(REPO, "0.4.12")
    assert result.status == "unavailable" and not result.download_available
    assert len(transport.created) == 1


@pytest.mark.parametrize("name", ["studio.zip", "Studio-Windows.zip", "Studio-Windows-x86_64.zip"])
def test_one_unambiguous_generic_windows_zip_is_accepted_for_later_inner_validation(transport, name):
    install_zip_check(transport, [zip_asset(name)])
    result = check_for_update(REPO, "0.4.12")
    assert result.download_available and result.release.asset_name == name


@pytest.mark.parametrize("version", ["0.4.32", "0.32.0", "32.0.0"])
@pytest.mark.parametrize("prefix", ["HoYoProjectStudio", "Studio"])
def test_numeric_version_components_are_not_confused_with_32_bit_windows(transport, version, prefix):
    name = f"{prefix}-{version}-windows-x64.zip"
    release, _, _ = release_data(version=version)
    release["assets"] = [zip_asset(name)]
    transport(FakeResponse(release))
    result = check_for_update(REPO, "0.4.12")
    assert result.download_available and result.release.version == version
    assert result.release.asset_name == name
    assert len(transport.created) == 1


def test_legacy_package_version_32_is_also_a_valid_windows_x64_release(transport):
    install_check(transport, version="0.4.32")
    result = check_for_update(REPO, "0.4.12")
    assert result.download_available and result.release.version == "0.4.32"


@pytest.mark.parametrize("reverse", [False, True])
def test_exact_versioned_zip_is_preferred_over_known_full_and_generic_zips(transport, reverse):
    canonical = zip_asset()
    choices = [zip_asset("other.zip", asset_id=20),
               zip_asset("hoyo_project_studio_preview_tabs_code_editor.zip", asset_id=21), canonical]
    install_zip_check(transport, list(reversed(choices)) if reverse else choices)
    result = check_for_update(REPO, "0.4.12")
    assert result.download_available and result.release.asset_id == canonical["id"]


def test_delivered_full_zip_name_is_preferred_when_canonical_name_is_missing(transport):
    name = "hoyo_project_studio_preview_tabs_code_editor.zip"
    install_zip_check(transport, [zip_asset("other.zip", asset_id=20), zip_asset(name, asset_id=21),
                                  zip_asset("studio-source.zip", asset_id=22)])
    result = check_for_update(REPO, "0.4.12")
    assert result.download_available and result.release.asset_name == name


def test_other_platforms_and_partial_archives_do_not_make_one_generic_zip_ambiguous(transport):
    install_zip_check(transport, [zip_asset("studio-windows.zip"), zip_asset("studio-linux.zip", asset_id=20),
                                  zip_asset("studio-patch.zip", asset_id=21), zip_asset("studio-src.zip", asset_id=22)])
    result = check_for_update(REPO, "0.4.12")
    assert result.download_available and result.release.asset_name == "studio-windows.zip"


@pytest.mark.parametrize("assets", [
    [zip_asset("first.zip"), zip_asset("second.zip", asset_id=13)],
    [zip_asset(), zip_asset(asset_id=13)],
    [zip_asset(), zip_asset(zip_asset()["name"].upper(), asset_id=13)],
    [zip_asset(), zip_asset("another.zip")],
])
def test_ambiguous_zip_choices_or_conflicting_identities_never_enable_download(transport, assets):
    install_zip_check(transport, assets)
    with pytest.raises(UpdateError):
        check_for_update(REPO, "0.4.12")
    assert len(transport.created) == 1


@pytest.mark.parametrize("digest", [None, "omit"])
def test_zip_only_release_without_github_checksum_waits_without_weakening_validation(transport, digest):
    asset = zip_asset(digest=digest)
    if digest == "omit":
        asset.pop("digest")
    install_zip_check(transport, [asset])
    result = check_for_update(REPO, "0.4.12")
    assert result.status == "unavailable" and result.is_newer and not result.download_available
    assert "verified Windows download" in result.message


@pytest.mark.parametrize("digest", ["", True, 123, "sha1:" + HASH, "SHA256:" + HASH, "sha256:bad",
                                    "sha256:" + "0" * 63, "sha256:" + "g" * 64, "sha256:" + HASH + "\n"])
def test_zip_only_release_rejects_invalid_checksums_before_downloading(transport, digest):
    install_zip_check(transport, [zip_asset(digest=digest)])
    with pytest.raises(UpdateError, match="SHA-256"):
        check_for_update(REPO, "0.4.12")
    assert len(transport.created) == 1


def test_github_sha256_hex_is_case_insensitive(transport):
    install_zip_check(transport, [zip_asset(digest="sha256:" + HASH.upper())])
    result = check_for_update(REPO, "0.4.12")
    assert result.download_available and result.release.sha256 == HASH


@pytest.mark.parametrize("change", [{"id": 0}, {"id": True}, {"size": 0}, {"size": True},
                                    {"size": updates.MAX_PACKAGE_BYTES + 1}, {"state": "starter"}])
def test_zip_only_release_preserves_asset_identity_size_and_upload_state_checks(transport, change):
    asset = zip_asset()
    asset.update(change)
    install_zip_check(transport, [asset])
    with pytest.raises(UpdateError):
        check_for_update(REPO, "0.4.12")
    assert len(transport.created) == 1


def test_legacy_manifest_without_its_zip_is_unavailable_without_requesting_a_missing_file(transport):
    release, _, raw = release_data()
    release["assets"].pop()
    transport(FakeResponse(release))
    transport(FakeResponse(raw=raw))
    result = check_for_update(REPO, "0.4.12")
    assert result.status == "unavailable" and result.is_newer and not result.download_available
    assert "download is not ready" in result.message and "studio-update.json" not in result.message
    assert len(transport.created) == 2


def test_zip_only_asset_changed_after_check_requires_another_check(transport, tmp_path):
    install_zip_check(transport)
    result = check_for_update(REPO, "0.4.12")
    transport(FakeResponse(zip_asset(asset_id=99)))
    with pytest.raises(UpdateError, match="changed"):
        download_update(result.release, tmp_path)
    assert len(transport.created) == 2 and not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("version,expected", [("0.4.12", "ahead"), ("0.4.13", "current"), ("0.4.14", "unavailable")])
def test_metadata_only_release_never_makes_a_download_available(version, expected, transport, tmp_path):
    release = replace(reviewed_release(), version=version, tag="v" + version,
                      asset_id=0, asset_name="", asset_size=0, sha256="")
    result = UpdateCheck("0.4.13", release)
    assert result.status == expected and not result.download_available
    with pytest.raises(UpdateError, match="no verified update"):
        download_update(release, tmp_path)
    assert not transport.created and not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("asset_changes", [{"size": updates.MAX_MANIFEST_BYTES + 1}, {"size": 0}, {"size": True},
                                           {"id": True}, {"id": 0}, {"state": "starter"}])
def test_incomplete_or_oversized_manifest_is_never_requested(transport, asset_changes):
    release, _, _ = release_data()
    release["assets"][0].update(asset_changes)
    transport(FakeResponse(release))
    with pytest.raises(UpdateError):
        check_for_update(REPO, "0.4.12")
    assert len(transport.created) == 1


@pytest.mark.parametrize("changes", [{"schema_version": True}, {"schema_version": 2}, {"application": "other-app"},
                                     {"platform": "linux-x64"}, {"layout": "flat"}, {"version": "0.4.14"},
                                     {"version": "v0.4.13"}, {"package": None}])
def test_incompatible_manifest_or_mismatched_tag_is_rejected(transport, changes):
    install_check(transport, manifest_changes=changes)
    with pytest.raises(UpdateError):
        check_for_update(REPO, "0.4.12")


@pytest.mark.parametrize("changes", [{"name": "../../evil.zip"}, {"name": "C:\\evil.zip"}, {"name": "CON.zip"}, {"name": "CON .zip"},
                                     {"name": "payload.exe"}, {"name": "x.zip:stream"}, {"name": "x.zip\n"},
                                     {"size": -1}, {"size": True}, {"size": updates.MAX_PACKAGE_BYTES + 1},
                                     {"sha256": "bad"}, {"sha256": None}])
def test_package_metadata_rejects_unsafe_names_sizes_and_hashes(transport, changes):
    install_check(transport, package_changes=changes)
    with pytest.raises(UpdateError):
        check_for_update(REPO, "0.4.12")


@pytest.mark.parametrize("mutation", ["duplicate_manifest", "duplicate_zip", "same_id", "size", "digest"])
def test_ambiguous_or_changed_assets_do_not_produce_an_update(transport, mutation):
    release, _, raw = release_data()
    if mutation == "duplicate_manifest":
        release["assets"].append(dict(release["assets"][0]))
    elif mutation == "duplicate_zip":
        release["assets"].append(dict(release["assets"][1]))
    elif mutation == "same_id":
        release["assets"][1]["id"] = 11
    elif mutation == "size":
        release["assets"][1]["size"] += 1
    elif mutation == "digest":
        release["assets"][1]["digest"] = "sha256:" + "0" * 64
    transport(FakeResponse(release))
    if mutation != "duplicate_manifest":
        transport(FakeResponse(raw=raw))
    with pytest.raises(UpdateError):
        check_for_update(REPO, "0.4.12")


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}',
                                  b'"text"', b"\xff", b'{"a":' + b"[" * 1100 + b"]" * 1100 + b"}"])
def test_invalid_json_duplicate_keys_and_nonfinite_numbers_are_rejected(transport, body):
    transport(FakeResponse(raw=body))
    with pytest.raises(UpdateError):
        check_for_update(REPO, "0.4.12")


@pytest.mark.parametrize("status,match", [(401, "sign-in"), (403, "Contents: read"),
                                        (429, "request limit"), (500, "temporarily"), (302, "redirected")])
def test_http_errors_are_actionable_and_never_echo_sensitive_server_data(transport, status, match):
    transport(FakeResponse({"message": TOKEN}, status=status, headers={"Location": "https://evil.invalid/" + TOKEN}))
    with pytest.raises(UpdateError, match=match) as caught:
        check_for_update(REPO, "0.4.12")
    assert caught.value.status == status
    assert TOKEN not in str(caught.value) and "evil.invalid" not in str(caught.value)
    assert len(transport.created) == 1


@pytest.mark.parametrize("session", [None, FakeSession()])
def test_latest_404_reports_no_release_only_after_verifying_repository_identity(transport, session):
    latest = transport(FakeResponse({"message": TOKEN}, status=404))
    repository = transport(FakeResponse({"id": 100, "full_name": REPO.upper(), "private": session is not None}))
    result = check_for_update(REPO, "0.4.12", session=session)
    assert result.release is None and result.status == "none"
    assert not result.is_newer and not result.download_available
    assert "No published update" in result.message and "up to date" not in result.message.lower()
    assert TOKEN not in repr(result)
    assert latest.calls[0][1] == f"/repos/{REPO}/releases/latest"
    assert repository.calls[0][1] == f"/repos/{REPO}"
    for connection in (latest, repository):
        assert connection.calls[0][3]["Accept"] == "application/vnd.github+json"
        if session is None:
            assert "Authorization" not in connection.calls[0][3]
        else:
            assert connection.calls[0][3]["Authorization"] == "Bearer " + TOKEN
    assert len(transport.created) == 2


@pytest.mark.parametrize("status,match", [(404, "accessible"), (401, "sign-in"), (403, "Contents: read"),
                                        (429, "request limit"), (500, "temporarily"), (301, "redirected")])
def test_latest_404_does_not_hide_missing_repo_auth_rate_limit_or_server_failures(transport, status, match):
    transport(FakeResponse({"message": TOKEN}, status=404))
    transport(FakeResponse({"message": TOKEN}, status=status,
                           headers={"Location": "https://evil.invalid/" + TOKEN}))
    with pytest.raises(UpdateError, match=match) as caught:
        check_for_update(REPO, "0.4.12")
    assert caught.value.status == status and TOKEN not in str(caught.value)
    assert len(transport.created) == 2


@pytest.mark.parametrize("identity", [None, {}, {"id": True, "full_name": REPO}, {"id": 0, "full_name": REPO},
                                       {"id": 100, "full_name": "another/repository"},
                                       {"id": 100, "full_name": "https://github.com/" + REPO},
                                       {"id": 100, "full_name": None}])
def test_latest_404_never_treats_unverified_repository_metadata_as_no_updates(transport, identity):
    transport(FakeResponse(status=404))
    transport(FakeResponse(identity))
    with pytest.raises(UpdateError):
        check_for_update(REPO, "0.4.12")
    assert len(transport.created) == 2


def test_cancellation_after_latest_404_prevents_repository_probe(transport):
    cancelled = threading.Event()
    first = transport(FakeResponse(status=404))
    original_close = first.close

    def close_and_cancel():
        original_close()
        cancelled.set()

    first.close = close_and_cancel
    with pytest.raises(UpdateError) as caught:
        check_for_update(REPO, "0.4.12", cancelled=cancelled)
    assert caught.value.cancelled and len(transport.created) == 1


@pytest.mark.parametrize("headers", [{"X-RateLimit-Remaining": "0"}, {"Retry-After": TOKEN}])
def test_github_secondary_and_primary_rate_limit_messages(transport, headers):
    transport(FakeResponse(status=403, headers=headers))
    with pytest.raises(UpdateError, match="request limit") as caught:
        check_for_update(REPO, "0.4.12")
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("host", sorted(updates.ASSET_HOSTS))
def test_documented_asset_redirects_drop_auth_and_cookie_headers(transport, host):
    redirect = transport(FakeResponse(status=302, headers={"Location": f"https://{host}/asset?signature=signed_value",
                                                         "Set-Cookie": "secret=cookie"}))
    binary = transport(FakeResponse(raw=PACKAGE, headers={"Content-Length": len(PACKAGE)}))
    client = updates._GitHubTransport(session=FakeSession())
    assert client.read(f"/repos/{REPO}/releases/assets/12", binary=True, expected_size=len(PACKAGE)) == PACKAGE
    assert redirect.calls[0][3]["Authorization"] == "Bearer " + TOKEN
    assert transport.created[1][1] == (host,)
    method, path, body, headers = binary.calls[0]
    assert method == "GET" and body is None and path == "/asset?signature=signed_value"
    assert "Authorization" not in headers and "Cookie" not in headers and "Referer" not in headers
    assert "X-GitHub-Api-Version" not in headers


@pytest.mark.parametrize("location", [
    "http://release-assets.githubusercontent.com/x", "https://evil.invalid/x", "https://127.0.0.1/x",
    "https://api.github.com/user", "https://release-assets.githubusercontent.com.evil.invalid/x",
    "https://user:password@objects.githubusercontent.com/x", "https://objects.githubusercontent.com:444/x",
    "https://objects.githubusercontent.com/x#fragment", "//objects.githubusercontent.com/x",
    "https://objects.githubusercontent.com/x%0aInjected", "https://objects.githubusercontent.com/x\\y",
    "https://objects.githubusercontent.com/" + TOKEN, "https://objects.githubusercontent.com/x?secret=" + TOKEN,
    "https://objects.githubusercontent.com/x\nHeader:value", None,
])
def test_asset_redirects_reject_untrusted_hosts_downgrades_and_token_urls(transport, location):
    headers = {"Location": location} if location is not None else {}
    transport(FakeResponse(status=302, headers=headers))
    client = updates._GitHubTransport(session=FakeSession())
    with pytest.raises(UpdateError) as caught:
        client.read(f"/repos/{REPO}/releases/assets/12", binary=True)
    assert TOKEN not in str(caught.value)
    assert len(transport.created) == 1


def test_redirect_chain_is_bounded_and_never_reauthenticates(transport):
    for index in range(updates.MAX_REDIRECTS + 1):
        transport(FakeResponse(status=302, headers={"Location": f"https://objects.githubusercontent.com/{index}"}))
    with pytest.raises(UpdateError, match="too many"):
        updates._GitHubTransport(session=FakeSession()).read("/asset", binary=True)
    assert len(transport.created) == updates.MAX_REDIRECTS + 1
    for connection, _, _ in transport.created[1:]:
        assert "Authorization" not in connection.calls[0][3]


@pytest.mark.parametrize("headers,raw", [({"Content-Length": "-1"}, b"{}"), ({"Content-Length": "nan"}, b"{}"),
                                        ({"Content-Length": "999999999999999999999"}, b"{}"),
                                        ({"Content-Length": "500"}, b"{}"), ({"Content-Length": "3"}, b"{}"),
                                        ({"Content-Encoding": "gzip"}, b"{}"), ({}, b"x" * 101)])
def test_transport_bounds_actual_and_declared_size_and_rejects_encoding(transport, headers, raw):
    response = FakeResponse(raw=raw, headers=headers)
    transport(response)
    with pytest.raises(UpdateError):
        updates._GitHubTransport().read("/test", maximum=100)
    assert sum(response.read_sizes) <= 202


def test_download_rechecks_asset_identity_verifies_hash_and_reports_progress(transport, tmp_path):
    api, binary = install_download(transport)
    existing = tmp_path / reviewed_release().asset_name
    existing.write_bytes(b"keep older download")
    progress = []
    result = download_update(reviewed_release(), tmp_path, progress=lambda done, total: progress.append((done, total)))
    assert result.parent.parent == tmp_path and result.parent.name.startswith("studio-download-")
    assert result.name == reviewed_release().asset_name and result.read_bytes() == PACKAGE
    assert existing.read_bytes() == b"keep older download"
    assert progress[0] == (0, len(PACKAGE)) and progress[-1] == (len(PACKAGE), len(PACKAGE))
    assert api.calls[0][1] == binary.calls[0][1] == f"/repos/{REPO}/releases/assets/12"
    assert api.calls[0][3]["Accept"] == "application/vnd.github+json"
    assert not tuple(result.parent.glob("*.part"))


def test_download_hashes_payload_even_when_github_omits_its_optional_digest(transport, tmp_path):
    install_download(transport, digest=None)
    assert download_update(reviewed_release(), tmp_path).read_bytes() == PACKAGE


@pytest.mark.parametrize("changed", [{"id": 13}, {"size": len(PACKAGE) + 1}, {"name": "replaced.zip"},
                                     {"digest": "sha256:" + "0" * 64}, {"state": "starter"}])
def test_asset_changed_after_review_stops_before_downloading(transport, tmp_path, changed):
    install_download(transport, changed=changed)
    with pytest.raises(UpdateError):
        download_update(reviewed_release(), tmp_path)
    assert len(transport.created) == 1 and not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("payload,headers", [(b"x" * len(PACKAGE), {}), (PACKAGE[:-1], {}),
                                           (PACKAGE + b"unexpected", {}), (PACKAGE, {"Content-Length": len(PACKAGE) + 1})])
def test_corrupt_or_incomplete_download_never_leaves_installable_file(transport, tmp_path, payload, headers):
    install_download(transport, raw=payload, headers=headers)
    existing = tmp_path / "keep.txt"
    existing.write_text("user file")
    with pytest.raises(UpdateError):
        download_update(reviewed_release(), tmp_path)
    assert tuple(tmp_path.iterdir()) == (existing,)


def test_transfer_is_streamed_in_bounded_chunks_and_cancel_removes_partial_files(transport, tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "CHUNK_BYTES", 8)
    _, binary = install_download(transport)
    cancelled = threading.Event()
    progress = []

    def cancel_after_first_chunk(done, total):
        progress.append((done, total))
        if done:
            cancelled.set()

    with pytest.raises(UpdateError) as caught:
        download_update(reviewed_release(), tmp_path, cancelled=cancelled, progress=cancel_after_first_chunk)
    assert caught.value.cancelled and binary.response.offset == 8
    assert not tuple(tmp_path.iterdir())
    assert progress == [(0, len(PACKAGE)), (8, len(PACKAGE))]


def test_pre_cancelled_check_does_not_connect(transport):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(UpdateError) as caught:
        check_for_update(REPO, "0.4.12", cancelled=cancelled)
    assert caught.value.cancelled and not transport.created


def test_cancel_during_connect_cannot_send_headers_after_connection_completes(transport):
    cancelled = threading.Event()
    connection = transport(FakeResponse())
    connection.connect_action = cancelled.set
    with pytest.raises(UpdateError) as caught:
        check_for_update(REPO, "0.4.12", session=FakeSession(), cancelled=cancelled)
    assert caught.value.cancelled and connection.calls == [] and connection.closed


def test_stalled_dns_is_cancellable_and_late_dns_result_cannot_make_request(transport):
    cancelled, entered, release = threading.Event(), threading.Event(), threading.Event()
    connection = transport(FakeResponse())

    def stall():
        entered.set()
        release.wait(3)

    connection.connect_action = stall
    errors = []

    def run():
        try:
            check_for_update(REPO, "0.4.12", cancelled=cancelled)
        except UpdateError as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(2)
        cancelled.set()
        worker.join(1)
        assert not worker.is_alive() and errors and errors[0].cancelled
    finally:
        release.set()
        worker.join(3)
    assert connection.calls == []


def test_cancellation_interrupts_only_the_owned_socket(transport):
    cancelled = threading.Event()
    connection = transport(FakeResponse())

    def blocked_response():
        cancelled.set()
        assert connection.socket.stopped.wait(2)
        raise OSError(TOKEN)

    connection.response_action = blocked_response
    with pytest.raises(UpdateError) as caught:
        check_for_update(REPO, "0.4.12", cancelled=cancelled)
    assert caught.value.cancelled and TOKEN not in str(caught.value)


def test_request_deadline_interrupts_a_stalled_response(transport, monkeypatch):
    connection = transport(FakeResponse())

    def blocked_response():
        assert connection.socket.stopped.wait(2)
        raise OSError(TOKEN)

    connection.response_action = blocked_response
    with pytest.raises(UpdateError, match="timed out"):
        updates._GitHubTransport().read("/test", timeout=0.1)


@pytest.mark.parametrize("exception,match", [(ssl.SSLCertVerificationError(TOKEN), "verify"),
                                           (socket.timeout(TOKEN), "timed out"),
                                           (OSError(TOKEN), "transfer")])
def test_tls_network_and_timeout_failures_do_not_expose_transport_secrets(transport, exception, match):
    connection = transport(FakeResponse())

    def fail():
        raise exception

    connection.connect_action = fail
    with pytest.raises(UpdateError, match=match) as caught:
        check_for_update(REPO, "0.4.12", session=FakeSession())
    assert TOKEN not in str(caught.value) and connection.calls == []


@pytest.mark.parametrize("next_token", [None, OTHER_TOKEN, "invalid changed sign-in 日本語"])
def test_sign_out_or_account_change_invalidates_a_pending_update(transport, next_token):
    session = FakeSession()
    connection = transport(FakeResponse())
    connection.connect_action = lambda: setattr(session, "token", next_token)
    with pytest.raises(UpdateError) as caught:
        check_for_update(REPO, "0.4.12", session=session)
    assert caught.value.cancelled and connection.calls == []
    assert TOKEN not in str(caught.value) and OTHER_TOKEN not in str(caught.value)


def test_explicit_private_mode_without_sign_in_fails_locally(transport):
    with pytest.raises(UpdateError, match="Connect your GitHub") as caught:
        check_for_update(REPO, "0.4.12", session=FakeSession(None))
    assert TOKEN not in str(caught.value) and not transport.created


def test_failed_download_write_cleans_only_its_own_folder(transport, tmp_path, monkeypatch):
    install_download(transport)
    original = Path.open

    def fail_partial(path, *args, **kwargs):
        if path.name == "package.zip.part":
            raise PermissionError("private filesystem details")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_partial)
    keep = tmp_path / "keep"
    keep.mkdir()
    with pytest.raises(UpdateError, match="save"):
        download_update(reviewed_release(), tmp_path)
    assert tuple(tmp_path.iterdir()) == (keep,)


@pytest.mark.parametrize("field,value", [("asset_id", True), ("asset_size", -1), ("asset_name", "../unsafe.zip"),
                                       ("sha256", "bad"), ("repository", "https://evil.invalid/o/r"),
                                       ("tag", "v9.9.9")])
def test_forged_or_stale_download_object_is_validated_before_network(transport, tmp_path, field, value):
    with pytest.raises(UpdateError):
        download_update(replace(reviewed_release(), **{field: value}), tmp_path)
    assert not transport.created and not tuple(tmp_path.iterdir())
