from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot


class EditorBridge(QObject):
    """Receive selection and plain-text editing messages from the preview."""

    element_selected = Signal(dict)
    browser_message = Signal(str)
    inline_edit = Signal(dict)
    inline_flushed = Signal(dict)

    @Slot(str)
    def selectElement(self, payload: str) -> None:  # noqa: N802 - exposed to JS
        try:
            data: Any = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            self.browser_message.emit(f"Invalid selection payload: {payload}")
            return
        if isinstance(data, dict):
            self.element_selected.emit(data)

    @Slot(str)
    def inlineEdit(self, payload: str) -> None:  # noqa: N802 - exposed to JS
        try:
            data: Any = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            self.browser_message.emit("Invalid inline editing message.\n")
            return
        if (
            not isinstance(data, dict)
            or data.get("phase") not in {"start", "update", "finish", "cancel"}
            or not isinstance(data.get("id"), str)
            or not data["id"]
            or not isinstance(data.get("session"), str)
            or not data["session"]
            or not isinstance(data.get("text"), str)
            or not isinstance(data.get("document_token"), str)
            or not data["document_token"]
        ):
            self.browser_message.emit("Invalid inline editing message.\n")
            return
        self.inline_edit.emit(data)

    @Slot(str)
    def inlineFlushed(self, payload: str) -> None:  # noqa: N802 - exposed to JS
        try:
            data: Any = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            self.browser_message.emit("Invalid inline save acknowledgement.\n")
            return
        if (
            isinstance(data, dict)
            and isinstance(data.get("document_token"), str)
            and isinstance(data.get("request"), str)
            and type(data.get("success")) is bool
        ):
            self.inline_flushed.emit(data)

    @Slot(str)
    def log(self, message: str) -> None:
        self.browser_message.emit(str(message))
