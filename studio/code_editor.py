from __future__ import annotations

import html
import io
import os
import re
import stat
import tokenize
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QFontMetrics, QKeySequence, QPainter,
    QStandardItem, QStandardItemModel, QTextCursor, QTextFormat,
)
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from .python_assist import (
    AssistRequest, SYNTAX_PYTHON_VERSION, basic_completion_items,
    get_python_assist, shutdown_python_assist,
)
from .syntax_highlighter import EditorHighlighter, codepoint_column, utf16_length


TEXT_FILE_NAMES = {
    ".gitignore", ".gitattributes", ".editorconfig", ".env",
    "dockerfile", "makefile", "procfile", "license",
}
TEXT_EXTENSIONS = {
    ".py", ".pyw", ".pyi", ".json", ".jsonc", ".html", ".htm", ".xml", ".svg",
    ".css", ".scss", ".sass", ".less", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".md", ".markdown", ".rst", ".txt", ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".conf", ".csv", ".tsv", ".sql", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java", ".kt",
    ".kts", ".go", ".rs", ".lua", ".rb", ".php", ".swift", ".dart",
}
MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024


def is_supported_text_file(path: Path) -> bool:
    """Return whether a file is suitable for the built-in text editor."""

    path = Path(path)
    if not path.is_file():
        return False
    if path.name.lower() in TEXT_FILE_NAMES or path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    try:
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            return False
        with path.open("rb") as stream:
            sample = stream.read(4096)
    except OSError:
        return False
    return b"\x00" not in sample


EDITOR_STYLE = """
QWidget#CodeEditorDocument { background: #0b1220; color: #dce6f5; }
QPlainTextEdit#CodeTextEditor {
    background: #101827; color: #dce6f5; border: 1px solid #293952;
    border-radius: 5px; selection-background-color: #304f7a;
    selection-color: #ffffff; padding: 5px;
}
QLabel#CodeEditorPath { color: #a8bfdc; padding: 3px; }
QLabel#CodeEditorStatus { color: #a5b6cb; font-size: 11px; }
QLabel#PythonAssistStatus { color: #9bafc9; font-size: 11px; }
QLabel#CodeLanguageLabel { color: #a8bfdc; }
QComboBox#CodeLanguageSelector {
    background: #1b2d47; color: #e3efff; border: 1px solid #344c6b;
    border-radius: 4px; padding: 5px 7px; min-height: 21px;
}
QComboBox#CodeLanguageSelector QAbstractItemView {
    background: #172439; color: #e1edff; border: 1px solid #597da8;
    selection-background-color: #2e5688; selection-color: #ffffff;
}
QLabel#PythonSignature {
    color: #c8defa; background: #15243b; border: 1px solid #304663;
    border-radius: 4px; padding: 6px 9px;
}
QPushButton#CodeEditorActionButton, QPushButton#PythonHelpClose {
    background: #1b2d47; color: #e3efff; border: 1px solid #344c6b;
    border-radius: 4px; padding: 5px 10px; min-height: 21px;
}
QPushButton#CodeEditorActionButton:hover, QPushButton#PythonHelpClose:hover {
    background: #28476b; border-color: #5f8fc7;
}
QPushButton#CodeEditorActionButton:disabled { color: #73829a; background: #162235; }
QPushButton#PythonSyntaxStatus {
    color: #a5cbb7; background: transparent; border: none;
    padding: 2px 4px; text-align: left; font-size: 11px;
}
QPushButton#PythonSyntaxStatus[syntaxError="true"] { color: #ff98a8; }
QWidget#PythonHelpPanel { background: #152034; border: 1px solid #314562; border-radius: 5px; }
QLabel#PythonHelpTitle { color: #d6e8ff; font-weight: bold; border: none; }
QTextBrowser#PythonHelpText {
    background: #101b2c; color: #dce6f5; border: none; padding: 7px;
    selection-background-color: #304f7a; selection-color: #ffffff;
}
"""


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeTextEdit") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.editor.paint_line_number_area(event)


class CodeTextEdit(QPlainTextEdit):
    save_requested = Signal()
    completion_requested = Signal(bool)
    help_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("CodeTextEditor")
        available = set(QFontDatabase.families())
        family = next((name for name in ("Cascadia Code", "Cascadia Mono", "Consolas", "DejaVu Sans Mono")
                       if name in available), "monospace")
        font = QFont(family, 11)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(QFontMetrics(font).horizontalAdvance(" ") * 4)
        self.is_python = False
        self.comment_prefix = "#"
        self._diagnostic: dict | None = None
        self._completion_anchor: tuple[int, int] | None = None
        self._accepting_completion = False

        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_line_number_width(0)
        self._highlight_current_line()

        self.completion_model = QStandardItemModel(self)
        self.completer = QCompleter(self.completion_model, self)
        self.completer.setWidget(self)
        self.completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setMaxVisibleItems(10)
        self.completer.activated[QModelIndex].connect(self._accept_completion)
        popup = self.completer.popup()
        popup.setFont(font)
        popup.setStyleSheet("""
            QAbstractItemView {
                background: #172439; color: #e1edff; border: 1px solid #597da8;
                selection-background-color: #2e5688; selection-color: #ffffff;
                padding: 3px; outline: none;
            }
            QAbstractItemView::item { padding: 5px 9px; min-height: 22px; }
            QAbstractItemView::item:selected { background: #2e5688; color: #ffffff; }
        """)

    def line_number_area_width(self) -> int:
        return 16 + self.fontMetrics().horizontalAdvance("9") * len(str(max(1, self.blockCount())))

    def _update_line_number_width(self, _count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_width(0)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        contents = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(contents.left(), contents.top(), self.line_number_area_width(), contents.height())
        )

    def paint_line_number_area(self, event) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#0b1321"))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()
        error_line = self._diagnostic.get("line", 0) - 1 if self._diagnostic else -1
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                color = "#ff8fa5" if number == error_line else "#a1bee2" if number == current else "#6f839f"
                painter.setPen(QColor(color))
                painter.drawText(0, top, self.line_number_area.width() - 8,
                                 self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    def set_syntax_diagnostic(self, diagnostic: dict | None) -> None:
        self._diagnostic = diagnostic if diagnostic and not diagnostic.get("ok") else None
        self._highlight_current_line()
        self.line_number_area.update()

    def diagnostic_cursor(self) -> QTextCursor | None:
        if not self._diagnostic:
            return None
        error = self._diagnostic
        block = self.document().findBlockByNumber(max(0, error.get("line", 1) - 1))
        if not block.isValid():
            block = self.document().lastBlock()
        text = block.text()
        start_cp = min(len(text), max(0, error.get("column", 0)))
        end_cp = error.get("end_column", start_cp + 1)
        if error.get("end_line", error.get("line")) != error.get("line"):
            end_cp = len(text)
        end_cp = min(len(text), max(start_cp + 1, end_cp))
        if start_cp == end_cp and start_cp > 0:
            start_cp -= 1
        cursor = QTextCursor(self.document())
        cursor.setPosition(block.position() + utf16_length(text[:start_cp]))
        cursor.setPosition(block.position() + utf16_length(text[:end_cp]), QTextCursor.MoveMode.KeepAnchor)
        return cursor

    def _highlight_current_line(self) -> None:
        current = QTextEdit.ExtraSelection()
        current.format.setBackground(QColor("#17263d"))
        current.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        current.cursor = self.textCursor()
        current.cursor.clearSelection()
        selections = [current]
        diagnostic_cursor = self.diagnostic_cursor()
        if diagnostic_cursor is not None:
            error = QTextEdit.ExtraSelection()
            error.cursor = diagnostic_cursor
            error.format.setUnderlineStyle(error.format.UnderlineStyle.WaveUnderline)
            error.format.setUnderlineColor(QColor("#ff7e96"))
            error.format.setToolTip(self._diagnostic.get("message", "Syntax error"))
            selections.append(error)
        self.setExtraSelections(selections)
        self.line_number_area.update()

    def hide_completions(self) -> None:
        self.completer.popup().hide()
        self._completion_anchor = None

    def show_completions(self, items: list[dict], *, revision: int, cursor_position: int) -> None:
        selected_name = None
        if self._completion_anchor == (revision, cursor_position) and self.completer.popup().isVisible():
            selected = self.completer.popup().currentIndex().data(Qt.ItemDataRole.UserRole)
            if isinstance(selected, dict):
                selected_name = selected.get("name")
        self.hide_completions()
        self.completion_model.clear()
        selected_row = 0
        for entry in items:
            if entry["name"] == selected_name:
                selected_row = self.completion_model.rowCount()
            item = QStandardItem(f"{entry['name']}    {entry.get('kind', '')}")
            item.setData(entry, Qt.ItemDataRole.UserRole)
            item.setToolTip(f"{entry['name']} ({entry.get('kind', 'symbol')}) — Tab or Enter to insert")
            self.completion_model.appendRow(item)
        if not items or not self.isVisible() or not self.hasFocus():
            return
        self._completion_anchor = (revision, cursor_position)
        self.completer.setCompletionPrefix("")
        rect = self.cursorRect()
        rect.setWidth(min(610, max(320, self.viewport().width() - rect.left() - 12)))
        self.completer.complete(rect)
        self.completer.popup().setCurrentIndex(self.completer.completionModel().index(selected_row, 0))

    def _accept_completion(self, index: QModelIndex) -> None:
        context = self._completion_anchor
        if context is None or context != (self.document().revision(), self.textCursor().position()):
            self.hide_completions()
            return
        entry = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            self.hide_completions()
            return
        cursor = self.textCursor()
        text = cursor.block().text()
        column = codepoint_column(text, cursor.positionInBlock())
        start_cp = max(0, column - int(entry.get("prefix_length", 0)))
        end_cp = column
        # Completing pri|nt replaces the whole identifier, and a pre-existing
        # keyword '=' is consumed when the selected completion includes it.
        while end_cp < len(text) and (text[end_cp].isalnum() or text[end_cp] == "_"):
            end_cp += 1
        replacement = entry["name"]
        if replacement.endswith("=") and text[end_cp:end_cp + 1] == "=":
            end_cp += 1
        start = cursor.block().position() + utf16_length(text[:start_cp])
        end = cursor.block().position() + utf16_length(text[:end_cp])
        self.hide_completions()
        self._accepting_completion = True
        cursor.beginEditBlock()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(replacement)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self._accepting_completion = False

    def _selected_blocks(self) -> list:
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        first = self.document().findBlock(start)
        last = self.document().findBlock(end)
        if cursor.hasSelection() and end == last.position() and last != first:
            last = last.previous()
        blocks = []
        block = first
        while block.isValid():
            blocks.append(block)
            if block == last:
                break
            block = block.next()
        return blocks

    def change_indentation(self, *, outdent: bool) -> None:
        original = self.textCursor()
        if not original.hasSelection() and not outdent:
            column = original.positionInBlock()
            original.insertText(" " * (4 - column % 4))
            self.setTextCursor(original)
            return
        blocks = self._selected_blocks()
        original.beginEditBlock()
        for block in reversed(blocks):
            cursor = QTextCursor(block)
            text = block.text()
            if outdent:
                count = 1 if text.startswith("\t") else min(4, len(text) - len(text.lstrip(" ")))
                cursor.setPosition(block.position() + count, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
            else:
                cursor.insertText("    ")
        original.endEditBlock()
        self.setTextCursor(original)

    def toggle_comment(self) -> None:
        blocks = self._selected_blocks()
        prefix = self.comment_prefix
        nonempty = [block for block in blocks if block.text().strip()]
        if not nonempty:
            self.insertPlainText(prefix + " ")
            return
        remove = all(block.text().lstrip().startswith(prefix) for block in nonempty)
        common_indent = min(len(block.text()) - len(block.text().lstrip()) for block in nonempty)
        original = self.textCursor()
        original.beginEditBlock()
        for block in reversed(nonempty):
            text = block.text()
            indent = len(text) - len(text.lstrip()) if remove else common_indent
            cursor = QTextCursor(self.document())
            cursor.setPosition(block.position() + utf16_length(text[:indent]))
            if remove:
                count = len(prefix) + (1 if text[indent + len(prefix):].startswith(" ") else 0)
                cursor.setPosition(cursor.position() + count, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
            else:
                cursor.insertText(prefix + " ")
        original.endEditBlock()
        self.setTextCursor(original)

    def _insert_indented_newline(self) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            cursor.removeSelectedText()
        line = cursor.block().text()
        column = codepoint_column(line, cursor.positionInBlock())
        before = line[:column]
        indentation = re.match(r"[ \t]*", before).group()
        if self.is_python:
            tokens = []
            try:
                for token in tokenize.generate_tokens(io.StringIO(before).readline):
                    if token.type not in {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE,
                                          tokenize.NL, tokenize.ENDMARKER, tokenize.COMMENT}:
                        tokens.append(token)
            except (tokenize.TokenError, IndentationError, SyntaxError):
                pass
            previous_state = cursor.block().previous().userState()
            if previous_state not in {1, 2, 3, 4} and tokens:
                operators = [token.string for token in tokens if token.type == tokenize.OP]
                opens = sum(value in "([{" for value in operators)
                closes = sum(value in ")]}" for value in operators)
                if (tokens[-1].type == tokenize.OP and tokens[-1].string == ":") or opens > closes:
                    indentation += "    "
        elif before.rstrip().endswith("{"):
            indentation += "    "
        cursor.insertText("\n" + indentation)
        cursor.endEditBlock()
        self.setTextCursor(cursor)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if event.matches(QKeySequence.StandardKey.Save):
            self.save_requested.emit()
            event.accept()
            return
        if self.is_python and ctrl and event.key() == Qt.Key.Key_Space:
            self.completion_requested.emit(True)
            event.accept()
            return
        if self.is_python and (event.key() == Qt.Key.Key_F1 or ctrl and event.key() == Qt.Key.Key_Q):
            self.help_requested.emit()
            event.accept()
            return
        if ctrl and event.key() == Qt.Key.Key_Slash:
            self.hide_completions()
            self.toggle_comment()
            event.accept()
            return
        if self.completer.popup().isVisible():
            if event.key() in {Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Tab} and not shift:
                self._accept_completion(self.completer.popup().currentIndex())
                event.accept()
                return
            if event.key() == Qt.Key.Key_Escape:
                self.hide_completions()
                event.accept()
                return
        if event.key() == Qt.Key.Key_Backtab or event.key() == Qt.Key.Key_Tab and shift:
            self.hide_completions()
            self.change_indentation(outdent=True)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Tab and not modifiers:
            self.change_indentation(outdent=False)
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and not ctrl:
            self._insert_indented_newline()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Backspace and not modifiers and not self.textCursor().hasSelection():
            cursor = self.textCursor()
            prefix = cursor.block().text()[:codepoint_column(cursor.block().text(), cursor.positionInBlock())]
            if prefix and not prefix.strip() and "\t" not in prefix:
                count = (len(prefix) - 1) % 4 + 1
                cursor.setPosition(cursor.position() - count, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
                self.setTextCursor(cursor)
                event.accept()
                return
        super().keyPressEvent(event)
        if self.is_python and not ctrl and not self._accepting_completion:
            typed = event.text()
            if (typed and (typed[-1].isalnum() or typed[-1] in "_.")) or event.key() in {Qt.Key.Key_Backspace, Qt.Key.Key_Delete}:
                self.completion_requested.emit(False)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.hide_completions()
        super().hideEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self.hide_completions()
        super().focusOutEvent(event)


class CodeEditorDocument(QWidget):
    """One file editor with native highlighting and optional Python assistance."""

    title_changed = Signal(str)
    status_message = Signal(str)
    saved = Signal(object)
    language_changed = Signal(str)

    def __init__(self, path: Path, parent=None, *, project_path: Path | None = None,
                 python_executable: str | Path | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CodeEditorDocument")
        self.setStyleSheet(EDITOR_STYLE)
        self.path = Path(path).expanduser().resolve()
        self.project_path = Path(project_path).expanduser().resolve() if project_path else self.path.parent
        self.python_executable = str(python_executable) if python_executable else None
        self.encoding = "utf-8"
        self.newline = "\n"
        self._loading = False
        self._loaded_successfully = False
        self._closed = False
        self._version = 0
        self._request_counter = 0
        self._last_request: dict[str, int] = {}
        self._manual_completion = False
        self._assist_backend: str | None = None
        self._assist_status = ""
        self._assist_detail = ""
        self._language_mode = "auto"
        self._python = self.path.suffix.lower() in {".py", ".pyw", ".pyi"}
        self._service = None
        self._document_id = 0
        self._assist_destroy_cleanup = None

        self.path_label = QLabel(str(self.path))
        self.path_label.setObjectName("CodeEditorPath")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setToolTip(str(self.path))
        self.path_label.setMinimumWidth(80)
        self.path_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        language_label = QLabel("Language:")
        language_label.setObjectName("CodeLanguageLabel")
        self.language_selector = QComboBox()
        self.language_selector.setObjectName("CodeLanguageSelector")
        self.language_selector.setAccessibleName("Editor language")
        self.language_selector.addItem("Auto (file name)", "auto")
        self.language_selector.addItem("Python", "python")
        self.language_selector.addItem("Plain text", "plain")
        self.language_selector.setToolTip(
            "Auto uses the file name (.py, .pyw or .pyi for Python). "
            "Choose Python to enable colours, suggestions and help for any open file. "
            "Changing language keeps your text and file name."
        )
        self.language_selector.currentIndexChanged.connect(
            lambda _index: self.set_language_mode(self.language_selector.currentData())
        )
        language_label.setBuddy(self.language_selector)

        self.save_button = self._action_button("Save", "Save this file (Ctrl+S)", self.save)
        self.reload_button = self._action_button("Reload", "Reload the file from disk", self.reload)
        self.suggest_button = self._action_button("Suggest", "Python completion (Ctrl+Space). Tab or Enter inserts; Esc dismisses.",
                                                 lambda: self.request_completion(True))
        self.help_button = self._action_button("Python Help", "Documentation for the name under the cursor (F1 or Ctrl+Q)",
                                              self.request_help)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.path_label, 1)
        header.addWidget(language_label)
        header.addWidget(self.language_selector)
        for button in (self.suggest_button, self.help_button, self.reload_button, self.save_button):
            header.addWidget(button)

        self.editor = CodeTextEdit()
        self.editor.is_python = self._python
        if self.path.suffix.lower() in {".js", ".ts", ".jsx", ".tsx", ".c", ".cpp", ".cs", ".java", ".go", ".rs", ".swift"}:
            self.editor.comment_prefix = "//"
        self.editor.save_requested.connect(self.save)
        self.editor.completion_requested.connect(self.request_completion)
        self.editor.help_requested.connect(self.request_help)
        self.editor.document().modificationChanged.connect(self._modified_changed)
        self.editor.textChanged.connect(self._text_changed)
        self.editor.cursorPositionChanged.connect(self._cursor_changed)

        self.signature_label = QLabel()
        self.signature_label.setObjectName("PythonSignature")
        self.signature_label.setTextFormat(Qt.TextFormat.RichText)
        self.signature_label.setWordWrap(True)
        self.signature_label.hide()

        self.help_panel = QWidget()
        self.help_panel.setObjectName("PythonHelpPanel")
        help_layout = QVBoxLayout(self.help_panel)
        help_layout.setContentsMargins(8, 6, 8, 5)
        help_layout.setSpacing(4)
        help_header = QHBoxLayout()
        self.help_title = QLabel("Python Help")
        self.help_title.setObjectName("PythonHelpTitle")
        help_close = QPushButton("Close")
        help_close.setObjectName("PythonHelpClose")
        help_close.clicked.connect(self.help_panel.hide)
        help_header.addWidget(self.help_title, 1)
        help_header.addWidget(help_close)
        self.help_text = QTextBrowser()
        self.help_text.setObjectName("PythonHelpText")
        self.help_text.setOpenExternalLinks(False)
        self.help_text.setFont(self.editor.font())
        self.help_text.setMinimumHeight(115)
        self.help_text.setMaximumHeight(200)
        help_layout.addLayout(help_header)
        help_layout.addWidget(self.help_text)
        self.help_panel.hide()

        self.status_label = QLabel()
        self.status_label.setObjectName("CodeEditorStatus")
        self.syntax_label = QPushButton("Syntax check: checking…")
        self.syntax_label.setObjectName("PythonSyntaxStatus")
        self.syntax_label.setMinimumWidth(160)
        self.syntax_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.syntax_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.syntax_label.setToolTip(
            f"Checks syntax using the editor's Python {SYNTAX_PYTHON_VERSION}. "
            "It does not execute your code, check types, or detect undefined names. "
            "Click an error to jump to its location."
        )
        self.syntax_label.clicked.connect(self._jump_to_diagnostic)
        self.syntax_label.setVisible(self._python)
        self.assist_label = QLabel()
        self.assist_label.setObjectName("PythonAssistStatus")
        self.assist_label.setWordWrap(True)
        footer = QHBoxLayout()
        footer.addWidget(self.status_label)
        footer.addWidget(self.syntax_label, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.signature_label)
        layout.addWidget(self.help_panel)
        layout.addLayout(footer)
        layout.addWidget(self.assist_label)

        self._completion_timer = self._timer(220, self._submit_completion)
        self._signature_timer = self._timer(350, lambda: self._submit("signature"))
        self._syntax_timer = self._timer(550, lambda: self._submit("syntax"))
        self.highlighter: EditorHighlighter | None = None
        if self._python:
            self._enable_python_assist()
        self._set_assist_ready()
        self.load()

    def _action_button(self, text: str, tooltip: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("CodeEditorActionButton")
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        return button

    def _timer(self, interval: int, callback) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval)
        timer.timeout.connect(callback)
        return timer

    def _enable_python_assist(self) -> None:
        if self._document_id:
            return
        self._service = get_python_assist()
        self._document_id = self._service.register()
        self._service.set_version(self._document_id, self._version)
        self._service.finished.connect(self._on_assist_result, Qt.ConnectionType.QueuedConnection)
        service, identifier = self._service, self._document_id
        self._assist_destroy_cleanup = lambda: service.unregister(identifier)
        self.destroyed.connect(self._assist_destroy_cleanup)

    def _disable_python_assist(self) -> None:
        if self._service is None or not self._document_id:
            return
        self._service.unregister(self._document_id)
        self._document_id = 0
        try:
            self._service.finished.disconnect(self._on_assist_result)
        except (RuntimeError, TypeError):
            pass
        if self._assist_destroy_cleanup is not None:
            try:
                self.destroyed.disconnect(self._assist_destroy_cleanup)
            except (RuntimeError, TypeError):
                pass
            self._assist_destroy_cleanup = None

    @property
    def language_mode(self) -> str:
        """Selected language override: auto, python or plain."""

        return self._language_mode

    def set_language_mode(self, mode: str) -> None:
        """Change editing tools without reloading or modifying the open file."""

        if mode not in {"auto", "python", "plain"}:
            raise ValueError(f"Unsupported editor language: {mode}")
        if self._closed or mode == self._language_mode:
            return
        self._invalidate_requests()
        self._disable_python_assist()
        self._language_mode = mode
        self._python = mode == "python" or (
            mode == "auto" and self.path.suffix.lower() in {".py", ".pyw", ".pyi"}
        )
        self._manual_completion = False
        self._assist_backend = None
        self._assist_status = ""
        self._assist_detail = ""
        self.editor.is_python = self._python
        self.editor.comment_prefix = (
            "//" if mode == "auto" and self.path.suffix.lower() in {
                ".js", ".ts", ".jsx", ".tsx", ".c", ".cpp", ".cs", ".java", ".go", ".rs", ".swift",
            } else "#"
        )
        self.editor.completion_model.clear()
        self.editor.set_syntax_diagnostic(None)
        self.signature_label.clear()
        self.signature_label.hide()
        self.help_text.clear()
        self.help_panel.hide()
        self.syntax_label.setVisible(self._python)
        self.syntax_label.setProperty("syntaxError", False)
        self.syntax_label.style().unpolish(self.syntax_label)
        self.syntax_label.style().polish(self.syntax_label)
        self.syntax_label.setText("Syntax check: checking…" if self._python else "")

        # Replacing formatting alone preserves the buffer, cursor, undo history,
        # encoding and newline convention. Never reload the file for a mode change.
        was_loading = self._loading
        self._loading = True
        try:
            self._replace_highlighter()
        finally:
            self._loading = was_loading
        self.language_selector.blockSignals(True)
        self.language_selector.setCurrentIndex(self.language_selector.findData(mode))
        self.language_selector.blockSignals(False)
        if self._python:
            self._enable_python_assist()
            self._syntax_timer.start(120)
        self._set_assist_ready()
        self._update_cursor_status()
        self.language_changed.emit(mode)
        self.editor.setFocus()

    def _replace_highlighter(self) -> None:
        if self.highlighter is not None:
            self.highlighter.setDocument(None)
            self.highlighter.deleteLater()
        self.highlighter = EditorHighlighter(
            self.editor.document(), self.path, language_mode=self._language_mode,
        )
        self.highlighter.rehighlight()

    @property
    def is_modified(self) -> bool:
        return self.editor.document().isModified()

    @property
    def display_title(self) -> str:
        return f"{'● ' if self.is_modified else ''}{self.path.name}"

    def _decode(self, raw: bytes) -> str:
        # Selecting an editing language must not change how this file's bytes
        # are decoded on reload. Python filenames retain encoding-cookie support.
        if self.path.suffix.lower() in {".py", ".pyw", ".pyi"}:
            try:
                encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
                text = raw.decode(encoding)
                self.encoding = encoding
                return text
            except (SyntaxError, UnicodeDecodeError, LookupError):
                pass
        if raw.startswith(b"\xef\xbb\xbf"):
            self.encoding = "utf-8-sig"
            return raw.decode("utf-8-sig")
        for encoding in ("utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(encoding)
                self.encoding = encoding
                return text
            except UnicodeDecodeError:
                pass
        self.encoding = "utf-8"
        return raw.decode("utf-8", errors="replace")

    def load(self) -> None:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_TEXT_FILE_BYTES + 1)
        except OSError as error:
            QMessageBox.critical(self, "Unable to open file", str(error))
            return
        if len(raw) > MAX_TEXT_FILE_BYTES or b"\x00" in raw[:4096]:
            QMessageBox.warning(self, "Unsupported file", "This file is binary or too large for the built-in editor.")
            return
        text = self._decode(raw)
        self.newline = "\r\n" if "\r\n" in text else "\r" if "\r" in text else "\n"
        self._loading = True
        self._invalidate_requests()
        self.editor.setPlainText(text.replace("\r\n", "\n").replace("\r", "\n"))
        self.editor.document().setModified(False)
        self._replace_highlighter()
        self._loading = False
        self._loaded_successfully = True
        self.editor.set_syntax_diagnostic(None)
        self.signature_label.hide()
        self.help_panel.hide()
        if self._python:
            self.syntax_label.setText("Syntax check: checking…")
            self._syntax_timer.start(120)
        self._update_cursor_status()
        self.title_changed.emit(self.display_title)
        self.status_message.emit(f"Opened {self.path}")

    def save(self) -> bool:
        if not self._loaded_successfully or self._closed:
            return False
        text = self.editor.toPlainText()
        if self.newline != "\n":
            text = text.replace("\n", self.newline)
        try:
            data = text.encode(self.encoding)
        except (LookupError, UnicodeEncodeError) as error:
            QMessageBox.critical(self, "Unable to save file",
                                 f"This file uses {self.encoding}. Some edited characters cannot be saved in that encoding.\n\n"
                                 f"Your unsaved text has been kept.\n\n{error}")
            return False
        temporary = self.path.with_name(f".{self.path.name}.hps-save.tmp")
        try:
            original_mode = stat.S_IMODE(self.path.stat().st_mode) if self.path.exists() else None
            temporary.write_bytes(data)
            if original_mode is not None:
                os.chmod(temporary, original_mode)
            os.replace(temporary, self.path)
        except OSError as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            QMessageBox.critical(self, "Unable to save file", str(error))
            return False
        self.editor.document().setModified(False)
        self.title_changed.emit(self.display_title)
        self.saved.emit(self.path)
        self.status_message.emit(f"Saved {self.path}")
        return True

    def reload(self) -> None:
        if self.is_modified:
            answer = QMessageBox.question(
                self, "Discard unsaved changes?", f"Reload {self.path.name} and discard your unsaved changes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.load()

    def can_close(self) -> bool:
        if not self.is_modified:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", f"Save changes to {self.path.name} before closing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return True

    def shutdown(self) -> None:
        """Call after close is approved, before removing/deleting this tab."""

        if self._closed:
            return
        self._closed = True
        self._completion_timer.stop()
        self._signature_timer.stop()
        self._syntax_timer.stop()
        self.editor.hide_completions()
        self._disable_python_assist()

    def _invalidate_requests(self) -> None:
        self._version += 1
        self._last_request.clear()
        self._completion_timer.stop()
        self._signature_timer.stop()
        self._syntax_timer.stop()
        self.editor.hide_completions()
        if self._service:
            self._service.set_version(self._document_id, self._version)

    def _text_changed(self) -> None:
        if self._loading or self._closed:
            return
        self._invalidate_requests()
        self.editor.set_syntax_diagnostic(None)
        self.signature_label.hide()
        if self._python:
            self.syntax_label.setText("Syntax check: checking…")
            self.syntax_label.setProperty("syntaxError", False)
            self.syntax_label.style().unpolish(self.syntax_label)
            self.syntax_label.style().polish(self.syntax_label)
            self._syntax_timer.start()
            if self.editor.hasFocus():
                self._signature_timer.start()
        self._update_cursor_status()

    def _modified_changed(self, _modified: bool) -> None:
        if not self._loading:
            self.title_changed.emit(self.display_title)
            self._update_cursor_status()

    def _cursor_changed(self) -> None:
        self._update_cursor_status()
        if self._loading or self._closed:
            return
        anchor = self.editor._completion_anchor
        if anchor and anchor[1] != self.editor.textCursor().position():
            self.editor.hide_completions()
        self.signature_label.hide()
        if self._python and self.editor.hasFocus():
            self._signature_timer.start()

    def _update_cursor_status(self) -> None:
        cursor = self.editor.textCursor()
        column = codepoint_column(cursor.block().text(), cursor.positionInBlock()) + 1
        newline = "CRLF" if self.newline == "\r\n" else "CR" if self.newline == "\r" else "LF"
        self.status_label.setText(
            f"Ln {cursor.blockNumber() + 1}, Col {column}   •   {self.encoding.upper()} / {newline}"
            f"   •   {'Modified' if self.is_modified else 'Saved'}"
        )

    def _set_assist_ready(self) -> None:
        self.suggest_button.setEnabled(self._python)
        self.help_button.setEnabled(self._python)
        if not self._python:
            if self._language_mode == "plain":
                explanation = "Plain text mode."
            elif not self.path.suffix:
                explanation = "This file has no extension."
            else:
                explanation = "Python assistance is off for this file type."
            self.assist_label.setText(
                f"{explanation} Choose Python in Language to enable code completion and help."
            )
            self.assist_label.setToolTip(
                "Changing the language keeps your current text and file name. "
                "In Auto mode, .py, .pyw and .pyi files enable Python tools."
            )
            return
        if self._assist_status:
            self.assist_label.setText(self._assist_status)
        elif self._service and not self._service.jedi_available:
            self.assist_label.setText("Basic Python suggestions active · Ctrl+Space suggestions · F1 help")
        else:
            self.assist_label.setText("Python · Ctrl+Space suggestions · F1 help · Ctrl+/ comment · Tab / Shift+Tab indent")
        detail = self._assist_detail
        if not detail and self._service and not self._service.jedi_available:
            detail = (
                "Jedi is not installed. Basic suggestions for Python names and this file remain available. "
                "Run Setup HoYo Project Studio.cmd to enable project and package suggestions."
            )
        self.assist_label.setToolTip(
            (f"{detail}\n\n" if detail else "")
            + f"Project: {self.project_path}\nCompletion interpreter: {self.python_executable or 'editor Python'}\n"
            f"Syntax checker: editor Python {SYNTAX_PYTHON_VERSION}. Checks syntax only, without executing edited code."
        )

    def request_completion(self, manual: bool = True) -> None:
        if not self._python or self._closed or self._loading:
            return
        if manual:
            self.editor.setFocus()
        if not self._service:
            return
        cursor = self.editor.textCursor()
        before = cursor.block().text()[:codepoint_column(cursor.block().text(), cursor.positionInBlock())]
        match = re.search(r"[\w]+$", before)
        if not manual and not before.endswith(".") and (match is None or len(match.group()) < 2):
            self.editor.hide_completions()
            return
        if not manual and self._in_string_or_comment():
            return
        self._manual_completion = manual
        if manual:
            self.assist_label.setText("Looking for Python suggestions…")
        self._completion_timer.start(0 if manual else 220)

    def _in_string_or_comment(self) -> bool:
        cursor = self.editor.textCursor()
        position = max(0, cursor.positionInBlock() - 1)
        for span in cursor.block().layout().formats():
            if span.start <= position < span.start + span.length:
                return span.format.foreground().color().name() in {
                    EditorHighlighter.COLORS["string"], EditorHighlighter.COLORS["comment"],
                }
        return False

    def _submit_completion(self) -> None:
        self._signature_timer.stop()
        if (self._closed or self._loading or not self._python or self._service is None
                or not self.isVisible() or not self.editor.hasFocus()):
            return
        cursor = self.editor.textCursor()
        items = basic_completion_items(
            self.editor.toPlainText(), cursor.blockNumber() + 1,
            codepoint_column(cursor.block().text(), cursor.positionInBlock()),
        )
        if items:
            self.editor.show_completions(
                items, revision=self.editor.document().revision(), cursor_position=cursor.position(),
            )
            self.assist_label.setText(
                "Basic Python suggestions active · project suggestions loading…"
                if self._service.jedi_available else "Basic Python suggestions active · Ctrl+Space suggestions · F1 help"
            )
        self._submit("complete")

    def request_help(self) -> None:
        if not self._python or self._closed:
            return
        self.editor.hide_completions()
        if not self._service:
            return
        self._show_help("Python Help", "Looking up documentation for the name at the cursor…")
        self._submit("help")

    def _submit(self, action: str) -> None:
        if self._closed or self._loading or not self._python or self._service is None:
            return
        if action in {"complete", "signature"} and (not self.isVisible() or not self.editor.hasFocus()):
            return
        cursor = self.editor.textCursor()
        self._request_counter += 1
        self._last_request[action] = self._request_counter
        self._service.submit(AssistRequest(
            document_id=self._document_id, version=self._version,
            request_id=self._request_counter, action=action,
            code=self.editor.toPlainText(), path=str(self.path), project_path=str(self.project_path),
            python_executable=self.python_executable,
            line=cursor.blockNumber() + 1,
            column=codepoint_column(cursor.block().text(), cursor.positionInBlock()),
            cursor_position=cursor.position(),
        ))

    def _on_assist_result(self, result: dict) -> None:
        if (self._closed or not self._python or result.get("document_id") != self._document_id
                or result.get("version") != self._version
                or result.get("request_id") != self._last_request.get(result.get("action"))):
            return
        action = result["action"]
        if action != "syntax" and result.get("cursor_position") != self.editor.textCursor().position():
            return
        if action in {"complete", "signature"} and (not self.isVisible() or not self.editor.hasFocus()):
            return
        if result.get("error"):
            if action == "syntax":
                self.syntax_label.setText("Syntax check: unavailable")
                self.syntax_label.setToolTip(result["error"])
            else:
                self.assist_label.setText(result["error"])
                if action == "help":
                    self._show_help("Python Help unavailable", result["error"])
            return
        if action == "syntax":
            diagnostic = result["diagnostic"]
            self.editor.set_syntax_diagnostic(diagnostic)
            if diagnostic["ok"]:
                self.syntax_label.setText(f"Syntax check: no errors · Python {SYNTAX_PYTHON_VERSION}")
            else:
                self.syntax_label.setText(f"Syntax check: line {diagnostic['line']} — {diagnostic['message'][:105]}")
            self.syntax_label.setToolTip(
                (f"Line {diagnostic['line']}: {diagnostic['message']}\n\n" if not diagnostic["ok"] else "")
                + f"Checks syntax using the editor's Python {SYNTAX_PYTHON_VERSION}. "
                "It does not execute your code, check types, or detect undefined names. "
                "Click an error to jump to its location."
            )
            self.syntax_label.setProperty("syntaxError", not diagnostic["ok"])
            self.syntax_label.style().unpolish(self.syntax_label)
            self.syntax_label.style().polish(self.syntax_label)
            return
        self._assist_backend = result.get("backend", self._assist_backend)
        self._assist_status = result.get("status", "")
        self._assist_detail = result.get("detail", "")
        self._set_assist_ready()
        if action == "complete":
            items = result.get("completions", [])
            self.editor.show_completions(items, revision=self.editor.document().revision(),
                                         cursor_position=result["cursor_position"])
            if not items and self._manual_completion:
                prefix = "Basic Python suggestions: " if self._assist_backend == "basic" else ""
                self.assist_label.setText(prefix + "No suggestions at this position. Put the cursor after a name or a dot, then press Ctrl+Space.")
        if "signatures" in result:
            signatures = result["signatures"]
            if signatures:
                signature = signatures[0]
                text = "<b>Call signature:</b> " + html.escape(signature["text"])
                active = signature.get("index")
                parameters = signature.get("parameters", [])
                if active is not None and 0 <= active < len(parameters):
                    text += " &nbsp; <b>Current argument:</b> " + html.escape(parameters[active])
                self.signature_label.setText(text)
                self.signature_label.show()
            else:
                self.signature_label.hide()
        if action == "help":
            entries = result.get("help", [])
            if not entries:
                self._show_help("Python Help", "No documentation found at this position.\n\nPlace the cursor on a Python name, function, class, or keyword, then press F1.\n\n"
                                "For project functions, add a docstring below the function definition to make your own documentation available here.")
            else:
                sections = [f"{entry['name']}\n{entry['description']}\n\n{entry['docstring'] or 'No docstring is available for this symbol.'}"
                            for entry in entries]
                self._show_help(f"Python Help — {entries[0]['name']}", "\n\n".join(sections))

    def _show_help(self, title: str, text: str) -> None:
        self.help_title.setText(title)
        self.help_text.setPlainText(text)
        self.help_panel.show()

    def _jump_to_diagnostic(self) -> None:
        cursor = self.editor.diagnostic_cursor()
        if cursor is not None:
            self.editor.setTextCursor(cursor)
            self.editor.centerCursor()
            self.editor.setFocus()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._completion_timer.stop()
        self._signature_timer.stop()
        self.editor.hide_completions()
        super().hideEvent(event)
