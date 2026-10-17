"""Serialized, cancellable Python assistance for every editor tab.

Only plain Python data crosses the worker boundary.  In particular, Jedi's lazy
objects never leave this thread: its default parser cache is not thread safe.
The service analyzes text and compiles syntax; it never executes the edited code.
"""

from __future__ import annotations

import ast
import builtins
import importlib.util
import inspect
import itertools
import keyword
import re
import sys
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QStandardPaths, Signal


MAX_ASSIST_CHARACTERS = 500_000
SYNTAX_PYTHON_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"

# This small, dependency-free vocabulary is also used on the GUI thread before
# Jedi finishes. Keep it bounded and static: inspecting a user's objects or
# importing their modules could execute their code merely by opening the editor.
_BASIC_SOURCE_LIMIT = 32_768
_BASIC_NAME_LIMIT = 2_000
_NAME = re.compile(r"[^\W\d]\w*", re.UNICODE)
_BUILTINS = {name: value for name, value in vars(builtins).items()
             if not name.startswith("_")}
_MATH_FUNCTIONS = set("""
acos acosh asin asinh atan atan2 atanh cbrt ceil comb copysign cos cosh degrees
dist erf erfc exp exp2 expm1 fabs factorial floor fmod frexp fsum gamma gcd hypot
isclose isfinite isinf isnan isqrt lcm ldexp lgamma log log10 log1p log2 modf
nextafter perm pow prod radians remainder sin sinh sqrt tan tanh trunc ulp
""".split())
_MATH_CONSTANTS = {"e", "inf", "nan", "pi", "tau"}


def console_python_executable(executable: str | None, project_path: str) -> str | None:
    """Use the selected interpreter's console twin for Jedi's pipe protocol.

    A source installation is launched with pythonw.exe on Windows. Preserve its
    virtual environment, but prefer python.exe for child processes that exchange
    data through standard streams. Do not resolve venv symlinks to a base Python.
    """

    if executable is None:
        if getattr(sys, "frozen", False):
            return None
        executable = sys.executable
    path = Path(executable).expanduser()
    if not path.is_absolute():
        path = Path(project_path) / path
    console_name = {"pythonw.exe": "python.exe", "pyw.exe": "py.exe"}.get(path.name.lower())
    if console_name:
        console = path.with_name(console_name)
        if console.is_file():
            path = console
    return str(path)


def _basic_source(code: str) -> str:
    # Keep declarations near the start and recent names near the end of larger
    # files without making every keystroke scan half a million characters.
    if len(code) <= _BASIC_SOURCE_LIMIT:
        return code
    half = _BASIC_SOURCE_LIMIT // 2
    return code[:half].rsplit("\n", 1)[0] + "\n" + code[-half:].partition("\n")[2]


def _math_aliases(source: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for match in re.finditer(r"(?m)^[ \t]*(?:import|from)[ \t]+[^\n]+", source):
        try:
            statements = ast.parse(match.group().strip()).body
        except (SyntaxError, ValueError, RecursionError):
            continue
        for statement in statements:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.name == "math":
                        aliases[alias.asname or "math"] = "math"
            elif isinstance(statement, ast.ImportFrom) and statement.module == "math" and not statement.level:
                for alias in statement.names:
                    if alias.name != "*":
                        aliases[alias.asname or alias.name] = "math." + alias.name
    return aliases


def basic_completion_items(code: str, line: int, column: int) -> list[dict[str, Any]]:
    """Return bounded, immediate suggestions without Jedi, imports, or file IO.

    These are Python keywords/builtins, words in the current buffer, and a small
    static math catalogue. They do not claim to infer third-party package types.
    Both offsets and prefix lengths are Unicode code points, matching Jedi.
    """

    if len(code) > MAX_ASSIST_CHARACTERS:
        return []
    lines = code.split("\n")
    if not 1 <= line <= len(lines):
        return []
    before = lines[line - 1][:max(0, column)]
    match = re.search(r"\w*$", before)
    prefix = match.group() if match else ""
    prefix_start = len(before) - len(prefix)
    source = _basic_source(code)
    candidates: dict[str, str] = {}
    if prefix_start and before[prefix_start - 1] == ".":
        target_match = re.search(r"([\w]+(?:\.[\w]+)*)\.$", before[:prefix_start])
        target = target_match.group(1) if target_match else ""
        if _math_aliases(source).get(target) == "math":
            candidates.update((name, "function") for name in _MATH_FUNCTIONS)
            candidates.update((name, "value") for name in _MATH_CONSTANTS)
        elif target in {"str", "list", "dict", "set", "tuple", "bytes", "int", "float"}:
            # Only trusted builtin types are inspected; never an object from
            # the file or an import resolved from the project directory.
            candidates.update((name, "method") for name in dir(_BUILTINS[target])
                              if not name.startswith("__") or prefix.startswith("__"))
        elif target:
            pattern = re.compile(r"(?<!\w)" + re.escape(target) + r"\.([^\W\d]\w*)")
            for item in itertools.islice(pattern.finditer(source), _BASIC_NAME_LIMIT):
                candidates[item.group(1)] = "name in file"
    elif re.match(r"^[ \t]*(?:import|from)[ \t]+[\w]*$", before):
        candidates.update((name, "module") for name in getattr(sys, "stdlib_module_names", ()))
    else:
        candidates.update((name, "keyword") for name in keyword.kwlist + keyword.softkwlist)
        for name, value in _BUILTINS.items():
            candidates[name] = "class" if isinstance(value, type) else "function" if callable(value) else "value"
        for item in itertools.islice(_NAME.finditer(source), _BASIC_NAME_LIMIT):
            candidates.setdefault(item.group(), "name in file")
        for match in re.finditer(r"\b(def|class)[ \t]+([^\W\d]\w*)", source):
            candidates[match.group(2)] = "function" if match.group(1) == "def" else "class"
    return [
        {"name": name, "kind": candidates[name], "prefix_length": len(prefix)}
        for name in sorted(candidates, key=lambda name: (name.startswith("_"), name.casefold()))
        if name.casefold().startswith(prefix.casefold()) and name != prefix
    ][:1000]


def _basic_help_name(request: AssistRequest) -> str:
    lines = request.code.split("\n")
    if not 1 <= request.line <= len(lines):
        return ""
    text = lines[request.line - 1]
    before, after = text[:request.column], text[request.column:]
    left = re.search(r"[\w.]*$", before)
    right = re.match(r"\w*", after)
    return (left.group() if left else "") + (right.group() if right else "")


def _basic_builtin_signature(name: str, argument: int = 0) -> dict[str, Any] | None:
    value = _BUILTINS.get(name)
    if value is None or not callable(value):
        return None
    try:
        signature = inspect.signature(value)
    except (ValueError, TypeError):
        return None
    return {"text": name + str(signature), "index": argument,
            "parameters": list(signature.parameters)}


def basic_assistance(request: AssistRequest, reason: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "backend": "basic",
        "status": "Basic Python suggestions · rerun Setup for full project assistance",
        "detail": reason,
    }
    if request.action == "complete":
        result["completions"] = basic_completion_items(request.code, request.line, request.column)
    if request.action in {"complete", "signature"}:
        lines = request.code.split("\n")
        before = lines[request.line - 1][:request.column] if 1 <= request.line <= len(lines) else ""
        call = re.search(r"(?<![\w.])([^\W\d]\w*)\(([^()]*)$", before)
        signature = _basic_builtin_signature(call.group(1), call.group(2).count(",")) if call else None
        result["signatures"] = [signature] if signature else []
    if request.action == "help":
        name = _basic_help_name(request)
        value = _BUILTINS.get(name)
        if value is not None:
            signature = _basic_builtin_signature(name)
            result["help"] = [{"name": "builtins." + name,
                               "description": signature["text"] if signature else name,
                               "docstring": (getattr(value, "__doc__", "") or "")[:18_000]}]
        else:
            result["help"] = []
    return result


@dataclass(frozen=True)
class AssistRequest:
    document_id: int
    version: int
    request_id: int
    action: str
    code: str
    path: str
    project_path: str
    python_executable: str | None
    line: int
    column: int
    cursor_position: int


def syntax_check(code: str, path: str) -> dict[str, Any]:
    """Check compile-time syntax without evaluating a module or its imports."""

    try:
        compile(code, path, "exec", dont_inherit=True)
    except SyntaxError as error:
        return {
            "ok": False,
            "message": error.msg,
            "line": error.lineno or 1,
            "column": max(0, (error.offset or 1) - 1),
            "end_line": error.end_lineno or error.lineno or 1,
            "end_column": max(0, (error.end_offset or error.offset or 1) - 1),
        }
    except (ValueError, RecursionError, MemoryError) as error:
        return {"ok": False, "message": str(error), "line": 1, "column": 0,
                "end_line": 1, "end_column": 1}
    return {"ok": True, "message": "No syntax errors"}


class PythonAssistService(QObject):
    finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.jedi_available = importlib.util.find_spec("jedi") is not None
        self._condition = threading.Condition()
        self._requests: dict[tuple[int, str], AssistRequest] = {}
        self._active: dict[int, int] = {}
        self._latest: dict[tuple[int, str], int] = {}
        self._ids = itertools.count(1)
        self._stopping = False
        self._jedi = None
        self._cache_location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        self._projects: dict[tuple[str, str | None], Any] = {}
        self._thread = threading.Thread(
            target=self._run, name="Python editor assistance", daemon=True,
        )
        self._thread.start()

    def register(self) -> int:
        with self._condition:
            identifier = next(self._ids)
            self._active[identifier] = 0
            return identifier

    def set_version(self, identifier: int, version: int) -> None:
        with self._condition:
            if identifier not in self._active:
                return
            self._active[identifier] = version
            for key in list(self._requests):
                if key[0] == identifier:
                    self._requests.pop(key, None)

    def unregister(self, identifier: int) -> None:
        with self._condition:
            self._active.pop(identifier, None)
            for mapping in (self._requests, self._latest):
                for key in list(mapping):
                    if key[0] == identifier:
                        mapping.pop(key, None)

    def submit(self, request: AssistRequest) -> None:
        with self._condition:
            if self._stopping or self._active.get(request.document_id) != request.version:
                return
            key = (request.document_id, request.action)
            self._requests[key] = request
            self._latest[key] = request.request_id
            self._condition.notify()

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            self._requests.clear()
            self._active.clear()
            self._condition.notify_all()
        # A running Jedi request cannot be interrupted safely.  The worker is a
        # daemon and suppresses all replies after shutdown, so closing never waits
        # indefinitely for an interpreter or parser.
        self._thread.join(timeout=0.2)

    def _is_current(self, request: AssistRequest) -> bool:
        return (
            not self._stopping
            and self._active.get(request.document_id) == request.version
            and self._latest.get((request.document_id, request.action)) == request.request_id
        )

    def _run(self) -> None:
        priorities = {"help": 0, "complete": 1, "syntax": 2, "signature": 3}
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopping or bool(self._requests))
                if self._stopping:
                    return
                key = min(self._requests, key=lambda key: (
                    priorities.get(key[1], 4), self._requests[key].request_id,
                ))
                request = self._requests.pop(key)
                if not self._is_current(request):
                    continue
            result = {
                "document_id": request.document_id,
                "version": request.version,
                "request_id": request.request_id,
                "action": request.action,
                "cursor_position": request.cursor_position,
            }
            try:
                if len(request.code) > MAX_ASSIST_CHARACTERS:
                    result["error"] = "Python assistance is paused for files over 500,000 characters."
                elif request.action == "syntax":
                    result["diagnostic"] = syntax_check(request.code, request.path)
                else:
                    result.update(self._query_python(request))
            except Exception as error:
                # A bad/incompatible environment or a parser problem must never
                # prevent opening, editing, or saving a file.
                detail = str(error).strip() or type(error).__name__
                result["error"] = f"Python helper unavailable: {detail[:350]}"
            with self._condition:
                if not self._is_current(request):
                    continue
            try:
                self.finished.emit(result)
            except RuntimeError:
                # The application may have destroyed Qt while a final request
                # was still finishing on this daemon thread.
                return

    def _query_python(self, request: AssistRequest) -> dict[str, Any]:
        """Keep completion available when optional dependencies/envs fail.

        The application interpreter is a useful secondary environment for a
        stale project path. If both fail, return native suggestions with the
        reason visible in the editor, rather than silently dropping the popup.
        """

        if not self.jedi_available:
            return basic_assistance(
                request,
                "Jedi is not installed in Studio's Python environment. Close Studio, run "
                "Setup HoYo Project Studio.cmd, then reopen it.\n"
                f"Studio Python: {sys.executable}\n"
                "Basic suggestions include Python keywords, builtins, names in the current file, "
                "and common math attributes. Third-party package analysis requires Jedi.",
            )
        selected = console_python_executable(request.python_executable, request.project_path)
        try:
            result = self._query_jedi(replace(request, python_executable=selected))
            result.setdefault("backend", "jedi")
            return result
        except Exception as error:
            detail = str(error).strip() or type(error).__name__
            reason = f"Project completion interpreter: {selected or 'automatic'}\n{detail[:350]}"
        fallback = None if getattr(sys, "frozen", False) else console_python_executable(None, request.project_path)
        if fallback and fallback != selected:
            try:
                result = self._query_jedi(replace(request, python_executable=fallback))
            except Exception as error:
                detail = str(error).strip() or type(error).__name__
                reason += f"\nStudio completion interpreter: {fallback}\n{detail[:350]}"
            else:
                result.update({
                    "backend": "jedi",
                    "status": "Python suggestions · using Studio's Python",
                    "detail": reason + "\nThe selected project interpreter could not be used. "
                    "Completion is using Studio's Python; packages installed only in the project "
                    "environment may be unavailable. The project's interpreter setting is unchanged.",
                })
                return result
        return basic_assistance(request, reason + "\nBasic Python suggestions remain available. "
                                "Run Setup HoYo Project Studio.cmd to check the editor dependencies.")

    def _query_jedi(self, request: AssistRequest) -> dict[str, Any]:
        if self._jedi is None:
            import jedi
            self._jedi = jedi
            # Sandboxed/read-only user profiles are common in portable builds.
            # Jedi must have a writable cache, or even builtin completion fails.
            candidates = [Path(self._cache_location) / "python-assist" / "jedi",
                          Path(tempfile.gettempdir()) / "hoyo-project-studio" / "jedi"]
            for cache in candidates:
                try:
                    cache.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryFile(dir=cache):
                        pass
                except OSError:
                    continue
                jedi.settings.cache_directory = str(cache)
                break
        key = (request.project_path, request.python_executable)
        if key not in self._projects:
            if len(self._projects) > 12:
                self._projects.clear()
            self._projects[key] = self._jedi.Project(
                path=request.project_path,
                environment_path=request.python_executable or None,
                load_unsafe_extensions=False,
            )
        script = self._jedi.Script(
            code=request.code,
            path=request.path,
            project=self._projects[key],
        )
        result: dict[str, Any] = {}
        if request.action == "complete":
            completions = script.complete(request.line, request.column)
            result["completions"] = [
                {
                    "name": item.name_with_symbols,
                    "kind": item.type,
                    "prefix_length": item.get_completion_prefix_length(),
                }
                for item in completions[:1000]
            ]
        if request.action in {"complete", "signature"}:
            try:
                result["signatures"] = [
                    {
                        "text": signature.to_string(),
                        "index": signature.index,
                        "parameters": [parameter.name for parameter in signature.params],
                    }
                    for signature in script.get_signatures(request.line, request.column)[:3]
                ]
            except Exception:
                if request.action == "signature":
                    raise
                # Optional argument hints must not discard valid completions.
                result["signatures"] = []
        if request.action == "help":
            names = script.help(request.line, request.column)
            result["help"] = [
                {
                    "name": item.full_name or item.name,
                    "description": item.description,
                    "docstring": item.docstring()[:18_000],
                }
                for item in names[:3]
            ]
        return result


_service: PythonAssistService | None = None


def get_python_assist() -> PythonAssistService:
    """Create on the GUI thread, then share across all document widgets."""

    global _service
    if _service is None:
        _service = PythonAssistService()
    return _service


def shutdown_python_assist() -> None:
    global _service
    if _service is not None:
        _service.shutdown()
        _service = None
