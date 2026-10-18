"""Exercise the completion popup through real mouse, keyboard, and paint events."""

from dataclasses import asdict
import importlib.util
import sys

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QTextCursor, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyle, QStyleOptionSlider, QStyleOptionViewItem

from test_code_editor import (  # Reuse the real Qt document/worker lifecycle fixtures.
    completion_names, documents, editor_app, wait_for,
)


def selected_name(popup):
    entry = popup.currentIndex().data(Qt.ItemDataRole.UserRole)
    return entry.get("name") if isinstance(entry, dict) else None


def hover_row(document, row):
    popup = document.editor.completer.popup()
    index = popup.model().index(row, 0)
    popup.scrollTo(index)
    # Start outside the suggestion list. The first Enter/MouseMove into a row
    # must update selection without a click or a previous move over another row.
    QTest.mouseMove(document.editor.viewport(), QPoint(4, 4))
    QTest.mouseMove(popup.viewport(), popup.visualRect(index).center())


def show_controlled_items(document, monkeypatch):
    requests = []
    monkeypatch.setattr(document._service, "submit", requests.append)
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    for timer in (document._completion_timer, document._signature_timer, document._syntax_timer):
        timer.stop()
    document.editor.show_completions(
        [
            {"name": "alpha", "kind": "function", "prefix_length": 2},
            {"name": "alphabet", "kind": "function", "prefix_length": 2},
            {"name": "alpine", "kind": "function", "prefix_length": 2},
        ],
        revision=document.editor.document().revision(),
        cursor_position=document.editor.textCursor().position(),
    )
    return requests


def detail_request(requests, name):
    return next((request for request in reversed(requests)
                 if request.action == "completion_detail" and request.completion_name == name), None)


def detail_reply(request, marker):
    return {
        **asdict(request),
        "backend": "jedi",
        "status": "Python project suggestions active",
        "name": request.completion_name,
        "description": "def " + request.completion_name,
        "signatures": [{"text": request.completion_name + "(value: str)",
                        "index": None, "parameters": ["value"]}],
        "docstring": marker,
        "detail_status": "ready",
    }


@pytest.mark.parametrize("accept", ["tab", "enter", "click"])
def test_hover_highlights_nonfirst_row_and_accepts_exact_hovered_symbol(
    editor_app, documents, monkeypatch, accept,
):
    document = documents("al")
    show_controlled_items(document, monkeypatch)
    popup = document.editor.completer.popup()
    wait_for(editor_app, popup.isVisible)
    assert selected_name(popup) == "alpha"
    hover_row(document, 1)
    wait_for(editor_app, lambda: selected_name(popup) == "alphabet", timeout=1)
    assert popup.selectionModel().isSelected(popup.model().index(1, 0))
    assert not popup.selectionModel().isSelected(popup.model().index(0, 0))
    if accept == "click":
        QTest.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton,
                         pos=popup.visualRect(popup.currentIndex()).center())
    else:
        QTest.keyClick(document.editor, Qt.Key.Key_Tab if accept == "tab" else Qt.Key.Key_Return)
    wait_for(editor_app, lambda: document.editor.toPlainText() == "alphabet", timeout=1)
    assert not popup.isVisible()
    assert document.is_modified
    assert document.path.read_text() == "al"


def test_hover_and_arrow_keys_show_docs_for_reexported_bound_methods(
    editor_app, documents, tmp_path,
):
    # Mirrors APIs that export bound methods through their module namespace,
    # rather than top-level functions whose docs are easier to resolve.
    package = tmp_path / "widget_api"
    package.mkdir()
    (package / "renderers.py").write_text(
        'class RenderTools:\n'
        '    def write(self, value: str, expanded: bool = False):\n'
        '        """Write a value into the current dashboard."""\n'
        '        return value\n'
        '    def warning(self, message: str, icon: str = "!"):\n'
        '        """Show a warning banner without stopping the app."""\n'
        '        return message\n', encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        "from .renderers import RenderTools\n"
        "_runtime = RenderTools()\n"
        "write = _runtime.write\n"
        "warning = _runtime.warning\n", encoding="utf-8",
    )
    document = documents("import widget_api as widgets\nwidgets.w")
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(document.editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    popup = document.editor.completer.popup()
    wait_for(editor_app, lambda: popup.isVisible() and "write" in completion_names(document)
             and "warning" in completion_names(document))
    write_row = completion_names(document).index("write")
    warning_row = completion_names(document).index("warning")
    assert write_row > 0 and warning_row == write_row - 1
    hover_row(document, write_row)
    wait_for(editor_app, lambda: "Write a value into the current dashboard." in popup.details_text.toPlainText())
    assert selected_name(popup) == "write"
    assert "write" in popup.details_title.text()
    assert "value" in popup.details_text.toPlainText()
    assert not popup.details_only
    assert document.editor.toPlainText().endswith("widgets.w")
    QTest.keyClick(document.editor, Qt.Key.Key_Up)
    wait_for(editor_app, lambda: selected_name(popup) == "warning"
             and "Show a warning banner without stopping the app." in popup.details_text.toPlainText())
    assert "message" in popup.details_text.toPlainText()
    assert "Write a value into the current dashboard." not in popup.details_text.toPlainText()
    QTest.keyClick(document.editor, Qt.Key.Key_Down)
    wait_for(editor_app, lambda: selected_name(popup) == "write"
             and "Write a value into the current dashboard." in popup.details_text.toPlainText())
    QTest.keyClick(document.editor, Qt.Key.Key_Tab)
    assert document.editor.toPlainText().endswith("widgets.write")
    assert not popup.isVisible()


@pytest.mark.skipif(importlib.util.find_spec("streamlit") is None, reason="Streamlit package not installed")
def test_streamlit_bar_chart_hover_shows_signature_and_package_documentation(
    editor_app, documents,
):
    already_imported = "streamlit" in sys.modules
    document = documents("import streamlit as st\nst.ba")
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(document.editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    popup = document.editor.completer.popup()
    wait_for(editor_app, lambda: popup.isVisible() and "bar_chart" in completion_names(document))
    hover_row(document, completion_names(document).index("bar_chart"))
    wait_for(editor_app, lambda: "Display a bar chart." in popup.details_text.toPlainText(), timeout=20)
    assert selected_name(popup) == "bar_chart"
    assert "bar_chart" in popup.details_title.text()
    assert "data" in popup.details_text.toPlainText()
    assert "Parameters" in popup.details_text.toPlainText()
    assert ("streamlit" in sys.modules) == already_imported
    QTest.keyClick(document.editor, Qt.Key.Key_Return)
    assert document.editor.toPlainText() == "import streamlit as st\nst.bar_chart"


def test_late_documentation_never_replaces_newer_hover_selection(
    editor_app, documents, monkeypatch,
):
    document = documents("al")
    requests = show_controlled_items(document, monkeypatch)
    popup = document.editor.completer.popup()
    wait_for(editor_app, lambda: detail_request(requests, "alpha") is not None)
    first = detail_request(requests, "alpha")
    hover_row(document, 1)
    wait_for(editor_app, lambda: detail_request(requests, "alphabet") is not None)
    second = detail_request(requests, "alphabet")
    document._on_assist_result(detail_reply(second, "CURRENT alphabet documentation."))
    assert "CURRENT alphabet documentation." in popup.details_text.toPlainText()
    document._on_assist_result(detail_reply(first, "STALE alpha documentation."))
    assert selected_name(popup) == "alphabet"
    assert "CURRENT alphabet documentation." in popup.details_text.toPlainText()
    assert "STALE" not in popup.details_text.toPlainText()


def test_reading_and_scrolling_documentation_preserves_selection_and_editor_text(
    editor_app, documents, monkeypatch,
):
    document = documents("al")
    requests = show_controlled_items(document, monkeypatch)
    popup = document.editor.completer.popup()
    hover_row(document, 1)
    wait_for(editor_app, lambda: detail_request(requests, "alphabet") is not None)
    detail = "\n".join(f"Parameter {index}: explanation of the selected symbol." for index in range(50))
    document._on_assist_result(detail_reply(detail_request(requests, "alphabet"), detail))
    viewport = popup.details_text.viewport()
    point = viewport.rect().center()
    QTest.mouseMove(viewport, point)
    QTest.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=point)
    assert popup.isVisible()
    assert selected_name(popup) == "alphabet"
    assert document.editor.toPlainText() == "al"
    scroll_bar = popup.details_text.verticalScrollBar()
    wait_for(editor_app, lambda: scroll_bar.maximum() > 0)
    event = QWheelEvent(
        QPointF(point), QPointF(viewport.mapToGlobal(point)), QPoint(), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    editor_app.sendEvent(viewport, event)
    wait_for(editor_app, lambda: scroll_bar.value() > 0, timeout=1)
    assert popup.isVisible() and selected_name(popup) == "alphabet"
    assert document.editor.toPlainText() == "al"


def test_background_completion_enrichment_cannot_reopen_popup_after_escape(
    editor_app, documents, monkeypatch,
):
    document = documents("pr")
    requests = []
    monkeypatch.setattr(document._service, "submit", requests.append)
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(document.editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    popup = document.editor.completer.popup()
    wait_for(editor_app, lambda: popup.isVisible() and any(item.action == "complete" for item in requests))
    pending = next(item for item in requests if item.action == "complete")
    QTest.keyClick(document.editor, Qt.Key.Key_Escape)
    assert not popup.isVisible()
    document._on_assist_result({
        **asdict(pending), "backend": "jedi", "status": "Python project suggestions active",
        "completions": [{"name": "print", "kind": "function", "prefix_length": 2}],
        "signatures": [],
    })
    assert not popup.isVisible()


def test_long_suggestion_list_scrollbar_mouse_drag_wheel_and_selection(
    editor_app, documents, monkeypatch,
):
    document = documents("al")
    show_controlled_items(document, monkeypatch)
    document.editor.show_completions(
        [{"name": f"alpha_{index:03d}", "kind": "function", "prefix_length": 2}
         for index in range(80)],
        revision=document.editor.document().revision(),
        cursor_position=document.editor.textCursor().position(),
    )
    popup = document.editor.completer.popup()
    scrollbar = popup.list_scrollbar
    wait_for(editor_app, lambda: popup.isVisible() and scrollbar.isVisible() and scrollbar.maximum() > 0)
    assert popup.childAt(scrollbar.geometry().center()) is scrollbar
    assert not scrollbar.geometry().intersects(popup.details_panel.geometry())
    assert popup.indexAt(QPoint(4, 4)).row() == 0

    # Click the scrollbar track beneath its handle to page through suggestions.
    QTest.mouseClick(scrollbar, Qt.MouseButton.LeftButton, pos=scrollbar.rect().center())
    wait_for(editor_app, lambda: scrollbar.value() > 0, timeout=1)
    assert popup.indexAt(QPoint(4, 4)).row() > 0
    assert popup.isVisible() and document.editor.toPlainText() == "al"

    # Drag the actual visible handle, rather than changing a scroll value in
    # code. This catches an obscured scrollbar or a disconnected mirror.
    option = QStyleOptionSlider()
    scrollbar.initStyleOption(option)
    handle = scrollbar.style().subControlRect(
        QStyle.ComplexControl.CC_ScrollBar, option,
        QStyle.SubControl.SC_ScrollBarSlider, scrollbar,
    )
    before_drag = scrollbar.value()
    QTest.mousePress(scrollbar, Qt.MouseButton.LeftButton, pos=handle.center())
    drag_target = QPoint(handle.center().x(), scrollbar.height() - 25)
    QTest.mouseMove(scrollbar, drag_target, 20)
    QTest.mouseRelease(scrollbar, Qt.MouseButton.LeftButton, pos=drag_target)
    wait_for(editor_app, lambda: scrollbar.value() > before_drag, timeout=1)
    assert scrollbar.value() == popup.verticalScrollBar().value()
    assert popup.isVisible() and document.editor.toPlainText() == "al"

    # Native QListView wheel scrolling must still synchronize the visible bar.
    viewport = popup.viewport()
    point = viewport.rect().center()
    before_wheel = scrollbar.value()
    wheel = QWheelEvent(
        QPointF(point), QPointF(viewport.mapToGlobal(point)), QPoint(), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    editor_app.sendEvent(viewport, wheel)
    wait_for(editor_app, lambda: scrollbar.value() < before_wheel, timeout=1)
    assert scrollbar.value() == popup.verticalScrollBar().value()
    index = popup.indexAt(point)
    expected = index.data(Qt.ItemDataRole.UserRole)["name"]
    QTest.mouseMove(viewport, point)
    wait_for(editor_app, lambda: selected_name(popup) == expected, timeout=1)
    QTest.keyClick(document.editor, Qt.Key.Key_Tab)
    assert document.editor.toPlainText() == expected
    assert not popup.isVisible()


def test_reopening_suggestions_refreshes_documentation_at_unchanged_cursor(
    editor_app, documents, monkeypatch,
):
    document = documents("al")
    requests = show_controlled_items(document, monkeypatch)
    popup = document.editor.completer.popup()
    wait_for(editor_app, lambda: detail_request(requests, "alpha") is not None)
    first = detail_request(requests, "alpha")
    document._on_assist_result(detail_reply(first, "Old imported-module documentation."))
    assert "Old imported-module documentation." in popup.details_text.toPlainText()
    QTest.keyClick(document.editor, Qt.Key.Key_Escape)
    requests = show_controlled_items(document, monkeypatch)
    wait_for(editor_app, lambda: detail_request(requests, "alpha") is not None)
    second = detail_request(requests, "alpha")
    assert second.version == first.version and second.cursor_position == first.cursor_position
    assert second.request_id != first.request_id
    document._on_assist_result(detail_reply(second, "Updated imported-module documentation."))
    assert "Updated imported-module documentation." in popup.details_text.toPlainText()
    assert "Old imported-module documentation." not in popup.details_text.toPlainText()


def test_enrichment_keeps_hovered_name_when_symbol_kind_is_refined(
    editor_app, documents, monkeypatch,
):
    document = documents("al")
    requests = show_controlled_items(document, monkeypatch)
    popup = document.editor.completer.popup()
    hover_row(document, 1)
    wait_for(editor_app, lambda: detail_request(requests, "alphabet") is not None)
    previous = detail_request(requests, "alphabet")
    document.editor.show_completions(
        [{"name": "alpha", "kind": "function", "prefix_length": 2},
         {"name": "alphabet", "kind": "instance", "prefix_length": 2}],
        revision=document.editor.document().revision(),
        cursor_position=document.editor.textCursor().position(),
    )
    assert selected_name(popup) == "alphabet"
    wait_for(editor_app, lambda: detail_request(requests, "alphabet").completion_kind == "instance")
    refined = detail_request(requests, "alphabet")
    document._on_assist_result(detail_reply(refined, "Refined instance documentation."))
    document._on_assist_result(detail_reply(previous, "STALE function documentation."))
    assert "Refined instance documentation." in popup.details_text.toPlainText()
    assert "STALE function documentation." not in popup.details_text.toPlainText()


@pytest.mark.parametrize("action", ["escape", "type", "move_cursor", "plain_text", "close"])
def test_pending_docs_cannot_reopen_dismissed_or_invalidated_popup(
    editor_app, documents, monkeypatch, action,
):
    document = documents("al")
    requests = show_controlled_items(document, monkeypatch)
    popup = document.editor.completer.popup()
    wait_for(editor_app, lambda: detail_request(requests, "alpha") is not None)
    pending = detail_request(requests, "alpha")
    if action == "escape":
        QTest.keyClick(document.editor, Qt.Key.Key_Escape)
    elif action == "type":
        QTest.keyClicks(document.editor, "x")
        document._completion_timer.stop()
    elif action == "move_cursor":
        document.editor.moveCursor(QTextCursor.MoveOperation.Start)
    elif action == "plain_text":
        document.set_language_mode("plain")
    else:
        document.shutdown()
    assert not popup.isVisible()
    document._on_assist_result(detail_reply(pending, "STALE documentation must stay hidden."))
    editor_app.processEvents()
    assert not popup.isVisible()
    assert "STALE documentation must stay hidden." not in popup.details_text.toPlainText()


def _paint_item_background(popup, *, selected, active):
    # QCompleter commonly leaves keyboard focus in the editor. Paint that
    # inactive/no-focus selection explicitly, so a platform's inactive palette
    # cannot erase the chosen row (the reported Windows symptom).
    option = QStyleOptionViewItem()
    option.initFrom(popup)
    option.widget = popup
    option.rect = QRect(0, 0, 340, 36)
    option.state = QStyle.StateFlag.State_Enabled
    if selected:
        option.state |= QStyle.StateFlag.State_Selected
    if active:
        option.state |= QStyle.StateFlag.State_Active
    option.palette.setCurrentColorGroup(QPalette.ColorGroup.Active if active else QPalette.ColorGroup.Inactive)
    image = QImage(option.rect.size(), QImage.Format.Format_ARGB32)
    image.fill(QColor("#101827"))
    painter = QPainter(image)
    popup.itemDelegate().paint(painter, option, popup.model().index(0, 0))
    painter.end()
    return image.pixelColor(325, 18)


def test_selected_row_remains_visibly_highlighted_without_popup_focus(
    editor_app, documents, monkeypatch,
):
    document = documents("al")
    show_controlled_items(document, monkeypatch)
    popup = document.editor.completer.popup()
    wait_for(editor_app, popup.isVisible)
    inactive = _paint_item_background(popup, selected=True, active=False)
    active = _paint_item_background(popup, selected=True, active=True)
    unselected = _paint_item_background(popup, selected=False, active=False)
    assert inactive == active
    difference = sum(abs(left - right) for left, right in zip(
        (inactive.red(), inactive.green(), inactive.blue()),
        (unselected.red(), unselected.green(), unselected.blue()),
    ))
    assert difference >= 60, "The selected row must stand out even while keyboard focus stays in the editor"
