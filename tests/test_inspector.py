"""Real Qt interactions for the visual editor's property fields and Save footer."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QSpinBox

from studio.inspector import ColorField, PropertyInspector
from studio.styles import APP_STYLE


@pytest.fixture(scope="module")
def inspector_app():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture
def inspector(inspector_app):
    panel = PropertyInspector()
    panel.resize(360, 800)
    panel.show()
    inspector_app.processEvents()
    yield panel
    panel.clear_selection()
    panel.close()
    panel.deleteLater()
    inspector_app.processEvents()


def selection(**overrides):
    return {
        "id": "page.title", "type": "text", "tag": "h1", "label": "Title",
        "text": "Original title", "source": "",
        "computed": {
            "font_size": 24, "width": 360, "height": 48, "padding": 8,
            "margin": 4, "border_radius": 6, "border_width": 1,
            "color": "#ffffff", "background_color": "#112233",
            "border_color": "#334455", "opacity": 0.8, "visible": True,
        },
        **overrides,
    }


def type_value(app, field: QLineEdit, value: str):
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, value)
    app.processEvents()


def test_deselection_clears_stale_values_and_hides_all_property_rows(inspector):
    inspector.set_selection(selection())
    patches = []
    inspector.patch_changed.connect(lambda *args: patches.append(args))
    inspector.clear_selection()

    assert inspector.title.text() == "No element selected"
    assert inspector.heading.text() == "Properties"
    assert inspector.heading.isVisible()
    assert "Only settings you can edit are shown" in inspector.details.text()
    assert not inspector.inline_hint.isVisible()
    assert all(group.isHidden() for group in inspector.groups.values())
    assert not inspector.element_id
    assert not inspector.has_pending_edits
    for key, widget in inspector._fields.items():
        assert not widget.isEnabled(), key
        assert inspector.field_rows[key].isHidden(), key
        assert inspector.field_labels[key].isHidden(), key
        assert inspector.field_markers[key].isHidden(), key
        assert not inspector.field_markers[key].text()
        assert inspector.field_rows[key].cursor().shape() == Qt.CursorShape.ForbiddenCursor
        assert "Select an element" in widget.toolTip()
        assert widget.accessibleName()
        assert widget.accessibleDescription()
        if isinstance(widget, QLineEdit):
            assert widget.isReadOnly()
            assert not widget.text()
        elif isinstance(widget, QSpinBox):
            assert widget.isReadOnly()
            assert widget.text() == "—"
        elif isinstance(widget, ColorField):
            assert not widget.edit.text()
            assert not widget.edit.isEnabled()
            assert widget.edit.isReadOnly()
            assert not widget.button.isEnabled()
            assert widget.button.cursor().shape() == Qt.CursorShape.ForbiddenCursor
    inspector.commit_pending_edits()
    assert patches == []


@pytest.mark.parametrize(
    ("data", "allowed", "blocked"),
    [
        (selection(), {"text", "font_size", "color", "background_color"}, {"source"}),
        (selection(type="image", tag="img", source="image.png"),
         {"source", "width", "height", "background_color"}, {"text", "font_size", "color"}),
        (selection(type="image", tag="picture"), {"width", "height"}, {"source", "text", "font_size", "color"}),
        (selection(type="input", tag="input"), {"width", "font_size", "color"}, {"text", "source"}),
        (selection(type="input", tag="select"), {"width", "font_size", "color"}, {"text", "source"}),
        (selection(type="container", tag="div"), {"width", "font_size", "color"}, {"text", "source"}),
    ],
)
def test_field_permissions_follow_element_type(inspector, data, allowed, blocked):
    inspector.set_selection(data)
    for key in allowed:
        assert inspector._fields[key].isEnabled(), key
        assert not inspector.field_rows[key].isHidden(), key
        assert not inspector.field_labels[key].isHidden(), key
        assert inspector.field_markers[key].isHidden(), key
        assert inspector.field_markers[key].text() == ""
        assert inspector.field_rows[key].cursor().shape() != Qt.CursorShape.ForbiddenCursor
    for key in blocked:
        assert not inspector._fields[key].isEnabled(), key
        assert inspector.field_rows[key].isHidden(), key
        assert inspector.field_labels[key].isHidden(), key
        assert inspector.field_markers[key].isHidden(), key
        assert inspector._fields[key].toolTip()


def test_dom_capabilities_override_coarse_type_and_block_all_edit_paths(inspector, inspector_app, monkeypatch):
    panel = inspector
    panel.set_selection(selection(
        supported_fields=["background_color", "opacity", "made_up_field"],
        field_reasons={"text": "Select the child text label.", "width": "Inline text has no width setting."},
    ))
    patches = []
    panel.patch_changed.connect(lambda *args: patches.append(args))
    monkeypatch.setattr("studio.inspector.QColorDialog.getColor", lambda *args: pytest.fail("Blocked colour picker opened"))

    QTest.keyClicks(panel.text, "cannot type")
    QTest.keyClick(panel.font_size, Qt.Key.Key_Up)
    panel.text.setText("programmatic value")
    panel.text.editingFinished.emit()
    panel.text_color.edit.setText("#ff0000")
    panel.text_color.edit.editingFinished.emit()
    panel.text_color._pick()
    panel.text_color.changed.emit("#ff0000")
    panel._emit("text", "bypassed control")
    panel.commit_pending_edits()

    assert patches == []
    assert not panel.has_pending_edits
    assert panel.text.toolTip() == "Select the child text label."
    assert panel.field_markers["width"].accessibleDescription() == "Inline text has no width setting."
    assert panel.background_color.isEnabled()
    assert panel.background_color.button.isEnabled()
    assert panel.groups["content"].isHidden()
    assert panel.groups["layout"].isHidden()
    assert not panel.groups["appearance"].isHidden()
    assert panel.inline_hint.isHidden()


def test_controls_reappear_for_next_element_without_unavailable_markers(inspector):
    inspector.set_selection(selection(type="image", tag="img"))
    assert inspector.text.isReadOnly()
    inspector.set_selection(selection())
    assert inspector.text.isEnabled()
    assert not inspector.text.isReadOnly()
    assert not inspector.field_rows["text"].isHidden()
    assert not inspector.field_labels["text"].isHidden()
    assert inspector.field_rows["source"].isHidden()
    assert inspector.field_labels["source"].isHidden()
    assert inspector.field_markers["text"].isHidden()
    assert inspector.field_markers["text"].text() == ""
    assert inspector.field_rows["text"].cursor().shape() != Qt.CursorShape.ForbiddenCursor
    assert not inspector.text_color.edit.isReadOnly()
    assert inspector.text_color.edit.isEnabled()
    assert inspector.text_color.button.isEnabled()


@pytest.mark.parametrize("editable_text", [True, False])
def test_inline_hint_and_groups_follow_actual_dom_capabilities(inspector, editable_text):
    fields = ["text", "color"] if editable_text else ["width"]
    inspector.set_selection(selection(supported_fields=fields))
    assert inspector.inline_hint.isVisible() == editable_text
    assert inspector.groups["content"].isVisible() == editable_text
    assert inspector.groups["appearance"].isVisible() == editable_text
    assert inspector.groups["layout"].isVisible() != editable_text
    if editable_text:
        assert inspector.inline_hint.text() == (
            "Click text in the preview to edit it. Enter to finish; Esc to cancel."
        )
    for key in inspector._fields:
        assert inspector.field_rows[key].isHidden() == (key not in fields)
        assert inspector.field_labels[key].isHidden() == (key not in fields)


def test_selected_element_with_no_editable_properties_has_clear_empty_state(inspector):
    inspector.set_selection(selection(supported_fields=[]))
    assert inspector.title.text() == "Title"
    assert "This element has no editable properties." in inspector.details.text()
    assert all(group.isHidden() for group in inspector.groups.values())
    assert inspector.inline_hint.isHidden()
    assert inspector.heading.isVisible()


@pytest.mark.parametrize("field_name", ["text", "source", "background_color"])
def test_typing_enables_save_before_blur_and_click_commits_before_save_signal(
    inspector, inspector_app, field_name,
):
    panel = inspector
    panel.set_selection(selection(type="image", tag="img", source="old.png") if field_name == "source" else selection())
    panel.set_save_state(False, project_name="Demo project")
    events = []
    pending = []

    def record_patch(element_id, patch):
        events.append((element_id, patch))
        panel.set_save_state(True, project_name="Demo project")

    panel.patch_changed.connect(record_patch)
    panel.pending_edits_changed.connect(pending.append)
    panel.save_requested.connect(lambda: events.append("save"))
    field = panel._fields[field_name]
    if isinstance(field, ColorField):
        field = field.edit
    value = "new.png" if field_name == "source" else "#123456" if field_name == "background_color" else "New title"
    type_value(inspector_app, field, value)

    assert events == [], "Typing must not create a patch for each partial keystroke"
    assert field.hasFocus()
    assert panel.has_pending_edits
    assert panel.save_button.isEnabled()
    assert panel.save_status.text() == "Unsaved changes"
    assert pending == [True]
    QTest.mouseClick(panel.save_button, Qt.MouseButton.LeftButton)
    inspector_app.processEvents()
    assert events == [("page.title", {field_name: value}), "save"]
    assert not panel.has_pending_edits
    panel.set_save_state(False, project_name="Demo project")
    assert not panel.save_button.isEnabled()
    assert panel.save_status.text() == "Saved"


def test_pending_number_commits_whole_value_without_partial_history(inspector, inspector_app):
    panel = inspector
    panel.set_selection(selection())
    panel.set_save_state(False, project_name="Demo")
    patches = []
    panel.patch_changed.connect(lambda *args: patches.append(args))
    type_value(inspector_app, panel.font_size.lineEdit(), "36")
    assert panel.font_size.value() == 24
    assert panel.has_pending_edits
    assert panel.save_button.isEnabled()
    assert patches == []
    panel.commit_pending_edits()
    assert panel.font_size.value() == 36
    assert patches == [("page.title", {"font_size": 36})]
    assert not panel.has_pending_edits
    panel.commit_pending_edits()
    assert len(patches) == 1


@pytest.mark.parametrize("change_selection", [True, False])
def test_selection_change_or_clear_commits_last_keystroke_to_old_element(
    inspector, inspector_app, change_selection,
):
    panel = inspector
    panel.set_selection(selection())
    patches = []
    panel.patch_changed.connect(lambda *args: patches.append(args))
    type_value(inspector_app, panel.text, "Last keystroke")
    if change_selection:
        panel.set_selection(selection(id="page.subtitle", text="Second label"))
    else:
        panel.clear_selection()
    assert patches == [("page.title", {"text": "Last keystroke"})]
    assert panel.text.text() == ("Second label" if change_selection else "")
    assert not panel.has_pending_edits


def test_no_op_typing_and_repeated_commit_do_not_create_dirty_patches(inspector, inspector_app):
    panel = inspector
    panel.set_selection(selection())
    panel.set_save_state(False, project_name="Demo")
    patches = []
    panel.patch_changed.connect(lambda *args: patches.append(args))
    type_value(inspector_app, panel.text, "Original title")
    assert not panel.has_pending_edits
    assert not panel.save_button.isEnabled()
    panel.commit_pending_edits()
    panel.text.editingFinished.emit()
    panel.background_color.edit.editingFinished.emit()
    panel.width.setValue(panel.width.value())
    panel.set_selection(selection())
    assert patches == []


def test_save_is_project_level_and_stays_visible_without_element_selection(inspector, inspector_app):
    panel = inspector
    panel.set_save_state(True, project_name="Unsaved project")
    panel.clear_selection()
    panel.resize(330, 350)
    inspector_app.processEvents()
    panel.scroll.verticalScrollBar().setValue(panel.scroll.verticalScrollBar().maximum())
    assert panel.save_button.isVisible()
    assert panel.save_button.isEnabled()
    assert panel.save_status.text() == "Unsaved changes"
    assert "Unsaved project" in panel.save_status.toolTip()
    assert panel.save_footer.parentWidget() is panel
    assert panel.save_footer.geometry().bottom() <= panel.size().height()
    assert panel.heading.isVisible()
    assert panel.heading.parentWidget() is panel
    panel.set_save_state(False, enabled=False)
    assert not panel.save_button.isEnabled()
    assert panel.save_status.text() == "No project selected"


def test_properties_heading_and_save_footer_stay_visible_while_scrolling(inspector, inspector_app):
    panel = inspector
    panel.set_selection(selection())
    panel.set_save_state(True, project_name="Demo")
    panel.resize(330, 350)
    inspector_app.processEvents()
    scroll = panel.scroll.verticalScrollBar()
    assert scroll.maximum() > 0
    heading_rect = panel.heading.geometry()
    footer_rect = panel.save_footer.geometry()
    scroll.setValue(scroll.maximum())
    inspector_app.processEvents()
    assert panel.heading.isVisible()
    assert panel.save_button.isVisible()
    assert panel.heading.geometry() == heading_rect
    assert panel.save_footer.geometry() == footer_rect


@pytest.mark.parametrize("text", ["Typed directly in preview", ""])
def test_inline_text_sync_updates_baseline_without_duplicate_patch(inspector, text):
    panel = inspector
    panel.set_selection(selection())
    panel.set_save_state(True, project_name="Demo")
    patches = []
    panel.patch_changed.connect(lambda *args: patches.append(args))
    assert panel.update_inline_text("page.title", text)
    assert panel.text.text() == text
    assert not panel.has_pending_edits
    assert panel.save_status.text() == "Unsaved changes"
    panel.commit_pending_edits()
    panel.text.editingFinished.emit()
    assert patches == []


@pytest.mark.parametrize("field_name", ["background_color", "font_size"])
def test_inline_text_sync_preserves_unfinished_independent_property_edits(
    inspector, inspector_app, field_name,
):
    panel = inspector
    panel.set_selection(selection())
    panel.set_save_state(False, project_name="Demo")
    patches = []
    pending = []
    panel.patch_changed.connect(lambda *args: patches.append(args))
    panel.pending_edits_changed.connect(pending.append)
    widget = panel._fields[field_name]
    field = widget.edit if isinstance(widget, ColorField) else widget.lineEdit()
    text = "#123456" if field_name == "background_color" else "36"
    expected = text if field_name == "background_color" else 36
    type_value(inspector_app, field, text)
    assert field.hasFocus()
    cursor = field.cursorPosition()
    assert panel.update_inline_text("page.title", "Preview draft")
    assert panel.text.text() == "Preview draft"
    assert field.hasFocus()
    assert field.cursorPosition() == cursor
    assert text in field.text()
    assert panel.has_pending_edits
    assert panel.save_button.isEnabled()
    assert pending == [True]
    assert patches == []
    panel.commit_pending_edits()
    assert patches == [("page.title", {field_name: expected})]
    assert pending == [True, False]


def test_inline_text_sync_preserves_unfinished_text_property_edit(inspector, inspector_app):
    panel = inspector
    panel.set_selection(selection())
    patches = []
    panel.patch_changed.connect(lambda *args: patches.append(args))
    type_value(inspector_app, panel.text, "Properties draft")
    assert not panel.update_inline_text("page.title", "Late browser update")
    assert panel.text.text() == "Properties draft"
    assert panel.text.hasFocus()
    assert panel.has_pending_edits
    assert patches == []
    panel.commit_pending_edits()
    assert patches == [("page.title", {"text": "Properties draft"})]


def test_inline_text_sync_rejects_other_element_and_non_text_selection(inspector):
    panel = inspector
    panel.set_selection(selection())
    assert not panel.update_inline_text("page.other", "Other element")
    assert panel.text.text() == "Original title"
    panel.set_selection(selection(type="image", tag="img"))
    assert not panel.update_inline_text("page.title", "Image value")
    assert panel.text.text() == "Original title"
    panel.clear_selection()
    assert not panel.update_inline_text("page.title", "Closed selection")
    assert not panel.text.text()


def test_long_inline_text_stays_complete_without_synthetic_property_patch(inspector):
    panel = inspector
    original = "A long text label. " * 2400
    replacement = original + "Final characters from the preview."
    assert len(original) > 32_767
    panel.set_selection(selection(text=original))
    assert panel.text.text() == original
    assert not panel.has_pending_edits
    patches = []
    panel.patch_changed.connect(lambda *args: patches.append(args))
    assert panel.update_inline_text("page.title", replacement)
    assert panel.text.text() == replacement
    assert not panel.has_pending_edits
    panel.commit_pending_edits()
    assert patches == []


def test_zero_opacity_loads_as_zero_without_false_dirty_patch(inspector):
    data = selection()
    data["computed"]["opacity"] = 0
    patches = []
    inspector.patch_changed.connect(lambda *args: patches.append(args))
    inspector.set_selection(data)
    assert inspector.opacity.value() == 0
    inspector.commit_pending_edits()
    assert patches == []
