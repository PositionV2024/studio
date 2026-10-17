"""Small runtime helper that managed Streamlit projects can copy or import.

The desktop editor writes visual settings to .studio/published.json. Components
that opt in can use this helper to read those settings when rendering the public
website.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
PUBLISHED_SETTINGS = PROJECT_ROOT / ".studio" / "published.json"


def load_published_settings() -> dict[str, Any]:
    try:
        payload = json.loads(PUBLISHED_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def element_settings(element_id: str) -> dict[str, Any]:
    settings = load_published_settings().get(element_id, {})
    return settings if isinstance(settings, dict) else {}


def editor_attributes(element_id: str, element_type: str) -> str:
    return (
        f'data-editor-id="{html.escape(element_id, quote=True)}" '
        f'data-editor-type="{html.escape(element_type, quote=True)}"'
    )


def inline_style(element_id: str) -> str:
    settings = element_settings(element_id)
    styles: list[str] = []
    mapping = {
        "font_size": ("font-size", "px"),
        "color": ("color", ""),
        "background_color": ("background-color", ""),
        "border_radius": ("border-radius", "px"),
        "padding": ("padding", "px"),
    }
    for key, (css_name, suffix) in mapping.items():
        value = settings.get(key)
        if value is not None and value != "":
            styles.append(f"{css_name}:{value}{suffix}")
    if settings.get("visible") is False:
        styles.append("display:none")
    return ";".join(styles)
