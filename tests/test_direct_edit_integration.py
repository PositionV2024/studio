"""Direct preview typing through production MainWindow and SettingsStore logic.

The shared visual fixture supplies real Qt Properties controls and project
stores. Only the browser flush and process endpoints are controlled here; the
development smoke harness exercises the corresponding real WebEngine input.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from studio.settings_store import SettingsStore
from test_visual_edit_integration import (
    assert_no_shutdown,
    disk_draft,
    select_project,
    select_text,
    text_selection,
    visual_edit_app,
    visual_window,
)


@pytest.fixture
def direct_window(visual_window):
    window = visual_window
    window._inline_visual_owner = None
    window._inline_pending = False
    window.preview.has_pending_inline_edits = False
    window.preview.inline_session_id = None
    window.preview.queued_inline_text = None
    window.preview.commit_inline_edit = Mock(side_effect=lambda: flush_inline(window))
    return window


def begin_inline(window, element_id="hero.title", text="Original title"):
    window._element_selected(text_selection(element_id, text))
    window.preview.inline_session_id = element_id
    window.preview.inline_original_text = text
    window._inline_edit_started(element_id)


def inline_text(window, text, element_id="hero.title"):
    window.preview.has_pending_inline_edits = True
    window._inline_pending = True
    window._inline_text_changed(element_id, {"text": text})


def finish_inline(window, *, cancel=False, element_id=None):
    current_id = element_id or window.preview.inline_session_id
    if current_id is None:
        return
    if cancel:
        # PreviewView delivers the browser's restored text before the canceled
        # session notification. MainWindow then restores exact patch absence.
        window._inline_text_changed(current_id, {"text": window.preview.inline_original_text})
    window.preview.has_pending_inline_edits = False
    window._inline_pending = False
    window._inline_edit_finished(current_id, cancel)
    if current_id == window.preview.inline_session_id:
        window.preview.inline_session_id = None


def flush_inline(window):
    element_id = window.preview.inline_session_id
    if element_id is not None:
        latest = window.preview.queued_inline_text
        if latest is not None:
            window.preview.queued_inline_text = None
            inline_text(window, latest, element_id)
        finish_inline(window, element_id=element_id)
    return True


def test_selection_does_not_flush_the_new_browser_inline_editor(direct_window):
    window = direct_window
    window.preview.commit_inline_edit.reset_mock()
    select_text(window)
    window.preview.commit_inline_edit.assert_not_called()
    assert window.inspector.element_id == "hero.title"


def test_live_inline_typing_updates_properties_and_dirty_state_without_patch_echo(direct_window):
    window = direct_window
    begin_inline(window)
    inline_text(window, "Direct preview draft")
    assert window.current_settings.load_draft()["hero.title"]["text"] == "Direct preview draft"
    assert window.inspector.text.text() == "Direct preview draft"
    assert not window.inspector.has_pending_edits
    assert window.inspector.save_button.isEnabled()
    assert window.inspector.save_status.text() == "Unsaved changes"
    assert window.preview.patches == [], "Do not echo browser keystrokes back through DOM patching"
    assert disk_draft(window) == {}


def test_save_flushes_last_browser_keystroke_and_reopens_from_disk(direct_window):
    window = direct_window
    begin_inline(window)
    inline_text(window, "Already received")
    window.preview.queued_inline_text = "Final key before clicking Save"
    assert window.save_visual_changes()
    window.preview.commit_inline_edit.assert_called()
    expected = {"hero.title": {"text": "Final key before clicking Save"}}
    assert disk_draft(window) == expected
    assert SettingsStore(window.current_project.path).load_draft() == expected
    assert not window.current_settings.is_dirty
    assert not window.inspector.has_pending_edits
    assert window.inspector.save_status.text() == "Saved"
    assert window._inline_visual_owner is None


def test_save_keeps_the_full_long_browser_text_instead_of_a_truncated_properties_mirror(direct_window):
    window = direct_window
    begin_inline(window)
    text = "Long browser content. " * 2200 + "Final saved characters."
    assert len(text) > 32_767
    inline_text(window, text)
    assert not window.inspector.has_pending_edits
    assert window.save_visual_changes()
    assert disk_draft(window)["hero.title"]["text"] == text
    assert window.inspector.text.text() == text


@pytest.mark.parametrize("cancelled", [False, True], ids=["enter", "escape"])
def test_finishing_a_stale_browser_selection_reflects_the_prior_properties_text(direct_window, cancelled):
    window = direct_window
    select_text(window)
    field = window.inspector.text
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, "Pending Properties heading")
    assert window.inspector.has_pending_edits
    # The DOM click was described before Qt committed the previous focused
    # Properties field, so its queued selection still contains the old text.
    begin_inline(window, text="Original title")
    expected = {"hero.title": {"text": "Pending Properties heading"}}
    assert window.current_settings.load_draft() == expected
    # No browser input occurred: PreviewView sends only the finish notification.
    window._inline_edit_finished("hero.title", cancelled)
    assert window.current_settings.load_draft() == expected
    assert window.preview.drafts[-1] == expected
    assert window.inspector.text.text() == "Pending Properties heading"
    assert not window.inspector.has_pending_edits


def test_inline_typing_is_one_visual_undo_step_and_redo_restores_final_text(direct_window):
    window = direct_window
    begin_inline(window)
    for value in ("A", "A complete", "A complete heading"):
        inline_text(window, value)
    finish_inline(window)
    window.preview.setFocus()
    window.undo()
    assert window.current_settings.load_draft() == {}
    assert not window.current_settings.is_dirty
    assert window.preview.drafts[-1] == {}
    window.redo()
    expected = {"hero.title": {"text": "A complete heading"}}
    assert window.current_settings.load_draft() == expected
    assert window.preview.drafts[-1] == expected
    assert window.current_settings.is_dirty
    assert disk_draft(window) == {}


@pytest.mark.parametrize("saved_text", [None, "Previously saved text"])
def test_escape_restores_exact_saved_draft_and_cleans_dirty_status(direct_window, saved_text):
    window = direct_window
    if saved_text is not None:
        window.current_settings.set_element_patch("hero.title", {"text": saved_text, "font_size": 40})
        window.current_settings.save_draft()
    before = window.current_settings.load_draft()
    begin_inline(window, text=saved_text or "Original title")
    inline_text(window, "Canceled direct text")
    assert window.current_settings.is_dirty
    finish_inline(window, cancel=True)
    assert window.current_settings.load_draft() == before
    assert disk_draft(window) == before
    assert not window.current_settings.is_dirty
    assert window.preview.drafts[-1] == before
    assert window.inspector.text.text() == (saved_text or "Original title")
    assert not window.inspector.save_button.isEnabled()
    assert window._inline_visual_owner is None


def test_escape_preserves_preexisting_unsaved_properties(direct_window):
    window = direct_window
    select_text(window)
    window._property_changed("hero.title", {"font_size": 44, "color": "#123456"})
    before = window.current_settings.load_draft()
    begin_inline(window)
    inline_text(window, "Discard only this text session")
    finish_inline(window, cancel=True)
    assert window.current_settings.load_draft() == before
    assert window.current_settings.is_dirty
    assert disk_draft(window) == {}
    assert window.preview.drafts[-1] == before


def test_escape_keeps_the_existing_visual_redo_chain(direct_window):
    window = direct_window
    select_text(window)
    window._property_changed("hero.title", {"font_size": 44})
    window.preview.setFocus()
    window.undo()
    assert window.current_settings.load_draft() == {}
    begin_inline(window)
    inline_text(window, "Canceled after undo")
    finish_inline(window, cancel=True)
    window.preview.setFocus()
    window.redo()
    assert window.current_settings.load_draft() == {"hero.title": {"font_size": 44}}


def test_typing_back_to_original_text_creates_no_override_or_undo_step(direct_window):
    window = direct_window
    begin_inline(window)
    inline_text(window, "Temporary replacement")
    inline_text(window, "Original title")
    finish_inline(window)
    assert window.current_settings.load_draft() == {}
    assert not window.current_settings.is_dirty
    assert window.current_settings.undo() is None
    assert not window.inspector.save_button.isEnabled()
    assert disk_draft(window) == {}


def test_live_inline_sync_preserves_unfinished_colour_until_save(direct_window):
    window = direct_window
    begin_inline(window)
    field = window.inspector.background_color.edit
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, "#2468ac")
    assert field.hasFocus()
    assert window.inspector.has_pending_edits
    inline_text(window, "Preview text with pending colour")
    assert field.hasFocus()
    assert field.text() == "#2468ac"
    assert window.inspector.has_pending_edits
    assert window.current_settings.load_draft() == {
        "hero.title": {"text": "Preview text with pending colour"}
    }
    assert window.save_visual_changes()
    assert disk_draft(window) == {
        "hero.title": {"text": "Preview text with pending colour", "background_color": "#2468ac"}
    }


def test_project_switch_flushes_to_original_owner_and_rejects_late_events(direct_window):
    window = direct_window
    begin_inline(window)
    inline_text(window, "Alpha in progress")
    window.preview.queued_inline_text = "Alpha final before project switch"
    alpha = window.current_settings
    select_project(window, "Bravo")
    assert window.current_project.name == "Bravo"
    assert alpha.load_draft() == {"hero.title": {"text": "Alpha final before project switch"}}
    assert alpha.is_dirty
    assert window.current_settings.load_draft() == {}
    window._inline_text_changed("hero.title", {"text": "Late Alpha callback"})
    window._inline_edit_finished("hero.title", False)
    assert window.current_settings.load_draft() == {}
    assert alpha.load_draft()["hero.title"]["text"] == "Alpha final before project switch"
    assert disk_draft(window, "Alpha") == {}
    assert disk_draft(window, "Bravo") == {}


def test_late_old_element_patch_and_finish_do_not_end_new_element_session(direct_window):
    window = direct_window
    begin_inline(window)
    inline_text(window, "First element")
    finish_inline(window)
    begin_inline(window, element_id="hero.subtitle", text="Original subtitle")
    window._inline_text_changed("hero.title", {"text": "Late old element"})
    window._inline_edit_finished("hero.title", False)
    inline_text(window, "Second element", "hero.subtitle")
    finish_inline(window)
    assert window.current_settings.load_draft() == {
        "hero.title": {"text": "First element"},
        "hero.subtitle": {"text": "Second element"},
    }


def test_inline_events_without_supported_selected_session_are_ignored(direct_window):
    window = direct_window
    window._inline_text_changed("hero.title", {"text": "No session"})
    window._inline_edit_started("not.selected")
    window._inline_text_changed("not.selected", {"text": "No selected element"})
    assert window.current_settings.load_draft() == {}
    window._element_selected({
        "id": "hero.image", "type": "image", "tag": "img", "label": "Image",
        "source": "logo.png", "supported_fields": ["source", "width", "height"],
    })
    window._inline_edit_started("hero.image")
    window._inline_text_changed("hero.image", {"text": "Image cannot be typed into"})
    assert window.current_settings.load_draft() == {}


def test_inline_messages_cannot_change_unrelated_properties(direct_window):
    window = direct_window
    begin_inline(window)
    window._inline_text_changed("hero.title", {"source": "unrelated.png"})
    window._inline_text_changed("hero.title", {"text": "Do not accept mixed payload", "font_size": 99})
    assert window.current_settings.load_draft() == {}


@pytest.mark.parametrize("action", ["save", "publish", "close"])
def test_failed_browser_flush_aborts_save_publish_or_shutdown(direct_window, action):
    window = direct_window
    begin_inline(window)
    inline_text(window, "Keep the editable buffer")
    window.preview.commit_inline_edit.side_effect = None
    window.preview.commit_inline_edit.return_value = False
    if action == "save":
        assert not window.save_visual_changes()
    elif action == "publish":
        window.publish()
        assert window.current_settings.load_published() == {}
    else:
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        assert_no_shutdown(window)
        assert window.dialogs.exit_prompts == []
    assert disk_draft(window) == {}
    assert window.current_settings.is_dirty
    assert window.current_settings.load_draft()["hero.title"]["text"] == "Keep the editable buffer"


def test_exit_save_flushes_browser_input_before_exit_prompt_and_disk_write(direct_window):
    window = direct_window
    begin_inline(window)
    inline_text(window, "Earlier characters")
    window.preview.queued_inline_text = "Last keystroke on exit"
    window.dialogs.exit_choice = QMessageBox.StandardButton.Save
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    assert disk_draft(window) == {"hero.title": {"text": "Last keystroke on exit"}}
    assert not window.current_settings.is_dirty
    window.runner.shutdown.assert_called_once()
