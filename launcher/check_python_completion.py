"""Check or repair Studio's Python helper without opening a user's code.

Run with the application's .venv Python. The default mode only checks. The
Windows repair shortcut passes --repair to update the two editor packages first.
All analysis runs in a child process with a deadline, including Jedi's startup.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


EDITOR_REQUIREMENTS = ("jedi>=0.19.2,<0.21", "Pygments>=2.18,<3")
CHECK_TIMEOUT_SECONDS = 20
INSTALL_TIMEOUT_SECONDS = 180


def _console_python(executable: str) -> str:
    # Preserve the virtual environment path: resolving its executable symlink
    # could accidentally select the base installation instead.
    path = Path(executable).absolute()
    if path.name.lower() == "pythonw.exe":
        console = path.with_name("python.exe")
        if console.is_file():
            path = console
    return str(path)


def _run_command(command: list[str], timeout: float) -> dict:
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **options,
        )
    except subprocess.TimeoutExpired as error:
        captured = error.output or ""
        if isinstance(captured, bytes):
            captured = captured.decode("utf-8", errors="replace")
        return {"ok": False, "output": f"Timed out after {timeout:g} seconds.\n{captured}"}
    except OSError as error:
        return {"ok": False, "output": f"Could not start the application Python: {error}"}
    return {"ok": completed.returncode == 0, "output": completed.stdout.strip()}


def _probe() -> int:
    """Run only inside the deadline-controlled child interpreter."""
    details = {
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "ok": False,
        "checks": [],
    }
    try:
        import jedi
        import parso
        import pygments

        details.update(jedi=jedi.__version__, parso=parso.__version__, pygments=pygments.__version__)
        with tempfile.TemporaryDirectory(prefix="hoyo-completion-check-") as scratch:
            root = Path(scratch)
            jedi.settings.cache_directory = str(root / "jedi-cache")
            project = jedi.Project(
                path=str(root),
                environment_path=_console_python(sys.executable),
                load_unsafe_extensions=False,
            )
            samples = (
                ("Python builtin: pri -> print", "pri", 1, 3, "print"),
                ("Python module: math. -> sqrt", "import math\nmath.", 2, 5, "sqrt"),
            )
            for label, code, line, column, expected in samples:
                script = jedi.Script(code=code, path=str(root / "example.py"), project=project)
                names = {item.name for item in script.complete(line, column)}
                passed = expected in names
                details["checks"].append({"label": label, "ok": passed})
                if not passed:
                    raise RuntimeError(f"The helper did not suggest {expected!r}.")
        details["ok"] = True
    except Exception as error:
        details["error"] = f"{type(error).__name__}: {error}"
    print(json.dumps(details, ensure_ascii=True))
    return 0 if details["ok"] else 1


def check_completion(interpreter: str, timeout: float = CHECK_TIMEOUT_SECONDS) -> dict:
    """Test real Jedi results in this environment; no application/Qt import."""
    command = [
        _console_python(interpreter), "-I", str(Path(__file__).resolve()), "--probe",
    ]
    result = _run_command(command, timeout)
    try:
        details = json.loads(result["output"])
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error": result["output"] or "The Python helper exited without a result."}
    if not isinstance(details, dict):
        return {"ok": False, "error": "The Python helper returned an invalid diagnostic result."}
    details["ok"] = bool(result["ok"] and details.get("ok"))
    return details


def write_report(text: str, application_root: Path) -> Path:
    """Prefer the app folder, with writable per-user and temporary fallbacks."""
    candidates = [application_root / "completion_diagnostic.txt"]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "HoYo Project Studio" / "completion_diagnostic.txt")
    candidates.append(Path(tempfile.gettempdir()) / "HoYo Project Studio" / "completion_diagnostic.txt")
    errors = []
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return path
        except OSError as error:
            errors.append(f"{path}: {error}")
    raise OSError("Could not save the diagnostic report.\n" + "\n".join(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="Update Studio's Jedi and Pygments packages, then check completion.")
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.probe:
        return _probe()

    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    root = Path(__file__).resolve().parent.parent
    interpreter = _console_python(sys.executable)
    report = [
        "HoYo Project Studio - Python completion diagnostic",
        f"Checked: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Application folder: {root}",
        f"Application Python: {interpreter}",
        "",
    ]
    install_ok = True
    if args.repair:
        print("Installing/updating the Python editor packages (up to 3 minutes)...", flush=True)
        installation = _run_command(
            [interpreter, "-I", "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
             "--timeout", "15", "--retries", "1", *EDITOR_REQUIREMENTS],
            INSTALL_TIMEOUT_SECONDS,
        )
        install_ok = installation["ok"]
        report += [f"Package installation: {'PASS' if install_ok else 'FAIL'}", installation["output"], ""]
        print(installation["output"])
        print(f"Package installation: {'PASS' if install_ok else 'FAIL'}", flush=True)

    print(f"Checking real Python completion (up to {CHECK_TIMEOUT_SECONDS} seconds)...", flush=True)
    details = check_completion(interpreter)
    for package in ("python_version", "jedi", "parso", "pygments"):
        if package in details:
            report.append(f"{package}: {details[package]}")
    for check in details.get("checks", []):
        line = f"{'PASS' if check['ok'] else 'FAIL'} - {check['label']}"
        report.append(line)
        print(line)
    if details.get("error"):
        report += ["FAIL - Python completion check", details["error"]]
        print(f"FAIL - {details['error']}")
    passed = bool(install_ok and details.get("ok"))
    outcome = "PASS - Python completion helper is ready." if passed else "FAIL - Python completion repair/check needs attention."
    report += ["", outcome, "", "This check uses temporary example code and does not open or execute your project files.",
               "It checks the application's helper; packages specific to a managed project depend on that project's Python environment."]
    if not passed:
        report += ["", "Run Repair Python Completion.cmd from the application folder after closing Studio.",
                   "If it still fails, share this report with the person helping you fix the editor."]
    try:
        report_path = write_report("\n".join(report) + "\n", root)
    except OSError as error:
        print(f"FAIL - {error}")
        return 1
    print(outcome)
    print(f"Diagnostic report: {report_path}")
    if passed:
        print("Reopen HoYo Project Studio and try typing pri in a .py file.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
