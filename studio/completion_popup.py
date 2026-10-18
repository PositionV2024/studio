from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QListView, QScrollBar, QTextBrowser, QVBoxLayout, QWidget


class CompletionPopup(QListView):
    """One native completer popup containing suggestions and their documentation.

    QCompleter owns keyboard routing and dismissal. Viewport margins reserve room
    for the detail pane without opening another window or transferring focus away
    from the editor while the user types or browses documentation.
    """

    entry_highlighted = Signal(object)
    dismissed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("PythonCompletionPopup")
        self.setAccessibleName("Python completions and documentation")
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QListView.SelectionBehavior.SelectRows)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.details_only = False
        self._detail_width = 375
        self._footer_height = 34
        self._updating = False
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.timeout.connect(self._layout_children)

        # A completer popup keeps the editor focused. Explicit inactive colours
        # are essential on Windows, where the ordinary inactive selection can
        # otherwise be nearly indistinguishable from the dark background.
        palette = self.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette.setColor(group, QPalette.ColorRole.Highlight, QColor("#35669c"))
            palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        self.setPalette(palette)
        self.setStyleSheet("""
            QListView#PythonCompletionPopup {
                background: #172439; color: #e1edff; border: 1px solid #597da8;
                selection-background-color: #35669c; selection-color: #ffffff;
                outline: none;
            }
            QListView#PythonCompletionPopup::item { padding: 6px 10px; min-height: 23px; }
            QListView#PythonCompletionPopup::item:hover { background: #294b72; color: #ffffff; }
            QListView#PythonCompletionPopup::item:selected,
            QListView#PythonCompletionPopup::item:selected:!active {
                background: #35669c; color: #ffffff;
            }
            QWidget#CompletionDetails { background: #101c2d; border-left: 1px solid #344c6b; }
            QLabel#CompletionDetailsTitle { color: #e4efff; border: none; font-weight: bold; }
            QTextBrowser#CompletionDetailsText {
                background: #101c2d; color: #ccdcef; border: none;
                selection-background-color: #35669c; selection-color: #ffffff;
            }
            QLabel#CompletionFooter {
                background: #122036; color: #a9bfdc; border-top: 1px solid #344c6b;
                padding-left: 9px; font-size: 11px;
            }
            QScrollBar:vertical { background: #172439; width: 11px; margin: 0; }
            QScrollBar::handle:vertical { background: #446084; min-height: 24px; border-radius: 4px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        self.details_panel = QWidget(self)
        self.details_panel.setObjectName("CompletionDetails")
        details_layout = QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(12, 10, 10, 7)
        details_layout.setSpacing(7)
        self.details_title = QLabel("Python completion")
        self.details_title.setObjectName("CompletionDetailsTitle")
        self.details_title.setWordWrap(True)
        self.details_title.setTextFormat(Qt.TextFormat.PlainText)
        self.details_text = QTextBrowser()
        self.details_text.setObjectName("CompletionDetailsText")
        self.details_text.setAccessibleName("Documentation for selected Python completion")
        self.details_text.setOpenExternalLinks(False)
        self.details_text.setOpenLinks(False)
        self.details_text.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.details_text.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        self.details_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        details_layout.addWidget(self.details_title)
        details_layout.addWidget(self.details_text, 1)
        self.footer = QLabel(self)
        self.footer.setObjectName("CompletionFooter")
        self.footer.setTextFormat(Qt.TextFormat.PlainText)
        # A native QListView reserves its own scrollbar at the outer window
        # edge, which belongs to the documentation pane in this composite popup.
        # Mirror its range and value to a visible native QScrollBar next to the
        # suggestion list. Wheel and keyboard scrolling still use QListView.
        self.list_scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self.list_scrollbar.setObjectName("CompletionListScrollBar")
        self.list_scrollbar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_scrollbar.setAccessibleName("Scroll Python suggestions")
        self.verticalScrollBar().valueChanged.connect(self.list_scrollbar.setValue)
        self.list_scrollbar.valueChanged.connect(self.verticalScrollBar().setValue)
        self.set_details_only(False)
        self.entered.connect(self._hovered)
        self.verticalScrollBar().rangeChanged.connect(lambda _minimum, _maximum: self._relayout_timer.start(0))

    def set_code_font(self, font: QFont) -> None:
        self.setFont(font)
        details_font = QFont(font)
        details_font.setPointSizeF(max(9, font.pointSizeF() - 1))
        self.details_text.setFont(details_font)

    def set_details_only(self, enabled: bool) -> None:
        self.details_only = enabled
        self.footer.setText(
            "F1 / Ctrl+Q  documentation    ·    Ctrl+Space  suggestions    ·    Esc  close"
            if enabled else
            "↑ ↓ or hover  select    ·    Tab / Enter or click  insert    ·    Ctrl+Space  suggestions    ·    Esc  close"
        )
        self._layout_children()

    def set_details(self, title: str, text: str) -> None:
        """Display plain text only; documentation is never interpreted as HTML."""

        self.details_title.setText(title)
        self.details_text.setPlainText(text)
        self.details_text.verticalScrollBar().setValue(0)
        self.details_panel.layout().activate()

    def selected_entry(self) -> dict | None:
        if self.details_only:
            return None
        entry = self.currentIndex().data(Qt.ItemDataRole.UserRole)
        return entry if isinstance(entry, dict) else None

    def _hovered(self, index: QModelIndex) -> None:
        if self._updating or self.details_only or not index.isValid():
            return
        if index != self.currentIndex():
            self.setCurrentIndex(index)

    def currentChanged(self, current: QModelIndex, previous: QModelIndex) -> None:  # noqa: N802
        super().currentChanged(current, previous)
        if self._updating or self.details_only:
            return
        entry = current.data(Qt.ItemDataRole.UserRole)
        if isinstance(entry, dict):
            self.entry_highlighted.emit(entry)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        # `entered` provides native hover semantics; indexAt also handles moving
        # back into a row after a keyboard selection or a model refresh.
        self._hovered(self.indexAt(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_children()
        self._relayout_timer.start(0)

    def _layout_children(self) -> None:
        if not hasattr(self, "details_panel"):
            return
        inner = self.contentsRect()
        detail_width = inner.width() if self.details_only else max(210, int(inner.width() * 0.53))
        detail_width = min(inner.width(), detail_width)
        self._detail_width = detail_width
        footer_height = min(self._footer_height, inner.height())
        source_scrollbar = self.verticalScrollBar()
        needs_list_scrollbar = not self.details_only and source_scrollbar.maximum() > source_scrollbar.minimum()
        scrollbar_width = 11 if needs_list_scrollbar else 0
        self.setViewportMargins(0, 0, detail_width + scrollbar_width, footer_height)
        self.details_panel.setGeometry(
            inner.right() - detail_width + 1, inner.top(), detail_width,
            max(0, inner.height() - footer_height),
        )
        self.footer.setGeometry(inner.left(), inner.bottom() - footer_height + 1,
                                inner.width(), footer_height)
        self.list_scrollbar.setRange(source_scrollbar.minimum(), source_scrollbar.maximum())
        self.list_scrollbar.setPageStep(source_scrollbar.pageStep())
        self.list_scrollbar.setSingleStep(source_scrollbar.singleStep())
        self.list_scrollbar.setValue(source_scrollbar.value())
        self.list_scrollbar.setGeometry(
            inner.right() - detail_width - scrollbar_width + 1, inner.top(),
            scrollbar_width, max(0, inner.height() - footer_height),
        )
        self.list_scrollbar.setVisible(needs_list_scrollbar)
        self.list_scrollbar.raise_()
        self.details_panel.raise_()
        self.footer.raise_()

    def fit_to_editor(self, editor) -> None:
        """Keep both columns, the footer, and long docs within the current screen."""

        cursor = editor.cursorRect()
        anchor = editor.viewport().mapToGlobal(cursor.bottomLeft())
        screen = QApplication.screenAt(anchor) or editor.screen()
        available = screen.availableGeometry().adjusted(7, 7, -7, -7)
        width = min(780 if not self.details_only else 550, available.width())
        row_count = self.model().rowCount() if self.model() else 0
        row_height = max(31, self.sizeHintForRow(0))
        wanted_height = max(330, min(9, max(5, row_count)) * row_height + self._footer_height + 3)
        if self.details_only:
            wanted_height = 305
        below = available.bottom() - anchor.y()
        cursor_top = editor.viewport().mapToGlobal(cursor.topLeft()).y()
        above = cursor_top - available.top()
        height = min(wanted_height, available.height())
        if below >= min(height, 240) or below >= above:
            height = min(height, max(120, below))
            y = anchor.y() + 2
        else:
            height = min(height, max(120, above))
            y = cursor_top - height - 2
        x = min(max(available.left(), anchor.x()), available.right() - width + 1)
        y = min(max(available.top(), y), available.bottom() - height + 1)
        self.setGeometry(QRect(x, y, width, height))
        self._layout_children()
        self._relayout_timer.start(0)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.dismissed.emit()
