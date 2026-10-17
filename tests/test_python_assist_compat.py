"""Regression coverage for optional engines and GUI interpreter selection."""

import os
import shlex
import sys
from dataclasses import replace

import pytest

from studio.python_assist import (
    AssistRequest,
    MAX_ASSIST_CHARACTERS,
    PythonAssistService,
    basic_assistance,
    basic_completion_items,
    console_python_executable,
)


@pytest.fixture
def service():
    helper = PythonAssistService()
    yield helper
    helper.shutdown()


def request(tmp_path, code="pri", action="complete", executable=None, column=None):
    line = len(code.split("\n"))
    column = len(code.split("\n")[-1]) if column is None else column
    return AssistRequest(
        document_id=1, version=0, request_id=1, action=action,
        code=code, path=str(tmp_path / "example.py"), project_path=str(tmp_path),
        python_executable=executable or sys.executable,
        line=line, column=column, cursor_position=len(code),
    )


def names(result):
    return {item["name"] for item in result}


def test_native_suggestions_include_useful_names_and_skip_unchanged_prefix():
    code = "price_total = 25\npri"
    items = basic_completion_items(code, 2, 3)
    assert {"print", "price_total"} <= names(items)
    assert "pri" not in names(items)
    assert all(item["prefix_length"] == 3 for item in items)
    assert "return" in names(basic_completion_items("ret", 1, 3))


def test_native_suggestions_use_unicode_codepoint_columns():
    code = 'marker = "😀"; prnit'
    position = code.index("prnit") + 2
    items = basic_completion_items(code, 1, position)
    assert "print" in names(items)
    assert next(item for item in items if item["name"] == "print")["prefix_length"] == 2


def test_native_math_aliases_and_imports_need_no_module_execution(tmp_path):
    marker = tmp_path / "executed.txt"
    (tmp_path / "math.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nimport math as maths\nmaths."
    items = basic_completion_items(code, 4, len("maths."))
    assert {"sqrt", "sin", "pi"} <= names(items)
    assert not marker.exists()
    assert "math" in names(basic_completion_items("import ma", 1, 9))
    assert "append" in names(basic_completion_items("list.ap", 1, 7))
    assert "print" not in names(basic_completion_items("unknown.pr", 1, 10))


def test_native_completion_obeys_size_guard():
    assert basic_completion_items("x" * (MAX_ASSIST_CHARACTERS + 1), 1, 3) == []


def test_missing_jedi_still_returns_completion_signatures_and_builtin_docs(service, tmp_path):
    service.jedi_available = False
    result = service._query_python(request(tmp_path))
    assert "print" in names(result["completions"])
    assert result["backend"] == "basic"
    assert "Jedi is not installed" in result["detail"]
    result = service._query_python(request(tmp_path, "print(", action="signature"))
    assert result["signatures"][0]["text"].startswith("print(")
    result = service._query_python(request(tmp_path, "print", action="help", column=3))
    assert result["help"][0]["name"] == "builtins.print"
    assert "Print" in result["help"][0]["docstring"]


def test_missing_project_interpreter_recovers_with_studio_python(service, tmp_path):
    pytest.importorskip("jedi")
    result = service._query_python(request(tmp_path, executable=str(tmp_path / "removed-venv" / "python.exe")))
    assert result["backend"] == "jedi"
    assert "print" in names(result["completions"])
    assert "Studio's Python" in result["status"]
    assert "removed-venv" in result["detail"]


def test_incompatible_parser_returns_basic_suggestions(service, tmp_path, monkeypatch):
    parso = pytest.importorskip("parso")

    def unsupported_grammar(*args, **kwargs):
        raise NotImplementedError("Python version is currently not supported by this parser")

    monkeypatch.setattr(parso, "load_grammar", unsupported_grammar)
    result = service._query_python(request(tmp_path))
    assert result["backend"] == "basic"
    assert "print" in names(result["completions"])
    assert "not supported" in result["detail"]


def test_pythonw_normalization_preserves_virtual_environment_and_relative_paths(tmp_path):
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    console = scripts / "python.exe"
    console.touch()
    assert console_python_executable(str(scripts / "pythonw.exe"), str(tmp_path)) == str(console)
    assert console_python_executable(".venv/Scripts/PYTHONW.EXE", str(tmp_path)) == str(console)
    console.unlink()
    assert console_python_executable(str(scripts / "pythonw.exe"), str(tmp_path)) == str(scripts / "pythonw.exe")


@pytest.mark.skipif(os.name == "nt", reason="uses a shell fixture to model an unusable GUI interpreter")
def test_gui_interpreter_is_replaced_before_starting_real_jedi(service, tmp_path):
    """Model a GUI executable that cannot speak Jedi's child pipe protocol.

    This tests the actual recovery path with real Jedi. It does not pretend a
    Linux surrogate is a Windows desktop launch or that every pythonw fails.
    """

    pytest.importorskip("jedi")
    gui = tmp_path / "pythonw.exe"
    console = tmp_path / "python.exe"
    gui.write_text("#!/bin/sh\nexit 43\n", encoding="utf-8")
    console.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n', encoding="utf-8")
    gui.chmod(0o700)
    console.chmod(0o700)
    original = request(tmp_path, executable=str(gui))
    with pytest.raises(Exception, match="Could not get version information"):
        service._query_jedi(original)
    result = service._query_python(original)
    assert result["backend"] == "jedi"
    assert "print" in names(result["completions"])
    assert (str(tmp_path), str(console)) in service._projects


def test_signature_error_does_not_discard_valid_jedi_completions(service, tmp_path, monkeypatch):
    jedi = pytest.importorskip("jedi")

    def failing_signatures(*args, **kwargs):
        raise RuntimeError("signature fixture failed")

    monkeypatch.setattr(jedi.Script, "get_signatures", failing_signatures)
    result = service._query_python(request(tmp_path))
    assert result["backend"] == "jedi"
    assert "print" in names(result["completions"])
    assert result["signatures"] == []


def test_unknown_basic_help_returns_no_invented_documentation(tmp_path):
    result = basic_assistance(request(tmp_path, "unknown_user_function", action="help"), "test engine unavailable")
    assert result["help"] == []
    assert result["backend"] == "basic"
