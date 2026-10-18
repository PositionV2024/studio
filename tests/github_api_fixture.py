"""In-memory GitHub REST boundary with real Git object hashes; never a network."""
from __future__ import annotations

import base64
import copy
import hashlib
from urllib.parse import unquote

from studio.github_connection import GitHubError


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class FakeGitHub:
    def __init__(self, *, login="clarence", private=True, repository="clarence/project", existing=False):
        self.login = login
        self.account_id = 123
        self.private = private
        self.repository = repository
        self.exists = existing
        self.repository_id = 456
        self.owner_id = 123
        self.owner_type = "User"
        self.default_branch = "main"
        self.creation_default_branch = "main"
        self.branches = {}
        self.blobs = {}
        self.trees = {}
        self.commits = {}
        self.calls = []
        self.fail_create = False
        self.fail_push = False
        self.fail_for = {}
        self.before_request = None
        self.truncated = False
        self.permissions = {"push": True}
        self.users = {}

    def get_authenticated_user(self):
        return self.request("GET", "/user")

    def metadata(self):
        return {"id": self.repository_id, "full_name": self.repository, "private": self.private,
                "default_branch": self.default_branch, "owner": {"id": self.owner_id, "login": self.repository.split('/')[0], "type": self.owner_type},
                "permissions": self.permissions.copy(), "archived": False, "disabled": False}

    def put_blob(self, data):
        sha = blob_sha(data)
        self.blobs[sha] = data
        return sha

    def put_tree(self, leaves):
        nodes = {"": {}}
        for path, entry in leaves.items():
            parts = path.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index - 1])
                name = parts[index - 1]
                nodes.setdefault(parent, {})[name] = {"type": "tree", "mode": "040000", "sha": ""}
                nodes.setdefault("/".join(parts[:index]), {})
            nodes.setdefault("/".join(parts[:-1]), {})[parts[-1]] = dict(entry)
        entries = []
        for path in sorted(nodes, key=lambda value: (value.count("/"), len(value)), reverse=True):
            tree = nodes[path]
            body = b""
            for name, entry in sorted(tree.items(), key=lambda value: (value[0] + ("/" if value[1]["type"] == "tree" else "")).encode()):
                body += entry["mode"].lstrip("0").encode() + b" " + name.encode() + b"\0" + bytes.fromhex(entry["sha"])
                record = {"path": path + "/" + name if path else name, **entry}
                entries.append(record)
            sha = hashlib.sha1(b"tree " + str(len(body)).encode() + b"\0" + body).hexdigest()
            if path:
                parent, _, name = path.rpartition("/")
                nodes[parent][name]["sha"] = sha
        root_sha = sha
        self.trees[root_sha] = {"sha": root_sha, "tree": sorted(entries, key=lambda entry: entry["path"]), "truncated": False}
        return root_sha

    def put_commit(self, tree, parents=(), message="Fixture commit"):
        body = f"tree {tree}\n" + "".join(f"parent {parent}\n" for parent in parents)
        body += "author Fixture <fixture@example.invalid> 1234567890 +0000\ncommitter Fixture <fixture@example.invalid> 1234567890 +0000\n\n" + message + "\n"
        content = body.encode()
        sha = hashlib.sha1(b"commit " + str(len(content)).encode() + b"\0" + content).hexdigest()
        self.commits[sha] = {"sha": sha, "tree": {"sha": tree}, "parents": [{"sha": parent} for parent in parents], "message": message}
        return sha

    def seed(self, files):
        self.exists = True
        leaves = {}
        for path, value in files.items():
            if isinstance(value, tuple):
                data, mode = value
            else:
                data, mode = value, "100644"
            if isinstance(data, str):
                data = data.encode()
            leaves[path] = {"sha": self.put_blob(data), "type": "blob", "mode": mode, "size": len(data)}
        tree = self.put_tree(leaves)
        revision = self.put_commit(tree)
        self.branches[self.default_branch] = revision
        return revision

    def tree_for(self, branch):
        tree = self.trees[self.commits[self.branches[branch]]["tree"]["sha"]]
        return {entry["path"]: (self.blobs.get(entry["sha"], b""), entry["mode"]) for entry in tree["tree"] if entry["type"] != "tree"}

    def reference(self, branch):
        if branch not in self.branches:
            raise GitHubError("Missing reference", status=404 if self.branches else 409)
        return {"ref": "refs/heads/" + branch, "object": {"type": "commit", "sha": self.branches[branch]}}

    def _request(self, method, path, data=None):
        if method == "GET" and path == "/user":
            return {"login": self.login, "id": self.account_id, "name": "Clarence", "html_url": "https://github.com/" + self.login}
        if method == "GET" and path.startswith("/users/"):
            owner = unquote(path.removeprefix("/users/"))
            if owner in self.users:
                return self.users[owner]
            raise GitHubError("Missing user", status=404)
        if method == "POST" and (path == "/user/repos" or path.startswith("/orgs/") and path.endswith("/repos")):
            if self.exists or self.fail_create:
                raise GitHubError("Repository already exists", status=422)
            self.exists = True
            self.private = data["private"]
            assert data.get("auto_init") is False
            assert data["name"] == self.repository.split("/")[1]
            self.default_branch = self.creation_default_branch
            return self.metadata()
        prefix = "/repos/" + self.repository
        if not path.startswith(prefix) or not self.exists:
            raise GitHubError("Repository unavailable", status=404)
        endpoint = path[len(prefix):]
        if method == "GET" and endpoint == "":
            return self.metadata()
        if method == "GET" and endpoint == "/branches?per_page=1":
            return [{"name": branch, "commit": {"sha": sha}} for branch, sha in sorted(self.branches.items())][:1]
        if method == "GET" and endpoint.startswith("/git/ref/heads/"):
            return self.reference(unquote(endpoint.removeprefix("/git/ref/heads/")))
        if method == "GET" and endpoint.startswith("/git/commits/"):
            return self.commits[endpoint.removeprefix("/git/commits/")]
        if method == "GET" and endpoint.startswith("/git/trees/"):
            result = copy.deepcopy(self.trees[endpoint.removeprefix("/git/trees/").removesuffix("?recursive=1")])
            result["truncated"] = self.truncated
            return result
        if method == "GET" and endpoint.startswith("/git/blobs/"):
            sha = endpoint.removeprefix("/git/blobs/")
            content = self.blobs[sha]
            return {"sha": sha, "size": len(content), "encoding": "base64", "content": base64.encodebytes(content).decode()}
        if self.fail_push:
            raise GitHubError("Network unavailable")
        if method == "PUT" and endpoint.startswith("/contents/"):
            filepath = unquote(endpoint.removeprefix("/contents/"))
            branch = data.get("branch", self.default_branch)
            if branch not in self.branches and self.branches:
                raise GitHubError("Branch missing", status=404)
            contents = base64.b64decode(data["content"])
            if branch in self.branches:
                leaves = {entry["path"]: entry for entry in self.trees[self.commits[self.branches[branch]]["tree"]["sha"]]["tree"] if entry["type"] != "tree"}
                if filepath in leaves:
                    raise GitHubError("File already exists; sha required", status=422)
                parents = [self.branches[branch]]
            else:
                leaves, parents = {}, []
            sha = self.put_blob(contents)
            leaves[filepath] = {"mode": "100644", "type": "blob", "sha": sha, "size": len(contents)}
            tree = self.put_tree(leaves)
            revision = self.put_commit(tree, parents, data["message"])
            self.branches[branch] = revision
            return {"content": {"path": filepath, "sha": sha}, "commit": self.commits[revision]}
        if not self.branches:
            raise GitHubError("Git repository is empty", status=409)
        if method == "POST" and endpoint == "/git/blobs":
            assert data["encoding"] == "base64"
            return {"sha": self.put_blob(base64.b64decode(data["content"]))}
        if method == "POST" and endpoint == "/git/trees":
            leaves = {entry["path"]: dict(entry) for entry in self.trees[data["base_tree"]]["tree"] if entry["type"] != "tree"}
            for entry in data["tree"]:
                assert entry["sha"] is not None, "Publisher must not request deletions"
                leaves[entry["path"]] = {**entry, "size": len(self.blobs[entry["sha"]])}
            sha = self.put_tree(leaves)
            return self.trees[sha]
        if method == "POST" and endpoint == "/git/commits":
            sha = self.put_commit(data["tree"], data["parents"], data["message"])
            return self.commits[sha]
        if method == "POST" and endpoint == "/git/refs":
            branch = data["ref"].removeprefix("refs/heads/")
            if branch in self.branches:
                raise GitHubError("Branch exists", status=422)
            assert data["sha"] in self.commits
            self.branches[branch] = data["sha"]
            return self.reference(branch)
        if method == "PATCH" and endpoint.startswith("/git/refs/heads/"):
            assert data.get("force") is False
            branch = unquote(endpoint.removeprefix("/git/refs/heads/"))
            current = self.branches[branch]
            if current not in [parent["sha"] for parent in self.commits[data["sha"]]["parents"]]:
                raise GitHubError("Not a fast-forward", status=422)
            self.branches[branch] = data["sha"]
            return self.reference(branch)
        raise AssertionError(f"Unexpected GitHub request: {method} {endpoint}")

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if self.before_request is not None:
            self.before_request(self, method, path, data)
        failure = self.fail_for.get((method, path))
        if failure:
            raise failure
        return copy.deepcopy(self._request(method, path, data))
