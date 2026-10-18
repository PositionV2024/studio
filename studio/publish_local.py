"""Read project publishing metadata without Git or other executables.

Only project-local ignore files and the origin URL in local Git metadata are
read. Git config includes, credential helpers, hooks, URL rewrites and global
configuration are never evaluated. The caller still validates the destination
and independently excludes credentials, generated files and symbolic links.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


_MAX_IGNORE_BYTES = 1_000_000
_MAX_IGNORE_TOTAL = 8_000_000
_MAX_IGNORE_RULES = 20_000
_MAX_PATTERN_LENGTH = 4096
_MAX_CONFIG_BYTES = 1_000_000
_MAX_POINTER_BYTES = 8192
_ASCII_FOLD = bytes.maketrans(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ", b"abcdefghijklmnopqrstuvwxyz")

# Git's POSIX character classes are byte-oriented ASCII classes.
_CHAR_CLASSES = {
    "alnum": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "alpha": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "blank": " \t",
    "cntrl": "".join(chr(value) for value in range(32)) + "\x7f",
    "digit": "0123456789",
    "graph": "".join(chr(value) for value in range(33, 127)),
    "lower": "abcdefghijklmnopqrstuvwxyz",
    "print": "".join(chr(value) for value in range(32, 127)),
    "punct": "".join(chr(value) for value in range(33, 127) if not chr(value).isalnum()),
    "space": " \t\r\n\v\f",
    "upper": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "xdigit": "0123456789abcdefABCDEF",
}


def _read_regular(path: Path, limit: int) -> bytes | None:
    """Bound reads and skip symbolic links, including a last-component race."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode):
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Expected a regular file: {path.name}")
    if before.st_size > limit:
        raise ValueError(f"The {path.name} file is too large to read safely.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if not stat.S_ISREG(actual.st_mode) or (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError(f"The {path.name} file changed while it was being read.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"The {path.name} file is too large to read safely.")
    return data


def _trim_unescaped_spaces(line: str) -> str:
    end = len(line)
    while end and line[end - 1] == " ":
        cursor = end - 2
        while cursor >= 0 and line[cursor] == "\\":
            cursor -= 1
        if (end - 2 - cursor) % 2:
            break
        end -= 1
    return line[:end]


def _wildmatch_text(value: str) -> str:
    # Git wildcard matching operates on filename bytes. In particular, a '?'
    # consumes one byte rather than an entire multi-byte Unicode character.
    data = value.encode("utf-8", errors="surrogateescape")
    if os.name == "nt":
        data = data.translate(_ASCII_FOLD)
    return data.decode("latin1")


@dataclass(frozen=True)
class _CharacterClass:
    negate: bool
    literals: frozenset[str]
    ranges: tuple[tuple[str, str], ...]

    def matches(self, character: str) -> bool:
        if character == "/":
            return False
        included = character in self.literals or any(low <= character <= high for low, high in self.ranges)
        return not included if self.negate else included


def _character_class(pattern: str, start: int) -> tuple[_CharacterClass, int] | None:
    cursor = start + 1
    negate = cursor < len(pattern) and pattern[cursor] in "!^"
    cursor += int(negate)
    atoms: list[tuple[str, object]] = []
    if cursor < len(pattern) and pattern[cursor] == "]":
        atoms.append(("literal", "]"))
        cursor += 1
    while cursor < len(pattern):
        character = pattern[cursor]
        if character == "]":
            literals: set[str] = set()
            ranges: list[tuple[str, str]] = []
            index = 0
            while index < len(atoms):
                if (index + 2 < len(atoms) and atoms[index][0] == "literal"
                        and atoms[index + 1] == ("dash", "-") and atoms[index + 2][0] == "literal"):
                    # Wildmatch evaluates the first literal before the range;
                    # even an empty/reversed range still matches that literal.
                    literals.add(str(atoms[index][1]))
                    ranges.append((str(atoms[index][1]), str(atoms[index + 2][1])))
                    index += 3
                else:
                    literals.update(str(atoms[index][1]))
                    index += 1
            return _CharacterClass(negate, frozenset(literals), tuple(ranges)), cursor + 1
        if character == "\\":
            cursor += 1
            if cursor == len(pattern):
                return None
            atoms.append(("literal", pattern[cursor]))
        elif pattern.startswith("[:", cursor):
            end = pattern.find(":]", cursor + 2)
            if end < 0 or pattern[cursor + 2:end] not in _CHAR_CLASSES:
                return None
            members = _CHAR_CLASSES[pattern[cursor + 2:end]]
            atoms.append(("class", members.lower() if os.name == "nt" else members))
            cursor = end + 1
        else:
            atoms.append(("dash" if character == "-" else "literal", character))
        cursor += 1
    # Like Git wildmatch, an unterminated bracket expression never matches.
    return None


def _glob_tokens(pattern: str) -> tuple[tuple[str, object], ...] | None:
    tokens: list[tuple[str, object]] = []
    cursor = 0
    while cursor < len(pattern):
        character = pattern[cursor]
        if character == "\\":
            cursor += 1
            if cursor == len(pattern):
                return None
            tokens.append(("literal", pattern[cursor]))
        elif character == "*":
            start = cursor
            while cursor < len(pattern) and pattern[cursor] == "*":
                cursor += 1
            recursive = (cursor - start >= 2 and (start == 0 or pattern[start - 1] == "/")
                         and (cursor == len(pattern) or pattern[cursor] == "/"))
            if recursive and cursor < len(pattern):
                tokens.append(("directories", ""))
                cursor += 1
            else:
                tokens.append(("recursive" if recursive else "star", ""))
            continue
        elif character == "?":
            tokens.append(("any", ""))
        elif character == "[":
            parsed = _character_class(pattern, cursor)
            if parsed is None:
                return None
            character_class, cursor = parsed
            tokens.append(("class", character_class))
            continue
        else:
            tokens.append(("literal", character))
        cursor += 1
    return tuple(tokens)


def _glob_matches(tokens: tuple[tuple[str, object], ...], text: str) -> bool:
    """Evaluate wildcard states without recursive/exponential regex matching."""
    positions = {0}
    length = len(text)
    for kind, value in tokens:
        if not positions:
            return False
        if kind == "recursive":
            positions = set(range(min(positions), length + 1))
        elif kind == "directories":
            first = min(positions)
            positions |= {position for position in range(first + 1, length + 1) if text[position - 1] == "/"}
        elif kind == "star":
            advanced: set[int] = set()
            active = False
            for position in range(min(positions), length + 1):
                if position in positions:
                    active = True
                elif position and text[position - 1] == "/":
                    active = False
                if active:
                    advanced.add(position)
            positions = advanced
        elif kind == "any":
            positions = {position + 1 for position in positions if position < length and text[position] != "/"}
        elif kind == "class":
            assert isinstance(value, _CharacterClass)
            positions = {position + 1 for position in positions if position < length and value.matches(text[position])}
        else:
            positions = {position + 1 for position in positions if position < length and text[position] == value}
    return length in positions


@dataclass(frozen=True)
class _IgnoreRule:
    base: str
    tokens: tuple[tuple[str, object], ...]
    negate: bool
    directory_only: bool
    anchored: bool

    def matches(self, path: str, directory: bool) -> bool:
        if self.directory_only and not directory:
            return False
        relative = path[len(self.base):]
        if not self.anchored:
            relative = relative.rsplit("/", 1)[-1]
        return _glob_matches(self.tokens, _wildmatch_text(relative))


def ignored_project_paths(
    root: Path, candidates: Iterable[str], cancelled: Callable[[], None] | None = None,
) -> set[str]:
    """Return ignored relative file paths, respecting nested .gitignore rules.

    ``cancelled`` is a callback which raises the caller's cancellation error.
    Unreadable or oversized ignore files stop the scan instead of silently
    including files which the user expected to remain excluded. No repository
    is initialized. Neither global excludes nor executable Git configuration
    are read, matching this publisher's previously isolated ignore scan.
    """
    root = Path(root).expanduser().resolve()
    check_cancelled = cancelled or (lambda: None)
    total_bytes = 0
    total_rules = 0

    def load_rules(directory: str) -> tuple[_IgnoreRule, ...]:
        nonlocal total_bytes, total_rules
        check_cancelled()
        path = root.joinpath(*directory.split("/")) / ".gitignore" if directory else root / ".gitignore"
        try:
            data = _read_regular(path, _MAX_IGNORE_BYTES)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Could not read {path.relative_to(root).as_posix()} rules: {exc}") from exc
        if data is None:
            return ()
        total_bytes += len(data)
        if total_bytes > _MAX_IGNORE_TOTAL:
            raise ValueError("This project contains too much .gitignore data to review safely.")
        rules = []
        base = directory + "/" if directory else ""
        for number, raw in enumerate(data.decode("utf-8-sig", errors="surrogateescape").split("\n"), 1):
            if number % 128 == 0:
                check_cancelled()
            line = _trim_unescaped_spaces(raw.removesuffix("\r"))
            if not line or line.startswith("#"):
                continue
            if "\0" in line or len(line) > _MAX_PATTERN_LENGTH:
                raise ValueError(f"Cannot safely read the pattern at {path.relative_to(root).as_posix()}:{number}.")
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            directory_only = line.endswith("/")
            if directory_only:
                line = line[:-1]
            anchored = "/" in line
            line = line.removeprefix("/")
            if not line:
                continue
            tokens = _glob_tokens(_wildmatch_text(line))
            if tokens is None:
                continue
            rules.append(_IgnoreRule(base, tokens, negate, directory_only, anchored))
            total_rules += 1
            if total_rules > _MAX_IGNORE_RULES:
                raise ValueError("This project contains too many .gitignore patterns to review safely.")
        return tuple(rules)

    def is_ignored(path: str, rules: tuple[_IgnoreRule, ...], directory: bool) -> bool:
        ignored = False
        for index, rule in enumerate(rules):
            if index % 128 == 0:
                check_cancelled()
            if rule.matches(path, directory):
                ignored = not rule.negate
        return ignored

    # Each directory is assessed before loading its own rules. This prevents
    # a negation inside an excluded directory from resurrecting its children.
    directories: dict[str, tuple[bool, tuple[_IgnoreRule, ...]]] = {"": (False, load_rules(""))}
    ignored: set[str] = set()
    for candidate in candidates:
        check_cancelled()
        parts = candidate.split("/")
        if not candidate or any(part in {"", ".", ".."} for part in parts) or "\0" in candidate:
            raise ValueError("A project file has an unsafe relative path.")
        if PurePosixPath(candidate).is_absolute() or (os.name == "nt" and ("\\" in candidate or ":" in candidate)):
            raise ValueError("A project file has an unsafe relative path.")
        parent = ""
        for part in parts[:-1]:
            current = parent + "/" + part if parent else part
            if current not in directories:
                parent_ignored, parent_rules = directories[parent]
                blocked = (parent_ignored or root.joinpath(*current.split("/")).is_symlink()
                           or is_ignored(current, parent_rules, True))
                directories[current] = (blocked, () if blocked else parent_rules + load_rules(current))
            parent = current
        parent_ignored, rules = directories[parent]
        if parent_ignored or is_ignored(candidate, rules, False):
            ignored.add(candidate)
    return ignored


def _local_metadata_path(base: Path, raw: str) -> Path | None:
    # Never follow a UNC/network pointer or Windows device path while filling
    # in a suggested repository. This is metadata discovery, not a Git clone.
    if not raw or any(ord(character) < 32 for character in raw) or raw.startswith(("//", "\\\\")):
        return None
    value = Path(raw)
    if os.name != "nt" and ("\\" in raw or re.match(r"^[A-Za-z]:", raw)):
        return None
    value = value if value.is_absolute() else base / value
    value = Path(os.path.abspath(value))
    try:
        if any(parent.is_symlink() for parent in (value, *value.parents)):
            return None
    except OSError:
        return None
    return value


def _config_value(raw: str) -> str:
    quoted = False
    result: list[tuple[str, bool]] = []
    cursor = 0
    escapes = {"n": "\n", "t": "\t", "b": "\b", "\\": "\\", '"': '"'}
    for_start = raw.lstrip(" \t")
    while cursor < len(for_start):
        character = for_start[cursor]
        if character in "#;" and not quoted:
            break
        if character == '"':
            quoted = not quoted
        elif character == "\\":
            cursor += 1
            if cursor >= len(for_start) or for_start[cursor] not in escapes:
                raise ValueError("Invalid Git config escape")
            result.append((escapes[for_start[cursor]], True))
        else:
            result.append((character, quoted))
        cursor += 1
    if quoted:
        raise ValueError("Unclosed Git config quote")
    while result and result[-1][0] in " \t" and not result[-1][1]:
        result.pop()
    return "".join(character for character, _preserved in result)


_SECTION = re.compile(r'^\[([A-Za-z0-9.-]+)(?:[ \t]+"((?:[^"\\]|\\.)*)")?[ \t]*\]')
_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)[ \t]*(?:=(.*)|[#;].*)?$")


def _config_entries(data: bytes) -> list[tuple[str, str, str, str]]:
    text = data.decode("utf-8-sig")
    if "\0" in text:
        raise ValueError("Invalid Git config content")
    logical_lines: list[str] = []
    pending = ""
    for physical in text.split("\n"):
        line = pending + physical.removesuffix("\r")
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2:
            pending = line[:-1]
            continue
        pending = ""
        logical_lines.append(line)
    if pending:
        raise ValueError("Incomplete Git config continuation")
    entries = []
    section = subsection = ""
    for raw in logical_lines:
        line = raw.strip(" \t")
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            match = _SECTION.match(line)
            if not match:
                raise ValueError("Invalid Git config section")
            section, subsection = match.group(1).lower(), match.group(2) or ""
            subsection = re.sub(r"\\(.)", r"\1", subsection)
            if match.group(2) is None and "." in section:
                section, subsection = section.split(".", 1)
            line = line[match.end():].strip(" \t")
            if not line or line.startswith(("#", ";")):
                continue
        match = _KEY.match(line)
        if not section or not match:
            raise ValueError("Invalid Git config setting")
        value = "true" if match.group(2) is None else _config_value(match.group(2))
        entries.append((section, subsection, match.group(1).lower(), value))
    return entries


def read_origin_url(root: Path) -> str:
    """Return the literal local origin URL, or empty when it is unavailable.

    Linked worktrees and submodule gitdir files are supported using bounded
    reads of gitdir/commondir pointers. Includes, helpers and URL rewrites are
    deliberately ignored. This is only a suggestion: callers must validate
    that the URL is an allowed GitHub repository before displaying/using it.
    """
    try:
        root = Path(root).expanduser().resolve()
        metadata = root / ".git"
        if metadata.is_symlink():
            return ""
        if not metadata.is_dir():
            pointer = _read_regular(metadata, _MAX_POINTER_BYTES)
            if pointer is None:
                return ""
            line = pointer.decode("utf-8-sig").rstrip("\r\n")
            if not line.startswith("gitdir: "):
                return ""
            target = _local_metadata_path(root, line[len("gitdir: "):])
            if target is None or not target.is_dir():
                return ""
            metadata = target
        common = metadata
        pointer = _read_regular(metadata / "commondir", _MAX_POINTER_BYTES)
        if pointer is not None:
            target = _local_metadata_path(metadata, pointer.decode("utf-8-sig").rstrip("\r\n"))
            if target is None or not target.is_dir():
                return ""
            common = target
        config = _read_regular(common / "config", _MAX_CONFIG_BYTES)
        if config is None:
            return ""
        entries = _config_entries(config)
        worktree_config = False
        for section, _subsection, key, value in entries:
            if section == "extensions" and key == "worktreeconfig":
                worktree_config = value.lower() in {"true", "yes", "on", "1"}
        if worktree_config:
            extra = _read_regular(metadata / "config.worktree", _MAX_CONFIG_BYTES)
            if extra is not None:
                entries.extend(_config_entries(extra))
        urls: list[str] = []
        for section, subsection, key, value in entries:
            if (section, subsection, key) == ("remote", "origin", "url"):
                if not value:
                    urls.clear()
                else:
                    urls.append(value)
        return urls[0] if urls and not any(ord(character) < 32 for character in urls[0]) else ""
    except (OSError, ValueError, UnicodeError):
        return ""
