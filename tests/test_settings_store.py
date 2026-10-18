import json
import os
from pathlib import Path

import pytest

from studio.settings_store import SettingsConflictError, SettingsStore


def test_settings_store_round_trip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    result = store.set_element_patch("hero.title", {"text": "Hello", "font_size": 42})
    assert result["hero.title"]["text"] == "Hello"
    assert store.load_draft()["hero.title"]["font_size"] == 42

    store.publish()
    assert store.load_published()["hero.title"]["text"] == "Hello"

    store.undo()
    assert store.load_draft() == {}
    store.redo()
    assert store.load_draft()["hero.title"]["text"] == "Hello"


def test_visual_edits_are_not_saved_until_save_is_pressed(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.set_element_patch("hero.title", {"text": "Hello", "font_size": 42})

    assert store.is_dirty
    assert SettingsStore(tmp_path).load_draft() == {}
    assert store.load_published() == {}

    saved = store.save_draft()
    assert saved == {"hero.title": {"text": "Hello", "font_size": 42}}
    assert not store.is_dirty
    assert SettingsStore(tmp_path).load_draft() == saved
    assert store.load_published() == {}


def test_undo_after_save_is_unsaved_until_redo_returns_to_saved_state(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.set_element_patch("hero.title", {"text": "First"})
    store.save_draft()

    assert store.undo() == {}
    assert store.is_dirty
    assert SettingsStore(tmp_path).load_draft() == {"hero.title": {"text": "First"}}
    assert store.redo() == {"hero.title": {"text": "First"}}
    assert not store.is_dirty

    store.set_element_patch("hero.title", {"text": "Second"})
    assert store.is_dirty
    assert store.undo() == {"hero.title": {"text": "First"}}
    assert not store.is_dirty


def test_discard_reloads_saved_changes_and_forgets_abandoned_history(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.set_element_patch("hero.title", {"text": "Saved"})
    store.save_draft()
    store.set_element_patch("hero.title", {"text": "Discard me"})
    store.set_element_patch("hero.title", {"font_size": 52})
    store.undo()

    assert store.discard_changes() == {"hero.title": {"text": "Saved"}}
    assert not store.is_dirty
    assert store.undo() is None
    assert store.redo() is None
    assert SettingsStore(tmp_path).load_draft() == store.load_draft()


def test_no_op_edits_do_not_create_history_or_erase_redo(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    assert store.set_element_patch("unused", {}) == {}
    assert store.undo() is None
    store.set_element_patch("hero.title", {"text": "Same"})
    store.save_draft()
    store.set_element_patch("hero.title", {"text": "Same"})
    store.replace_draft(store.load_draft())
    assert not store.is_dirty

    assert store.undo() == {}
    store.replace_draft({})
    store.set_element_patch("unused", {})
    assert store.redo() == {"hero.title": {"text": "Same"}}
    assert not store.is_dirty


def test_mutating_input_and_return_values_cannot_bypass_dirty_tracking(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    payload = {"hero.title": {"text": "Saved", "nested": ["one"]}}
    store.replace_draft(payload)
    payload["hero.title"]["nested"].append("input mutation")
    saved = store.save_draft()
    saved["hero.title"]["nested"].append("return mutation")
    loaded = store.load_draft()
    loaded["hero.title"]["text"] = "Changed outside the editor"

    assert not store.is_dirty
    assert store.load_draft() == {"hero.title": {"text": "Saved", "nested": ["one"]}}
    patch = {"nested": ["two"]}
    result = store.set_element_patch("hero.title", patch)
    patch["nested"].append("input mutation")
    result["hero.title"]["nested"].append("return mutation")
    assert store.load_draft()["hero.title"]["nested"] == ["two"]
    undone = store.undo()
    undone["hero.title"]["nested"].append("history mutation")
    assert not store.is_dirty
    assert store.load_draft()["hero.title"]["nested"] == ["one"]


def test_failed_save_keeps_saved_file_and_working_edits_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(tmp_path)
    store.set_element_patch("hero.title", {"text": "Before"})
    store.save_draft()
    previous_bytes = store.draft_path.read_bytes()
    store.set_element_patch("hero.title", {"text": "Keep this edit"})
    original_replace = os.replace

    def fail_replace(source, destination):
        raise PermissionError("Settings folder is read-only")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="read-only"):
        store.save_draft()
    assert store.draft_path.read_bytes() == previous_bytes
    assert store.load_draft() == {"hero.title": {"text": "Keep this edit"}}
    assert store.is_dirty
    assert not list(store.root.glob("*.tmp"))

    monkeypatch.setattr(os, "replace", original_replace)
    store.save_draft()
    assert not store.is_dirty
    assert SettingsStore(tmp_path).load_draft()["hero.title"]["text"] == "Keep this edit"
    assert store.undo() == {"hero.title": {"text": "Before"}}
    assert store.is_dirty


def test_invalid_setting_cannot_partially_write_a_draft(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    before = store.draft_path.read_bytes()
    store.set_element_patch("hero.title", {"font_size": float("nan")})
    with pytest.raises(ValueError):
        store.save_draft()
    assert store.is_dirty
    assert store.draft_path.read_bytes() == before
    assert not list(store.root.glob("*.tmp"))


def test_external_save_is_not_overwritten_and_discard_reloads_it(tmp_path: Path) -> None:
    first = SettingsStore(tmp_path)
    second = SettingsStore(tmp_path)
    first.set_element_patch("hero.title", {"text": "First window"})
    second.set_element_patch("hero.title", {"text": "Second window"})
    second.save_draft()

    with pytest.raises(SettingsConflictError, match="changed outside Studio"):
        first.save_draft()
    assert first.is_dirty
    assert first.load_draft()["hero.title"]["text"] == "First window"
    assert SettingsStore(tmp_path).load_draft()["hero.title"]["text"] == "Second window"
    assert first.discard_changes()["hero.title"]["text"] == "Second window"
    assert not first.is_dirty
    first.set_element_patch("hero.title", {"text": "Edit after reloading"})
    first.save_draft()
    assert SettingsStore(tmp_path).load_draft()["hero.title"]["text"] == "Edit after reloading"


def test_external_change_during_save_does_not_get_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(tmp_path)
    store.set_element_patch("hero.title", {"text": "Working"})
    external = {"hero.title": {"text": "Saved by another editor"}}
    original_fsync = os.fsync

    def update_external_file(file_descriptor):
        store.draft_path.write_text(json.dumps(external), encoding="utf-8")
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", update_external_file)
    with pytest.raises(SettingsConflictError):
        store.save_draft()
    assert json.loads(store.draft_path.read_text(encoding="utf-8")) == external
    assert store.load_draft()["hero.title"]["text"] == "Working"
    assert store.is_dirty
    assert not list(store.root.glob("*.tmp"))


def test_discard_read_error_preserves_working_edits_and_history(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.set_element_patch("hero.title", {"text": "Keep until a successful discard"})
    store.draft_path.write_text("{broken JSON", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        store.discard_changes()
    assert store.is_dirty
    assert store.load_draft()["hero.title"]["text"] == "Keep until a successful discard"
    assert store.undo() == {}


def test_publish_saves_working_draft_before_publishing(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.set_element_patch("hero.title", {"text": "Saved draft"})
    store.save_draft()
    store.set_element_patch("hero.title", {"text": "Latest working edit"})

    store.publish()
    expected = {"hero.title": {"text": "Latest working edit"}}
    assert not store.is_dirty
    assert SettingsStore(tmp_path).load_draft() == expected
    assert store.load_published() == expected
    assert store.undo() == {"hero.title": {"text": "Saved draft"}}
    assert store.is_dirty


def test_publish_failure_does_not_damage_previous_published_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(tmp_path)
    store.set_element_patch("hero.title", {"text": "Published"})
    store.publish()
    store.set_element_patch("hero.title", {"text": "New draft"})
    original_replace = os.replace

    def fail_publishing(source, destination):
        if Path(destination) == store.published_path:
            raise OSError("Cannot replace published settings")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_publishing)
    with pytest.raises(OSError, match="Cannot replace published settings"):
        store.publish()
    assert store.load_published() == {"hero.title": {"text": "Published"}}
    assert SettingsStore(tmp_path).load_draft() == {"hero.title": {"text": "New draft"}}
    assert not store.is_dirty
    assert not list(store.root.glob("*.tmp"))


def test_reset_to_published_or_defaults_stays_unsaved_until_save(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.defaults_path.write_text('{"hero.title": {"font_size": 24}}', encoding="utf-8")
    store.set_element_patch("hero.title", {"text": "Published"})
    store.publish()
    store.set_element_patch("hero.title", {"text": "Saved draft"})
    store.save_draft()

    assert store.reset_draft_to_published() == {"hero.title": {"text": "Published"}}
    assert store.is_dirty
    assert SettingsStore(tmp_path).load_draft() == {"hero.title": {"text": "Saved draft"}}
    assert store.reset_draft_to_defaults() == {"hero.title": {"font_size": 24}}
    assert store.is_dirty
    store.save_draft()
    assert SettingsStore(tmp_path).load_draft() == {"hero.title": {"font_size": 24}}
    assert store.load_published() == {"hero.title": {"text": "Published"}}
