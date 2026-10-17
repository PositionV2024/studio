"""Real Qt/Jedi checks for editing and asynchronous assistance behavior."""

import sys
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from studio.code_editor import CodeEditorDocument, shutdown_python_assist
from studio.python_assist import syntax_check
from studio.syntax_highlighter import EditorHighlighter, utf16_length


@pytest.fixture(scope="session")
def editor_app():
    app = QApplication.instance() or QApplication([])
    yield app
    shutdown_python_assist()


@pytest.fixture
def documents(editor_app, tmp_path):
    opened = []

    def create(source="", name="sample.py", raw=None):
        path = tmp_path / name
        path.write_bytes(raw if raw is not None else source.encode("utf-8"))
        document = CodeEditorDocument(path, project_path=tmp_path, python_executable=sys.executable)
        document.resize(1000, 720)
        document.show()
        document.activateWindow()
        document.editor.setFocus()
        editor_app.processEvents()
        opened.append(document)
        return document

    yield create
    for document in opened:
        document.shutdown()
        document.close()
        document.deleteLater()
    editor_app.processEvents()


def wait_for(app, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        # Unlike QTest.qWait in some PySide versions, this releases the GIL so
        # the shared Python worker can make progress while Qt pumps events.
        time.sleep(0.01)
    assert predicate(), "Timed out waiting for editor assistance"


def set_cursor(document, line, column):
    block = document.editor.document().findBlockByNumber(line - 1)
    cursor = QTextCursor(document.editor.document())
    cursor.setPosition(block.position() + utf16_length(block.text()[:column]))
    document.editor.setTextCursor(cursor)
    document.editor.setFocus()


def token_color(document, line, column):
    block = document.editor.document().findBlockByNumber(line - 1)
    position = utf16_length(block.text()[:column])
    return next((span.format.foreground().color().name()
                 for span in block.layout().formats()
                 if span.start <= position < span.start + span.length), None)


def completion_names(document):
    model = document.editor.completion_model
    return [model.item(index).data(Qt.ItemDataRole.UserRole)["name"] for index in range(model.rowCount())]


def choose_completion(document, name):
    row = completion_names(document).index(name)
    popup = document.editor.completer.popup()
    popup.setCurrentIndex(document.editor.completer.completionModel().index(row, 0))
    QTest.keyClick(document.editor, Qt.Key.Key_Tab)


def test_python_colors_respect_multiline_strings_comments_and_unicode(documents):
    document = documents('doc = """first\n# still a string\nlast"""\ntext = "😀 # string" # "comment"\nprint(42)\n')
    colors = EditorHighlighter.COLORS
    assert token_color(document, 2, 0) == colors["string"]
    assert token_color(document, 4, 12) == colors["string"]
    assert token_color(document, 4, 24) == colors["comment"]
    assert token_color(document, 5, 0) == colors["builtin"]
    assert token_color(document, 5, 6) == colors["number"]
    assert not document.is_modified


def test_pygments_keeps_json_keys_strings_and_numbers_distinct(documents):
    document = documents('{"name": "value", "count": 42}', name="data.json")
    assert token_color(document, 1, 2) == EditorHighlighter.COLORS["tag"]
    assert token_color(document, 1, 10) == EditorHighlighter.COLORS["string"]
    assert token_color(document, 1, document.editor.toPlainText().index("42")) == EditorHighlighter.COLORS["number"]


@pytest.mark.parametrize("raw", [
    b"\xef\xbb\xbfprint('hello')\r\n",
    b"# coding: latin-1\r\nname = 'caf\xe9'\r\n",
    b"print('hello')\r",
])
def test_save_and_reload_preserve_encoding_newlines_and_one_highlighter(documents, raw):
    document = documents(raw=raw)
    old_highlighter = document.highlighter
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    document.editor.insertPlainText("# added\n")
    assert document.save()
    assert document.path.read_bytes() == raw + ("# added" + document.newline).encode(document.encoding).removeprefix(b"\xef\xbb\xbf")
    document.load()
    assert old_highlighter.document() is None
    assert document.highlighter.document() is document.editor.document()
    assert not document.is_modified


def test_indent_comment_and_native_undo(documents):
    document = documents("if True:  # block\n")
    set_cursor(document, 1, len("if True:  # block"))
    QTest.keyClick(document.editor, Qt.Key.Key_Return)
    assert document.editor.toPlainText() == "if True:  # block\n    \n"
    QTest.keyClick(document.editor, Qt.Key.Key_Backtab)
    assert document.editor.textCursor().positionInBlock() == 0
    document.editor.setPlainText("one = 1\ntwo = 2\n")
    document.editor.selectAll()
    QTest.keyClick(document.editor, Qt.Key.Key_Tab)
    assert document.editor.toPlainText() == "    one = 1\n    two = 2\n"
    QTest.keyClick(document.editor, Qt.Key.Key_Backtab)
    assert document.editor.toPlainText() == "one = 1\ntwo = 2\n"
    QTest.keyClick(document.editor, Qt.Key.Key_Slash, Qt.KeyboardModifier.ControlModifier)
    assert document.editor.toPlainText() == "# one = 1\n# two = 2\n"
    QTest.keyClick(document.editor, Qt.Key.Key_Slash, Qt.KeyboardModifier.ControlModifier)
    assert document.editor.toPlainText() == "one = 1\ntwo = 2\n"
    document.editor.undo()
    assert document.editor.toPlainText() == "# one = 1\n# two = 2\n"


def test_automatic_completion_after_typing_and_dot(editor_app, documents):
    document = documents("")
    QTest.keyClicks(document.editor, "pri")
    wait_for(editor_app, lambda: document.editor.completer.popup().isVisible() and "print" in completion_names(document))
    choose_completion(document, "print")
    assert document.editor.toPlainText() == "print"
    document.editor.setPlainText("import os\nos")
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClick(document.editor, Qt.Key.Key_Period)
    wait_for(editor_app, lambda: document.editor.completer.popup().isVisible() and "path" in completion_names(document))
    choose_completion(document, "path")
    assert document.editor.toPlainText() == "import os\nos.path"


def test_completion_replaces_suffix_after_non_bmp_character(editor_app, documents):
    source = 'marker = "😀"; prnit'
    document = documents(source)
    set_cursor(document, 1, source.index("prnit") + 2)
    QTest.keyClick(document.editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    wait_for(editor_app, lambda: document.editor.completer.popup().isVisible() and "print" in completion_names(document))
    choose_completion(document, "print")
    assert document.editor.toPlainText() == 'marker = "😀"; print'


def test_project_import_completion_uses_unsaved_current_file(editor_app, documents, tmp_path):
    (tmp_path / "helpers.py").write_text("def welcome_user(name):\n    return name\n", encoding="utf-8")
    document = documents("")
    document.editor.setPlainText("from helpers import welcome_user\nwelcome_u")
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    document.request_completion(True)
    wait_for(editor_app, lambda: document.editor.completer.popup().isVisible() and "welcome_user" in completion_names(document))
    assert document.path.read_text() == ""
    choose_completion(document, "welcome_user")
    assert document.editor.toPlainText().endswith("\nwelcome_user")


def test_signatures_and_explicit_help(editor_app, documents):
    source = 'def greet(name: str, repeat: int = 1):\n    """Welcome a person by name."""\n    return name * repeat\n\ngreet('
    document = documents(source)
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    document._submit("signature")
    wait_for(editor_app, lambda: document.signature_label.isVisible())
    assert "greet(name:str,repeat:int=1)" in document.signature_label.text().replace(" ", "")
    assert "Current argument:</b> name" in document.signature_label.text()
    set_cursor(document, 5, 3)
    QTest.keyClick(document.editor, Qt.Key.Key_F1)
    wait_for(editor_app, lambda: "Welcome a person by name." in document.help_text.toPlainText())
    assert document.help_panel.isVisible()


def test_syntax_errors_underlined_and_compile_scope_errors_detected(editor_app, documents):
    document = documents("return 42\n")
    wait_for(editor_app, lambda: document.editor._diagnostic is not None)
    assert "outside function" in document.syntax_label.text()
    assert len(document.editor.extraSelections()) == 2
    document.syntax_label.click()
    assert document.editor.textCursor().blockNumber() == 0
    document.editor.setPlainText("answer = 42\n")
    wait_for(editor_app, lambda: "no errors" in document.syntax_label.text())
    assert len(document.editor.extraSelections()) == 1


def test_syntax_check_does_not_execute_imports_or_statements(tmp_path):
    target = tmp_path / "must-not-exist.txt"
    result = syntax_check(f"from pathlib import Path\nPath({str(target)!r}).write_text('executed')\n", "sample.py")
    assert result["ok"]
    assert not target.exists()


def test_one_worker_and_stale_replies_are_discarded(editor_app, documents, monkeypatch):
    first = documents("pri", name="first.py")
    second = documents("pri", name="second.py")
    assert first._service is second._service
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    def delayed_query(request):
        entered.set()
        release.wait(5)
        completed.set()
        return {"completions": [{"name": "STALE_NAME", "kind": "function", "prefix_length": 3}], "signatures": []}

    monkeypatch.setattr(second._service, "_query_jedi", delayed_query)
    second.editor.moveCursor(QTextCursor.MoveOperation.End)
    second.request_completion(True)
    wait_for(editor_app, entered.is_set)
    second.editor.insertPlainText("nt")
    release.set()
    wait_for(editor_app, completed.is_set)
    wait_for(editor_app, lambda: "no errors" in second.syntax_label.text())
    assert not second.editor.completer.popup().isVisible()
    assert "STALE_NAME" not in completion_names(second)
    second.shutdown()
    assert second._document_id not in second._service._active


@pytest.mark.parametrize("trigger", ["automatic", "button", "shortcut"])
def test_missing_jedi_still_completes_and_opens_builtin_help(editor_app, documents, monkeypatch, trigger):
    document = documents("answer = 42\n")
    monkeypatch.setattr(document._service, "jedi_available", False)
    document._set_assist_ready()
    assert "Basic Python suggestions" in document.assist_label.text()
    assert document.suggest_button.isEnabled()
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    QTest.keyClicks(document.editor, "pri")
    if trigger == "button":
        document._completion_timer.stop()
        document.suggest_button.setFocus()
        QTest.mouseClick(document.suggest_button, Qt.MouseButton.LeftButton)
    elif trigger == "shortcut":
        document._completion_timer.stop()
        QTest.keyClick(document.editor, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    wait_for(editor_app, lambda: document.editor.completer.popup().isVisible() and "print" in completion_names(document))
    assert document.editor.hasFocus()
    choose_completion(document, "print")
    assert document.editor.toPlainText() == "answer = 42\nprint"
    QTest.keyClick(document.editor, Qt.Key.Key_F1)
    wait_for(editor_app, lambda: "print(" in document.help_text.toPlainText())
    assert document.help_panel.isVisible()
    assert "Basic Python suggestions" in document.assist_label.text()
    assert document.save()
    wait_for(editor_app, lambda: "no errors" in document.syntax_label.text())


def test_slow_project_helper_keeps_immediate_suggestions_and_selection(editor_app, documents, monkeypatch):
    document = documents("pr")
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delayed_query(request):
        entered.set()
        release.wait(5)
        finished.set()
        return {
            "backend": "jedi", "status": "Python project suggestions active",
            "completions": [
                {"name": "print", "kind": "function", "prefix_length": 2},
                {"name": "private_name", "kind": "instance", "prefix_length": 2},
                {"name": "property", "kind": "class", "prefix_length": 2},
            ],
            "signatures": [],
        }

    monkeypatch.setattr(document._service, "_query_jedi", delayed_query)
    try:
        QTest.mouseClick(document.suggest_button, Qt.MouseButton.LeftButton)
        wait_for(editor_app, entered.is_set)
        wait_for(editor_app, lambda: document.editor.completer.popup().isVisible(), timeout=1)
        assert not finished.is_set()
        assert "print" in completion_names(document)
        popup = document.editor.completer.popup()
        row = completion_names(document).index("property")
        popup.setCurrentIndex(document.editor.completer.completionModel().index(row, 0))
        release.set()
        wait_for(editor_app, lambda: document._assist_backend == "jedi")
        assert popup.currentIndex().data(Qt.ItemDataRole.UserRole)["name"] == "property"
        QTest.keyClick(document.editor, Qt.Key.Key_Return)
        assert document.editor.toPlainText() == "property"
    finally:
        release.set()
        wait_for(editor_app, finished.is_set)


def test_extensionless_file_can_enable_python_completion_and_help_without_reload(editor_app, documents):
    raw = b'\xef\xbb\xbfimport math\r\nprint("Hello world")\r\n'
    document = documents(name="dwd", raw=raw)
    assert document.language_mode == "auto"
    assert document.language_selector.isVisible()
    assert document.suggest_button.isVisible() and not document.suggest_button.isEnabled()
    assert document.help_button.isVisible() and not document.help_button.isEnabled()
    assert "no extension" in document.assist_label.text()
    assert "Choose Python in Language" in document.assist_label.text()
    assert not document._python

    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    document.editor.insertPlainText("# unsaved\nmath.")
    before = document.editor.toPlainText()
    position = document.editor.textCursor().position()
    changes = []
    document.language_changed.connect(changes.append)
    document.language_selector.setCurrentIndex(document.language_selector.findData("python"))
    assert changes == ["python"]
    assert document.language_mode == "python"
    assert document._python and document.editor.is_python
    assert document.suggest_button.isEnabled() and document.help_button.isEnabled()
    assert document.editor.toPlainText() == before
    assert document.editor.textCursor().position() == position
    assert document.is_modified
    assert document.encoding == "utf-8-sig" and document.newline == "\r\n"
    assert document.path.name == "dwd" and document.path.read_bytes() == raw
    assert token_color(document, 1, 0) == EditorHighlighter.COLORS["keyword"]
    assert token_color(document, 2, 0) == EditorHighlighter.COLORS["builtin"]

    QTest.mouseClick(document.suggest_button, Qt.MouseButton.LeftButton)
    wait_for(editor_app, lambda: document._assist_backend == "jedi"
             and document.editor.completer.popup().isVisible() and "sqrt" in completion_names(document))
    choose_completion(document, "sqrt")
    assert document.editor.toPlainText().endswith("math.sqrt")
    set_cursor(document, 2, 2)
    QTest.keyClick(document.editor, Qt.Key.Key_F1)
    wait_for(editor_app, lambda: "print(" in document.help_text.toPlainText())
    assert document.help_panel.isVisible()
    assert document.save()
    assert document.path.read_bytes() == raw + b"# unsaved\r\nmath.sqrt"
    document.load()
    assert document.language_mode == "python"
    assert document.highlighter.is_python and not document.is_modified


def test_language_mode_preserves_selection_and_undo_and_plain_text_has_no_colours(documents):
    document = documents("value = 42\n")
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    document.editor.insertPlainText("print(value)")
    cursor = document.editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.StartOfLine, QTextCursor.MoveMode.KeepAnchor)
    document.editor.setTextCursor(cursor)
    before = (document.editor.toPlainText(), cursor.position(), cursor.anchor(), document.encoding, document.newline)
    for mode in ("plain", "python", "auto", "plain"):
        document.set_language_mode(mode)
        current = document.editor.textCursor()
        assert (document.editor.toPlainText(), current.position(), current.anchor(), document.encoding, document.newline) == before
        assert document.is_modified
    assert not document._python and document.highlighter.is_plain
    assert token_color(document, 1, 8) is None
    assert token_color(document, 2, 0) is None
    document.editor.undo()
    assert document.editor.toPlainText() == "value = 42\n"
    assert not document.is_modified
    document.set_language_mode("auto")
    assert document._python and document.highlighter.is_python
    assert not document.is_modified


@pytest.mark.parametrize("name", ["sample.py", "dwd"])
def test_reload_keeps_file_encoding_independent_of_language_selection(documents, name):
    # 0x80 differs between Latin-1 and Windows-1252. Changing language must not
    # reinterpret existing text when the same bytes are reloaded.
    raw = b"# coding: latin-1\r\ntext = '\x80'\r\n"
    document = documents(name=name, raw=raw)
    text, encoding = document.editor.toPlainText(), document.encoding
    for mode in ("python", "plain", "auto"):
        document.set_language_mode(mode)
        document.load()
        assert document.language_mode == mode
        assert document.encoding == encoding
        assert document.newline == "\r\n"
        assert document.editor.toPlainText() == text
        assert not document.is_modified
        assert document.save() and document.path.read_bytes() == raw


def test_language_change_discards_pending_assistance_and_reactivates_safely(editor_app, documents, monkeypatch):
    document = documents("pri")
    document.editor.moveCursor(QTextCursor.MoveOperation.End)
    service = document._service
    old_identifier = document._document_id
    original_query = service._query_jedi
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    def delayed_query(request):
        entered.set()
        release.wait(5)
        completed.set()
        return {"completions": [{"name": "STALE_NAME", "kind": "function", "prefix_length": 3}], "signatures": []}

    monkeypatch.setattr(service, "_query_jedi", delayed_query)
    try:
        document.request_completion(True)
        wait_for(editor_app, entered.is_set)
        assert document.editor.completer.popup().isVisible()
        document._show_help("Previous help", "Old documentation")
        document.signature_label.setText("Old signature")
        document.signature_label.show()
        document.editor.set_syntax_diagnostic({"ok": False, "line": 1, "column": 0, "message": "old error"})
        document.set_language_mode("plain")
        assert old_identifier not in service._active
        assert document._document_id == 0
        assert not document.editor.completer.popup().isVisible()
        assert completion_names(document) == []
        assert not document.help_panel.isVisible() and not document.signature_label.isVisible()
        assert document.editor._diagnostic is None and not document.syntax_label.isVisible()
        assert not any(timer.isActive() for timer in (
            document._completion_timer, document._signature_timer, document._syntax_timer,
        ))
        assert "Plain text mode" in document.assist_label.text()
        document.set_language_mode("python")
        assert document._document_id != old_identifier
        assert service._active[document._document_id] == document._version
        monkeypatch.setattr(service, "_query_jedi", original_query)
        release.set()
        wait_for(editor_app, completed.is_set)
        wait_for(editor_app, lambda: "no errors" in document.syntax_label.text())
        assert "STALE_NAME" not in completion_names(document)
        assert not document.editor.completer.popup().isVisible()
        document.request_completion(True)
        wait_for(editor_app, lambda: document._assist_backend == "jedi"
                 and document.editor.completer.popup().isVisible() and "print" in completion_names(document))
    finally:
        release.set()
        wait_for(editor_app, completed.is_set)
