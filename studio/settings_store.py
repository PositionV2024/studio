from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from .project_manifest import studio_directory


DEFAULTS_FILE = "defaults.json"
DRAFT_FILE = "draft.json"
PUBLISHED_FILE = "published.json"


class SettingsStore:
    """Read, write, publish, and undo visual-editor settings for one project."""

    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.root = studio_directory(project_path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.defaults_path = self.root / DEFAULTS_FILE
        self.draft_path = self.root / DRAFT_FILE
        self.published_path = self.root / PUBLISHED_FILE
        self._undo_stack: list[dict[str, Any]] = []
        self._redo_stack: list[dict[str, Any]] = []
        self.ensure_files()

    def ensure_files(self) -> None:
        for path in (self.defaults_path, self.draft_path, self.published_path):
            if not path.exists():
                path.write_text("{}\n", encoding="utf-8")

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load_defaults(self) -> dict[str, Any]:
        return self._read(self.defaults_path)

    def load_draft(self) -> dict[str, Any]:
        return self._read(self.draft_path)

    def load_published(self) -> dict[str, Any]:
        return self._read(self.published_path)

    def set_element_patch(self, element_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        before = self.load_draft()
        self._undo_stack.append(copy.deepcopy(before))
        self._redo_stack.clear()

        after = copy.deepcopy(before)
        current = after.get(element_id, {})
        if not isinstance(current, dict):
            current = {}
        current.update(patch)
        after[element_id] = current
        self._write(self.draft_path, after)
        return after

    def replace_draft(self, payload: dict[str, Any], *, record_history: bool = True) -> None:
        if record_history:
            self._undo_stack.append(copy.deepcopy(self.load_draft()))
            self._redo_stack.clear()
        self._write(self.draft_path, payload)

    def publish(self) -> None:
        shutil.copyfile(self.draft_path, self.published_path)

    def reset_draft_to_published(self) -> dict[str, Any]:
        published = self.load_published()
        self.replace_draft(published)
        return published

    def reset_draft_to_defaults(self) -> dict[str, Any]:
        defaults = self.load_defaults()
        self.replace_draft(defaults)
        return defaults

    def undo(self) -> dict[str, Any] | None:
        if not self._undo_stack:
            return None
        current = self.load_draft()
        previous = self._undo_stack.pop()
        self._redo_stack.append(current)
        self._write(self.draft_path, previous)
        return previous

    def redo(self) -> dict[str, Any] | None:
        if not self._redo_stack:
            return None
        current = self.load_draft()
        next_state = self._redo_stack.pop()
        self._undo_stack.append(current)
        self._write(self.draft_path, next_state)
        return next_state
