"""Selected completion documentation must be accurate, lazy, and serializable."""

import importlib.util
import json
import sys
from dataclasses import replace

import pytest

from studio.python_assist import AssistRequest, PythonAssistService, basic_completion_detail


@pytest.fixture
def detail_service():
    service = PythonAssistService()
    yield service
    service.shutdown()


def detail_request(tmp_path, source, name, kind="function", **changes):
    request = AssistRequest(
        document_id=1, version=0, request_id=1, action="completion_detail",
        code=source, path=str(tmp_path / "sample.py"), project_path=str(tmp_path),
        python_executable=sys.executable, line=source.count("\n") + 1,
        column=len(source.rsplit("\n", 1)[-1]), cursor_position=len(source),
        completion_name=name, completion_kind=kind,
    )
    return replace(request, **changes)


def assert_details(result, name, signature, phrase):
    assert result["completion_name"] == name
    assert result["detail_status"] == "ready"
    assert any(signature in entry["text"] for entry in result["signatures"])
    assert phrase in result["docstring"]
    # No lazy Jedi objects are permitted in the worker's public response.
    json.dumps(result)


def test_selected_builtin_and_math_names_resolve_from_unfinished_prefix(detail_service, tmp_path):
    pytest.importorskip("jedi")
    result = detail_service._query_python(detail_request(tmp_path, "pri", "print"))
    assert_details(result, "print", "print(", "stdout")
    result = detail_service._query_python(detail_request(tmp_path, "import math\nmath.", "sqrt"))
    assert_details(result, "sqrt", "sqrt(", "square root")
    assert result["name"] == "math.sqrt"


def test_docstring_follows_current_unsaved_source_and_does_not_execute_it(detail_service, tmp_path):
    pytest.importorskip("jedi")
    marker = tmp_path / "executed.txt"
    saved = 'def greet(name):\n    """Old saved documentation."""\n    return name\ngre'
    (tmp_path / "sample.py").write_text(saved, encoding="utf-8")
    source = (
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        'def greet(name, punctuation="!"):\n    """Greet a visitor by name."""\n'
        "    return name + punctuation\ngre"
    )
    result = detail_service._query_python(detail_request(tmp_path, source, "greet"))
    assert_details(result, "greet", 'greet(name, punctuation="!")', "Greet a visitor")
    assert "Old saved" not in result["docstring"]
    assert not marker.exists()
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == saved


def test_reexported_bound_method_infers_real_signature_and_docstring(detail_service, tmp_path):
    pytest.importorskip("jedi")
    marker = tmp_path / "import-executed.txt"
    (tmp_path / "chart_api.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "class ChartTools:\n"
        "    def bar_chart(self, data=None, *, x=None):\n"
        '        """Render bars for the supplied data."""\n'
        "        return data\n"
        "_main = ChartTools()\n"
        "bar_chart = _main.bar_chart\n",
        encoding="utf-8",
    )
    request = detail_request(tmp_path, "import chart_api as st\nst.", "bar_chart", "statement")
    result = detail_service._query_python(request)
    assert_details(result, "bar_chart", "bar_chart(data=None, *, x=None)", "Render bars")
    assert result["completion_kind"] == "statement"
    assert "ChartTools.bar_chart" in result["name"]
    assert "self" not in result["signatures"][0]["parameters"]
    assert not marker.exists()


@pytest.mark.skipif(importlib.util.find_spec("streamlit") is None, reason="optional real Streamlit source fixture")
def test_actual_streamlit_bar_chart_selected_docs(detail_service, tmp_path):
    """Static Jedi analysis only: Streamlit need not be importable or running."""

    pytest.importorskip("jedi")
    request = detail_request(tmp_path, "import streamlit as st\nst.", "bar_chart", "statement")
    result = detail_service._query_python(request)
    assert_details(result, "bar_chart", "bar_chart(", "bar chart")
    assert "data" in result["signatures"][0]["parameters"]


def test_docs_are_lazy_and_hover_reuses_only_matching_completion_context(detail_service, tmp_path, monkeypatch):
    jedi = pytest.importorskip("jedi")
    from jedi.api.classes import Completion

    completed = []
    documented = []
    original_complete = jedi.Script.complete
    original_docstring = Completion.docstring

    def complete(script, *args, **kwargs):
        completed.append(True)
        return original_complete(script, *args, **kwargs)

    def docstring(item, *args, **kwargs):
        documented.append(item.name)
        return original_docstring(item, *args, **kwargs)

    monkeypatch.setattr(jedi.Script, "complete", complete)
    monkeypatch.setattr(Completion, "docstring", docstring)
    request = detail_request(tmp_path, "import math\nmath.", "sqrt")
    initial = detail_service._query_python(replace(request, action="complete"))
    assert len(initial["completions"]) > 20
    assert documented == []
    assert len(completed) == 1
    result = detail_service._query_python(request)
    assert_details(result, "sqrt", "sqrt(", "square root")
    assert documented == ["sqrt"]
    assert len(completed) == 1
    result = detail_service._query_python(replace(request, request_id=2, completion_name="sin"))
    assert_details(result, "sin", "sin(", "sine")
    assert documented == ["sqrt", "sin"]
    assert len(completed) == 1
    changed = replace(request, code="import math\nmath.sq", column=7, version=1)
    detail_service._query_python(changed)
    assert len(completed) == 2
    # Reopening completion refreshes dependency information even if the local
    # cursor/code did not change, since another project file may have changed.
    detail_service._query_python(replace(changed, action="complete", request_id=3))
    assert len(completed) == 3


def test_one_optional_detail_failure_preserves_the_other(detail_service, tmp_path, monkeypatch):
    pytest.importorskip("jedi")
    from jedi.api.classes import BaseName

    def failed_signature(*args, **kwargs):
        raise RuntimeError("Signature unavailable for this name")

    monkeypatch.setattr(BaseName, "get_signatures", failed_signature)
    result = detail_service._query_python(detail_request(tmp_path, "pri", "print"))
    assert result["detail_status"] == "ready"
    assert "stdout" in result["docstring"]
    assert result["signatures"] == []
    assert result["backend"] == "jedi"


def test_missing_jedi_selected_builtin_docs_and_unknown_names_are_truthful(detail_service, tmp_path):
    detail_service.jedi_available = False
    result = detail_service._query_python(detail_request(tmp_path, "pri", "print"))
    assert_details(result, "print", "print(", "stdout")
    assert result["backend"] == "basic"
    assert result["name"] == "builtins.print"
    result = detail_service._query_python(detail_request(tmp_path, "unknown_f", "unknown_function"))
    assert result["detail_status"] == "unavailable"
    assert result["docstring"] == ""
    assert result["signatures"] == []


@pytest.mark.parametrize("source", [
    "print = 3\npri",
    "def sample(print):\n    pri",
    "from custom_module import print\npri",
    "from custom_module import pr",
    "custom_object.pri",
])
def test_basic_details_do_not_mislabel_custom_name_as_builtin(tmp_path, source):
    result = basic_completion_detail(detail_request(tmp_path, source, "print"))
    assert result["detail_status"] == "unavailable"
    assert result["docstring"] == ""


def test_completion_identity_can_come_from_immediate_basic_row(detail_service, tmp_path):
    pytest.importorskip("jedi")
    # Before the Jedi reply, a local name has the native row kind. Resolve the
    # right name while echoing the selection's original identity to the UI.
    request = detail_request(
        tmp_path, 'def custom_function(name):\n    """Custom function help."""\n    return name\ncust',
        "custom_function", "name in file",
    )
    result = detail_service._query_python(request)
    assert_details(result, "custom_function", "custom_function(name)", "Custom function help")
    assert result["completion_kind"] == "name in file"


def test_original_positional_assist_requests_keep_working(tmp_path):
    request = AssistRequest(1, 0, 1, "complete", "pri", str(tmp_path / "sample.py"),
                            str(tmp_path), sys.executable, 1, 3, 3)
    assert request.completion_name == ""
    assert request.completion_kind == ""
