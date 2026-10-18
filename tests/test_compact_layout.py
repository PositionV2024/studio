"""Exercise relocated application files from a different working directory.

The real Python launcher, application entry point and build specification run
below. Lightweight Qt/build API substitutes isolate path and handoff contracts;
these checks are not a substitute for a Windows or Qt desktop run. Setup/build
subprocess checks execute their real, deliberately refused Linux entry points.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXE_NAME = "HoYo Project Studio.exe"


@pytest.fixture(scope="module")
def compact_content(tmp_path_factory):
    """Use delivered files when run inside app/, or stage the same flat sources.

    An isolated copy allows real helpers to write diagnostics without modifying
    the release being checked. Spaces and Unicode exercise path independence.
    """
    release = tmp_path_factory.mktemp("Studio 文件 & Co") / "HoYo Project Studio"
    release.mkdir()
    source_exe = (ROOT.parent if ROOT.name.casefold() == "app" else ROOT) / EXE_NAME
    assert source_exe.is_file()
    ignore = shutil.ignore_patterns(
        EXE_NAME, "qa", "tests", "__pycache__", ".pytest_cache", ".venv", "build", "dist",
        "*.log", "*.pyc", "*.pyo", ".hoyo_install.lock", "completion_diagnostic.txt",
    )
    shutil.copytree(ROOT, release / "app", ignore=ignore)
    shutil.copy2(source_exe, release / EXE_NAME)
    return release / "app"


BOOTSTRAP = r'''
import json
import os
from pathlib import Path
import runpy
import sys
import types

root = Path(sys.argv[1]).resolve()
request_background = sys.argv[2] == "background"
events = []
scheduled = []
observed = {}

class CoreApplication:
    @staticmethod
    def setOrganizationName(value):
        observed["organization"] = value
    @staticmethod
    def setApplicationName(value):
        observed["application"] = value
    @staticmethod
    def setApplicationVersion(value):
        observed["version"] = value

class Icon:
    def __init__(self, path):
        self.path = path
        observed.setdefault("icons", []).append(path)
        observed["icon_header"] = list(Path(path).read_bytes()[:4])

class Application:
    def __init__(self, arguments):
        observed["qt_arguments"] = arguments
    def setStyle(self, value):
        observed["style"] = value
    def setWindowIcon(self, icon):
        events.append("application_icon")
    def exec(self):
        events.append("event_loop")
        for callback in scheduled:
            callback()
        return 23

class Window:
    def __init__(self, app_root):
        observed["content_root"] = str(app_root)
        observed["window_cwd"] = str(Path.cwd())
        observed["import_root"] = sys.path[0]
        observed["resource_bytes"] = {
            name: len((app_root / name).read_bytes()) for name in (
                "resources/editor.js", "resources/chevron-down.svg",
                "resources/chevron-right.svg", "templates/streamlit_demo/app.py",
                "templates/streamlit_demo/requirements.txt", "studio_runtime.py",
            )
        }
        events.append("window_created")
    def setWindowIcon(self, icon):
        events.append("window_icon")
    def show(self):
        events.append("window_shown")
    def start_background_setup(self):
        events.append("background_started")

class Timer:
    @staticmethod
    def singleShot(delay, callback):
        observed["background_delay"] = delay
        scheduled.append(callback)
        events.append("background_scheduled")

qt = types.ModuleType("PySide6")
qt.__path__ = []
core = types.ModuleType("PySide6.QtCore")
core.QCoreApplication = CoreApplication
core.QTimer = Timer
gui = types.ModuleType("PySide6.QtGui")
gui.QIcon = Icon
widgets = types.ModuleType("PySide6.QtWidgets")
widgets.QApplication = Application
window_module = types.ModuleType("studio.main_window")
window_module.MainWindow = Window
sys.modules.update({
    "PySide6": qt, "PySide6.QtCore": core, "PySide6.QtGui": gui,
    "PySide6.QtWidgets": widgets, "studio.main_window": window_module,
})
launcher = root / "launcher/launcher.pyw"
sys.argv = [str(launcher), "--background-setup"] if request_background else [str(launcher)]
sys.argv.extend(["--example-user-argument", "value with spaces"])
observed["initial_cwd"] = str(Path.cwd())
try:
    runpy.run_path(str(launcher), run_name="__main__")
except SystemExit as result:
    observed["exit_code"] = result.code
observed["application_module"] = sys.modules["studio.application"].__file__
observed["events"] = events
print(json.dumps(observed))
'''


@pytest.mark.parametrize("background", [False, True])
def test_packaged_python_entry_point_loads_app_and_preserves_background_handoff(
    compact_content, tmp_path, background,
):
    unrelated = tmp_path / "Unrelated 当前 folder"
    unrelated.mkdir()
    # A package in the caller's directory must not shadow Studio's own files.
    (unrelated / "studio").mkdir()
    (unrelated / "studio/__init__.py").write_text(
        "raise RuntimeError('Loaded the wrong application directory')\n", encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", BOOTSTRAP, str(compact_content),
         "background" if background else "normal"],
        cwd=unrelated, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["exit_code"] == 23
    assert result["initial_cwd"] == str(unrelated)
    assert result["content_root"] == result["window_cwd"] == result["import_root"] == str(compact_content)
    assert Path(result["application_module"]) == compact_content / "studio/application.py"
    assert result["organization"] == "Clarence Studio"
    assert result["application"] == "HoYo Project Studio"
    assert result["icon_header"] == [0, 0, 1, 0]
    assert result["icons"] and set(result["icons"]) == {str(compact_content / "resources/hoyo_project_studio.ico")}
    assert all(result["resource_bytes"].values())
    assert result["qt_arguments"] == [
        str(compact_content / "launcher/launcher.pyw"), "--example-user-argument", "value with spaces",
    ]
    events = result["events"]
    assert events.index("window_shown") < events.index("event_loop")
    if background:
        assert result["background_delay"] == 0
        assert events.index("window_shown") < events.index("background_scheduled") < events.index("event_loop")
        assert events.index("event_loop") < events.index("background_started")
    else:
        assert "background_scheduled" not in events and "background_started" not in events
    assert {path.name for path in compact_content.parent.iterdir()} == {EXE_NAME, "app"}


@pytest.mark.skipif(os.name == "nt", reason="This check exercises the deliberate Linux platform refusal.")
@pytest.mark.parametrize("script,extra,log_name,error_text", [
    ("startup.py", ["--phase", "core"], "setup_hoyo_studio.log", "Use the Windows launcher"),
    ("windows_build.py", [], "build_windows_exe.log", "A Windows EXE must be built on Windows"),
])
def test_packaged_setup_and_build_helpers_keep_their_logs_inside_app(
    compact_content, tmp_path, script, extra, log_name, error_text,
):
    completed = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", str(compact_content / "launcher" / script),
         "--project-root", str(compact_content), *extra],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 1
    assert error_text in completed.stdout
    log = compact_content / log_name
    assert log.is_file() and error_text in log.read_text(encoding="utf-8")
    assert not (tmp_path / log_name).exists()
    assert not (compact_content.parent / log_name).exists()
    assert not (compact_content / ".venv").exists()
    assert {path.name for path in compact_content.parent.iterdir()} == {EXE_NAME, "app"}


def test_packaged_pyinstaller_spec_resolves_its_inputs_from_app(compact_content, tmp_path, monkeypatch):
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    collections = []

    def collect_all(package):
        collections.append(package)
        return [], [], []

    hooks.collect_all = collect_all
    monkeypatch.setitem(sys.modules, "PyInstaller", types.ModuleType("PyInstaller"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", types.ModuleType("PyInstaller.utils"))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    observed = {}

    def analysis(scripts, **options):
        observed["scripts"] = scripts
        observed["analysis"] = options
        return types.SimpleNamespace(pure=[], scripts=scripts, binaries=[], datas=options["datas"])

    def executable(*arguments, **options):
        observed["exe"] = options
        return object()

    monkeypatch.chdir(tmp_path)
    runpy.run_path(str(compact_content / "HoYoProjectStudio.spec"), init_globals={
        "SPECPATH": str(compact_content), "Analysis": analysis, "PYZ": lambda *args: object(),
        "EXE": executable, "COLLECT": lambda *args, **kwargs: object(),
    })
    assert observed["scripts"] == [str(compact_content / "main.py")]
    assert observed["analysis"]["pathex"] == [str(compact_content)]
    sources = [Path(source) for source, _ in observed["analysis"]["datas"]]
    assert {source.name for source in sources} == {"resources", "templates", "studio_runtime.py", "README.md"}
    assert all(source.parent == compact_content and source.exists() for source in sources)
    assert Path(observed["exe"]["icon"]).is_file()
    assert Path(observed["exe"]["icon"]) == compact_content / "resources/hoyo_project_studio.ico"
    assert observed["exe"]["console"] is False
    assert collections == ["jedi", "parso", "pygments"]


def test_packaged_native_rebuild_defaults_to_the_outer_entry_point(compact_content, tmp_path, monkeypatch):
    script = compact_content / "launcher/native/build_launcher.py"
    spec = importlib.util.spec_from_file_location("compact_native_rebuild", script)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    monkeypatch.chdir(tmp_path)
    spec.loader.exec_module(helper)
    assert helper.PROJECT == compact_content
    assert helper.default_output_path(helper.PROJECT) == compact_content.parent / EXE_NAME
    assert helper.default_output_path(tmp_path / "flat source") == tmp_path / "flat source" / EXE_NAME

