"""History and durability of a direct text editing session."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from studio.settings_store import SettingsConflictError, SettingsStore


def reopened_store(path: Path, saved: dict) -> SettingsStore:
    store = SettingsStore(path)
    store.replace_draft(saved)
    store.save_draft()
    return SettingsStore(path)


def test_typing_many_characters_is_one_undo_step_and_remains_unsaved(tmp_path):
    saved = {"hero.title": {"text": "Saved title", "font_size": 32}}
    store = reopened_store(tmp_path, saved)
    store.begin_text_edit("hero.title", original_text="Saved title")
    for text in ["N", "Ne", "New", "New title", "New title!"]:
        store.set_element_patch("hero.title", {"text": text})
    assert store.is_dirty
    assert SettingsStore(tmp_path).load_draft() == saved
    changed = {"hero.title": {"text": "New title!", "font_size": 32}}
    assert store.finish_text_edit() == changed
    assert store.undo() == saved
    assert not store.is_dirty
    assert store.undo() is None
    assert store.redo() == changed
    assert store.redo() is None


def test_escape_restores_exact_previous_dirty_draft_and_undo_redo_chain(tmp_path):
    saved = {"hero.title": {"text": "Saved title"}}
    store = reopened_store(tmp_path, saved)
    before = store.set_element_patch("hero.title", {"font_size": 48})
    next_state = store.set_element_patch("hero.title", {"color": "#123456"})
    assert store.undo() == before
    assert store.is_dirty

    store.begin_text_edit("hero.title", original_text="Saved title")
    store.set_element_patch("hero.title", {"text": "Discard this edit"})
    # The browser reports the restored string before its canceled-session event.
    store.set_element_patch("hero.title", {"text": "Saved title"})
    assert store.finish_text_edit(cancel=True) == before
    assert store.is_dirty
    assert store.redo() == next_state
    assert store.undo() == before
    assert store.undo() == saved
    assert not store.is_dirty
    assert store.undo() is None


def test_escape_removes_new_text_override_and_restores_clean_redo_state(tmp_path):
    store = SettingsStore(tmp_path)
    undone = store.set_element_patch("hero.title", {"font_size": 36})
    assert store.undo() == {}
    store.begin_text_edit("hero.title", original_text="DOM title")
    store.set_element_patch("hero.title", {"text": "Something else"})
    assert store.is_dirty
    assert store.finish_text_edit(cancel=True) == {}
    assert not store.is_dirty
    assert store.redo() == undone
    assert SettingsStore(tmp_path).load_draft() == {}


def test_returning_to_full_original_text_creates_no_override_or_history(tmp_path):
    store = SettingsStore(tmp_path)
    redo_state = store.set_element_patch("hero.title", {"color": "#abcdef"})
    store.undo()
    original = "  " + "Long original text " * 300 + "\n"
    store.begin_text_edit("hero.title", original_text=original)
    store.set_element_patch("hero.title", {"text": "Intermediate text"})
    store.set_element_patch("hero.title", {"text": original})
    assert store.finish_text_edit() == {}
    assert not store.is_dirty
    assert store.undo() is None
    assert store.redo() == redo_state


@pytest.mark.parametrize("cancel", [False, True])
def test_restoring_existing_saved_text_override_is_clean_and_has_no_extra_undo(tmp_path, cancel):
    saved = {"hero.title": {"text": "Saved override", "color": "#123456"}}
    store = reopened_store(tmp_path, saved)
    store.begin_text_edit("hero.title", original_text="Saved override")
    store.set_element_patch("hero.title", {"text": "Transient text"})
    if not cancel:
        store.set_element_patch("hero.title", {"text": "Saved override"})
    assert store.finish_text_edit(cancel=cancel) == saved
    assert not store.is_dirty
    assert store.undo() is None
    assert store.redo() is None


def test_unrelated_property_finishes_typing_and_keeps_separate_undo_step(tmp_path):
    store = SettingsStore(tmp_path)
    store.begin_text_edit("hero.title", original_text="DOM title")
    text_state = store.set_element_patch("hero.title", {"text": "New title"})
    changed = store.set_element_patch("hero.title", {"font_size": 48})
    assert changed == {"hero.title": {"text": "New title", "font_size": 48}}
    assert store.finish_text_edit(cancel=True) is None
    assert store.load_draft() == changed
    assert store.undo() == text_state
    assert store.undo() == {}
    assert store.undo() is None
    assert store.redo() == text_state
    assert store.redo() == changed


def test_unrelated_property_after_no_op_typing_does_not_reintroduce_text_override(tmp_path):
    store = SettingsStore(tmp_path)
    store.begin_text_edit("hero.title", original_text="DOM title")
    store.set_element_patch("hero.title", {"text": "Partial change"})
    store.set_element_patch("hero.title", {"text": "DOM title"})
    assert store.set_element_patch("hero.title", {"color": "#123456"}) == {
        "hero.title": {"color": "#123456"},
    }
    assert store.undo() == {}
    assert store.undo() is None


def test_next_element_starts_after_previous_session_and_each_can_be_undone(tmp_path):
    store = SettingsStore(tmp_path)
    store.begin_text_edit("first", original_text="First original")
    first = store.set_element_patch("first", {"text": "First edit"})
    store.begin_text_edit("second", original_text="Second original")
    store.set_element_patch("second", {"text": "Second edit"})
    assert store.finish_text_edit(cancel=True) == first
    assert store.undo() == {}
    assert store.redo() == first


def test_save_finishes_the_session_and_reopens_complete_text(tmp_path):
    store = SettingsStore(tmp_path)
    store.begin_text_edit("hero.title", original_text="Original")
    store.set_element_patch("hero.title", {"text": "Last character saved!"})
    saved = store.save_draft()
    assert saved == {"hero.title": {"text": "Last character saved!"}}
    assert SettingsStore(tmp_path).load_draft() == saved
    assert not store.is_dirty
    assert store.finish_text_edit(cancel=True) is None
    assert store.undo() == {}
    assert store.is_dirty
    assert store.redo() == saved
    assert not store.is_dirty


def test_failed_save_preserves_inline_draft_and_one_undo_step_for_retry(tmp_path, monkeypatch):
    saved = {"hero.title": {"text": "Previously saved"}}
    store = reopened_store(tmp_path, saved)
    original_bytes = store.draft_path.read_bytes()
    store.begin_text_edit("hero.title", original_text="Previously saved")
    for text in ["W", "Wo", "Working text"]:
        store.set_element_patch("hero.title", {"text": text})
    original_replace = os.replace

    def deny_replace(*args):
        raise PermissionError("The folder is read-only")

    monkeypatch.setattr(os, "replace", deny_replace)
    with pytest.raises(PermissionError, match="read-only"):
        store.save_draft()
    assert store.is_dirty
    assert store.load_draft() == {"hero.title": {"text": "Working text"}}
    assert store.draft_path.read_bytes() == original_bytes
    assert store.undo() == saved
    assert store.undo() is None
    store.redo()
    monkeypatch.setattr(os, "replace", original_replace)
    store.save_draft()
    assert SettingsStore(tmp_path).load_draft()["hero.title"]["text"] == "Working text"


def test_external_save_conflict_preserves_inline_draft_and_existing_file(tmp_path):
    store = SettingsStore(tmp_path)
    store.begin_text_edit("hero.title", original_text="Original")
    store.set_element_patch("hero.title", {"text": "My pending text"})
    external = {"hero.title": {"text": "Saved in another window"}}
    store.draft_path.write_text(json.dumps(external), encoding="utf-8")
    with pytest.raises(SettingsConflictError):
        store.save_draft()
    assert store.is_dirty
    assert store.load_draft() == {"hero.title": {"text": "My pending text"}}
    assert json.loads(store.draft_path.read_text(encoding="utf-8")) == external
    assert store.undo() == {}
    assert store.redo() == {"hero.title": {"text": "My pending text"}}
