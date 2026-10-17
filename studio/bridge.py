from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot


class EditorBridge(QObject):
    """Bridge used by injected browser JavaScript to report selected elements."""

    element_selected = Signal(dict)
    browser_message = Signal(str)

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
    def log(self, message: str) -> None:
        self.browser_message.emit(str(message))
