from __future__ import annotations

import html
import json
from pathlib import Path

import streamlit as st


ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / ".studio" / "published.json"


def load_settings() -> dict:
    try:
        payload = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def cfg(settings: dict, element_id: str, key: str, default):
    value = settings.get(element_id, {})
    return value.get(key, default) if isinstance(value, dict) else default


def style(settings: dict, element_id: str) -> str:
    values = settings.get(element_id, {})
    if not isinstance(values, dict):
        values = {}
    parts = []
    for key, css, suffix in (
        ("font_size", "font-size", "px"),
        ("color", "color", ""),
        ("background_color", "background-color", ""),
        ("border_radius", "border-radius", "px"),
        ("padding", "padding", "px"),
    ):
        value = values.get(key)
        if value not in (None, ""):
            parts.append(f"{css}:{value}{suffix}")
    if values.get("visible") is False:
        parts.append("display:none")
    return ";".join(parts)


st.set_page_config(page_title="Project Studio Demo", layout="wide")
settings = load_settings()

title = html.escape(str(cfg(settings, "hero.title", "text", "Visual Editor Demo")))
subtitle = html.escape(
    str(
        cfg(
            settings,
            "hero.subtitle",
            "text",
            "Click me in Edit Mode and change my text or style.",
        )
    )
)

st.markdown(
    f"""
    <style>
      .stApp {{background:linear-gradient(145deg,#080d18,#111b32);color:white}}
      .demo-wrap {{max-width:980px;margin:3rem auto}}
      .demo-card {{border:1px solid rgba(133,185,255,.3);box-shadow:0 28px 70px rgba(0,0,0,.38)}}
    </style>
    <main class="demo-wrap">
      <h1 data-editor-id="hero.title" data-editor-type="text" style="{style(settings, 'hero.title')}">{title}</h1>
      <p data-editor-id="hero.subtitle" data-editor-type="text" style="{style(settings, 'hero.subtitle')}">{subtitle}</p>
      <section class="demo-card" data-editor-id="demo.card" data-editor-type="container" style="{style(settings, 'demo.card')}">
        <h3>Editable container</h3>
        <p>This card demonstrates background colour, radius, padding and visibility controls.</p>
      </section>
    </main>
    """,
    unsafe_allow_html=True,
)
