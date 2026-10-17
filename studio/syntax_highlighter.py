"""Readable, stateful highlighting that also works without optional packages."""

from __future__ import annotations

import builtins
import keyword
import re
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

try:
    from pygments import lex
    from pygments.lexers import get_lexer_for_filename
    from pygments.token import Token
    from pygments.util import ClassNotFound
except ImportError:
    lex = None


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def codepoint_column(text: str, utf16_column: int) -> int:
    """Translate a QTextCursor column to the column expected by Python/Jedi."""

    return len(text.encode("utf-16-le", errors="surrogatepass")[:utf16_column * 2]
               .decode("utf-16-le", errors="ignore"))


class EditorHighlighter(QSyntaxHighlighter):
    """Python is incremental; optional Pygments covers other project languages.

    Python strings/comments are scanned in order and carry state between blocks.
    This avoids regex highlighting turning '# in a string' into a comment or
    treating a quoted word in a comment as live code.
    """

    COLORS = {
        "keyword": "#c792ea", "builtin": "#82aaff", "string": "#c3e88d",
        "number": "#f78c6c", "comment": "#8e9db5", "function": "#ffcb6b",
        "operator": "#89ddff", "tag": "#f07178", "text": "#dce6f5",
        "attribute": "#b7d5ff", "error": "#ff8797",
    }
    _string_start = re.compile(r"(?i:(?:br|rb|fr|rf|tr|rt|r|u|b|f|t)?)(?:\"\"\"|'''|\"|')")
    _word = re.compile(r"[^\W\d]\w*", re.UNICODE)
    _number = re.compile(
        r"(?:0[xX][\da-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|"
        r"(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?[\d_]+)?[jJ]?)"
    )

    def __init__(self, document, path: Path, *, language_mode: str = "auto") -> None:
        super().__init__(document)
        self.path = Path(path)
        self.is_plain = language_mode == "plain"
        self.is_python = language_mode == "python" or (
            language_mode == "auto" and self.path.suffix.lower() in {".py", ".pyw", ".pyi"}
        )
        self.formats: dict[str, QTextCharFormat] = {}
        for kind, color in self.COLORS.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if kind == "keyword":
                fmt.setFontWeight(QFont.Weight.DemiBold)
            if kind == "comment":
                fmt.setFontItalic(True)
            self.formats[kind] = fmt
        self._keywords = set(keyword.kwlist) | set(getattr(keyword, "softkwlist", ()))
        self._builtins = set(dir(builtins)) | {"self", "cls"}
        self._lexer = None
        self._cached_source: str | None = None
        self._cache_valid = False
        self._spans: dict[int, list[tuple[int, int, str]]] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._rebuild_cache)
        if not self.is_python and not self.is_plain and lex is not None:
            try:
                self._lexer = get_lexer_for_filename(self.path.name, stripnl=False, ensurenl=False)
            except ClassNotFound:
                pass
        if self._lexer is not None:
            document.contentsChange.connect(self._contents_change)
            self._rebuild_cache()

    def _contents_change(self, _position: int, removed: int, added: int) -> None:
        if removed or added:
            self._cache_valid = False
            self._timer.start()

    def _token_kind(self, token) -> str:
        if token in Token.Comment:
            return "comment"
        if token in Token.Keyword:
            return "keyword"
        if token in Token.Literal.String:
            return "string"
        if token in Token.Literal.Number:
            return "number"
        if token in Token.Name.Builtin:
            return "builtin"
        if token in Token.Name.Function or token in Token.Name.Class:
            return "function"
        if token in Token.Name.Tag:
            return "tag"
        if token in Token.Name.Attribute or token in Token.Name.Decorator:
            return "attribute"
        if token in Token.Operator:
            return "operator"
        if token in Token.Generic.Heading:
            return "function"
        if token in Token.Error:
            return "error"
        return "text"

    def _rebuild_cache(self) -> None:
        document = self.document()
        if document is None or self._lexer is None:
            return
        source = document.toPlainText()
        if len(source) > 500_000:
            # Large files remain editable with basic incremental highlighting.
            self._cache_valid = False
            return
        if source == self._cached_source:
            self._cache_valid = True
            return
        spans: dict[int, list[tuple[int, int, str]]] = {}
        line, column = 0, 0
        try:
            for token, value in lex(source, self._lexer):
                kind = self._token_kind(token)
                pieces = value.split("\n")
                for index, piece in enumerate(pieces):
                    length = utf16_length(piece)
                    if length:
                        spans.setdefault(line, []).append((column, length, kind))
                        column += length
                    if index < len(pieces) - 1:
                        line += 1
                        column = 0
        except Exception:
            self._cache_valid = False
            return
        self._spans = spans
        self._cached_source = source
        self._cache_valid = True
        self.rehighlight()

    def _paint(self, text: str, start: int, end: int, kind: str) -> None:
        self.setFormat(utf16_length(text[:start]), utf16_length(text[start:end]), self.formats[kind])

    @staticmethod
    def _string_end(text: str, start: int, quote: str) -> tuple[int, bool]:
        position = start
        while position < len(text):
            if text[position] == "\\":
                position += 2
            elif text.startswith(quote, position):
                return position + len(quote), True
            else:
                position += 1
        return len(text), False

    def _highlight_python(self, text: str) -> None:
        self.setCurrentBlockState(0)
        position = 0
        previous = self.previousBlockState()
        quotes = {1: "'''", 2: '\"\"\"', 3: "'", 4: '\"'}
        if previous in quotes:
            quote = quotes[previous]
            end, closed = self._string_end(text, 0, quote)
            self._paint(text, 0, end, "string")
            if not closed:
                if len(quote) == 3 or self._has_continuation(text):
                    self.setCurrentBlockState(previous)
                return
            position = end
        expects_definition = False
        while position < len(text):
            character = text[position]
            if character.isspace():
                position += 1
                continue
            if character == "#":
                self._paint(text, position, len(text), "comment")
                return
            string = self._string_start.match(text, position)
            if string is not None:
                marker = string.group()
                quote = "'''" if marker.endswith("'''") else '\"\"\"' if marker.endswith('\"\"\"') else marker[-1]
                end, closed = self._string_end(text, string.end(), quote)
                self._paint(text, position, end, "string")
                if not closed:
                    if len(quote) == 3 or self._has_continuation(text):
                        self.setCurrentBlockState({"'''": 1, '\"\"\"': 2, "'": 3, '\"': 4}[quote])
                    return
                position = end
                continue
            word = self._word.match(text, position)
            if word is not None:
                value = word.group()
                if expects_definition:
                    kind = "function"
                elif value in self._keywords:
                    kind = "keyword"
                elif value in self._builtins:
                    kind = "builtin"
                elif re.match(r"\s*\(", text[word.end():]):
                    kind = "function"
                else:
                    kind = "text"
                self._paint(text, position, word.end(), kind)
                expects_definition = value in {"def", "class"}
                position = word.end()
                continue
            number = self._number.match(text, position)
            if number is not None:
                self._paint(text, position, number.end(), "number")
                position = number.end()
                continue
            if character in "+-*/%=<>!&|^~:@":
                self._paint(text, position, position + 1, "operator")
            position += 1

    @staticmethod
    def _has_continuation(text: str) -> bool:
        return (len(text) - len(text.rstrip("\\"))) % 2 == 1

    def _highlight_basic(self, text: str) -> None:
        """Small fallback when Pygments is absent or a file is very large."""

        self.setCurrentBlockState(0)
        for match in re.finditer(r"\b(?:true|false|null|const|let|var|function|return|if|else|class|import|export|async|await|for|while)\b", text):
            self._paint(text, match.start(), match.end(), "keyword")
        for match in self._number.finditer(text):
            self._paint(text, match.start(), match.end(), "number")
        for match in re.finditer(r"[\w.-]+(?=\s*[:=])", text):
            self._paint(text, match.start(), match.end(), "attribute")
        position = 0
        if self.previousBlockState() == 1:
            end = text.find("*/")
            self._paint(text, 0, len(text) if end < 0 else end + 2, "comment")
            if end < 0:
                self.setCurrentBlockState(1)
                return
            position = end + 2
        while position < len(text):
            if text.startswith("/*", position):
                end = text.find("*/", position + 2)
                self._paint(text, position, len(text) if end < 0 else end + 2, "comment")
                if end < 0:
                    self.setCurrentBlockState(1)
                    return
                position = end + 2
            elif text[position] in "\"'`":
                end, _ = self._string_end(text, position + 1, text[position])
                kind = "attribute" if text[end:].lstrip().startswith(":") else "string"
                self._paint(text, position, end, kind)
                position = end
            elif text.startswith("//", position) or text[position] == "#":
                self._paint(text, position, len(text), "comment")
                return
            else:
                position += 1

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt API
        if self.is_plain:
            self.setCurrentBlockState(0)
        elif self.is_python:
            self._highlight_python(text)
        elif self._cache_valid:
            self.setCurrentBlockState(0)
            for start, length, kind in self._spans.get(self.currentBlock().blockNumber(), ()):
                self.setFormat(start, length, self.formats[kind])
        else:
            self._highlight_basic(text)
