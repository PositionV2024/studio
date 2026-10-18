from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .project_manifest import studio_directory


DEFAULTS_FILE = "defaults.json"
DRAFT_FILE = "draft.json"
PUBLISHED_FILE = "published.json"


class SettingsConflictError(ValueError):
    """The saved draft changed after this editing session loaded it."""


class SettingsStore:
    """Keep working UI changes separate from explicitly saved draft settings."""

    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.root = studio_directory(project_path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.defaults_path = self.root / DEFAULTS_FILE
        self.draft_path = self.root / DRAFT_FILE
        self.published_path = self.root / PUBLISHED_FILE
        self._undo_stack: list[dict[str, Any]] = []
        self._redo_stack: list[dict[str, Any]] = []
        self._text_edit: tuple[str, dict[str, Any], str | None] | None = None
        self.ensure_files()
        try:
            self._saved_disk_bytes: bytes | None = self.draft_path.read_bytes()
        except OSError:
            self._saved_disk_bytes = None
        try:
            saved = self._decode_draft(self._saved_disk_bytes)
        except ValueError:
            # Preserve the existing tolerant startup behavior. Explicit saves
            # still check the original file before replacing it.
            saved = {}
        self._saved_draft = copy.deepcopy(saved)
        self._draft = copy.deepcopy(saved)

    @property
    def is_dirty(self) -> bool:
        return self._draft != self._saved_draft

    def ensure_files(self) -> None:
        for path in (self.defaults_path, self.draft_path, self.published_path):
            if not path.exists():
                self._write(path, {})

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write(
        path: Path,
        payload: dict[str, Any],
        *,
        expected_bytes: bytes | None = None,
    ) -> bytes:
        """Replace a complete settings file without exposing a partial write."""
        try:
            data = (
                json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            ).encode("utf-8")
        except (TypeError, UnicodeError) as exc:
            raise ValueError("The UI draft contains a value that cannot be saved as JSON.") from exc
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if expected_bytes is not None:
                SettingsStore._check_disk_snapshot(path, expected_bytes)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return data

    @staticmethod
    def _decode_draft(raw: bytes | None) -> dict[str, Any]:
        try:
            payload = json.loads(raw) if raw is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("The saved UI draft is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("The saved UI draft must contain a JSON object.")
        return payload

    @staticmethod
    def _check_disk_snapshot(path: Path, expected_bytes: bytes | None) -> None:
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != expected_bytes:
            raise SettingsConflictError(
                "The saved UI draft changed outside Studio. Your unsaved changes are still here. "
                "To reload the saved version, close Studio, choose Discard, "
                "then reopen this project."
            )

    def load_defaults(self) -> dict[str, Any]:
        return self._read(self.defaults_path)

    def load_draft(self) -> dict[str, Any]:
        return copy.deepcopy(self._draft)

    def save_draft(self) -> dict[str, Any]:
        """Persist the working draft, preserving it and its dirty state on error."""
        self.finish_text_edit()
        self._check_disk_snapshot(self.draft_path, self._saved_disk_bytes)
        if not self.is_dirty:
            return self.load_draft()
        written = self._write(
            self.draft_path, self._draft, expected_bytes=self._saved_disk_bytes
        )
        self._saved_disk_bytes = written
        self._saved_draft = copy.deepcopy(self._draft)
        return self.load_draft()

    def discard_changes(self) -> dict[str, Any]:
        """Reload the saved draft and forget the discarded editing history."""
        raw = self.draft_path.read_bytes()
        saved = self._decode_draft(raw)
        self._saved_disk_bytes = raw
        self._saved_draft = copy.deepcopy(saved)
        self._draft = copy.deepcopy(saved)
        self._text_edit = None
        self._undo_stack.clear()
        self._redo_stack.clear()
        return self.load_draft()

    def load_published(self) -> dict[str, Any]:
        return self._read(self.published_path)

    def begin_text_edit(self, element_id: str, *, original_text: str | None = None) -> None:
        """Group direct typing into one undo step without writing to disk."""
        if self._text_edit is not None and self._text_edit[0] == element_id:
            return
        self.finish_text_edit()
        self._text_edit = (element_id, self.load_draft(), original_text)

    def finish_text_edit(self, *, cancel: bool = False) -> dict[str, Any] | None:
        """Finish a typing session, or restore its exact draft on Escape."""
        transaction = self._text_edit
        if transaction is None:
            return None
        self._text_edit = None
        element_id, before, original_text = transaction
        current = self._draft.get(element_id, {})
        unchanged_text = (
            original_text is not None
            and isinstance(current, dict)
            and current.get("text") == original_text
        )
        if cancel or unchanged_text:
            self._draft = before
        elif before != self._draft:
            self._undo_stack.append(before)
            self._redo_stack.clear()
        return self.load_draft()

    def set_element_patch(self, element_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not patch:
            return self.load_draft()
        inline_text = (
            self._text_edit is not None
            and self._text_edit[0] == element_id
            and set(patch) <= {"text"}
        )
        if not inline_text:
            # Finishing a no-op text session may remove a temporary override.
            # Build the next property patch from that finalized draft.
            self.finish_text_edit()
        after = self.load_draft()
        current = after.get(element_id, {})
        if not isinstance(current, dict):
            current = {}
        current.update(copy.deepcopy(patch))
        after[element_id] = current
        if inline_text:
            # Intermediate characters are immediately visible to dirty-state
            # tracking, while Escape can still retain the existing redo chain.
            self._draft = after
        else:
            self.replace_draft(after)
        return self.load_draft()

    def replace_draft(self, payload: dict[str, Any], *, record_history: bool = True) -> None:
        if not isinstance(payload, dict):
            raise ValueError("UI draft settings must be a dictionary.")
        self.finish_text_edit()
        if payload == self._draft:
            return
        if record_history:
            self._undo_stack.append(self.load_draft())
        self._redo_stack.clear()
        self._draft = copy.deepcopy(payload)

    def publish(self) -> None:
        self.save_draft()
        self._write(self.published_path, self._draft)

    def reset_draft_to_published(self) -> dict[str, Any]:
        published = self.load_published()
        self.replace_draft(published)
        return published

    def reset_draft_to_defaults(self) -> dict[str, Any]:
        defaults = self.load_defaults()
        self.replace_draft(defaults)
        return defaults

    def undo(self) -> dict[str, Any] | None:
        self.finish_text_edit()
        if not self._undo_stack:
            return None
        current = self.load_draft()
        previous = self._undo_stack.pop()
        self._redo_stack.append(current)
        self._draft = previous
        return self.load_draft()

    def redo(self) -> dict[str, Any] | None:
        self.finish_text_edit()
        if not self._redo_stack:
            return None
        current = self.load_draft()
        next_state = self._redo_stack.pop()
        self._undo_stack.append(current)
        self._draft = next_state
        return self.load_draft()
