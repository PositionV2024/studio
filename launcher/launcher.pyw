"""Console-free launcher for HoYo Project Studio on Windows."""

from __future__ import annotations

import ctypes
import os
import sys
import traceback
from pathlib import Path


def _show_error(title: str, message: str) -> None:
    """Display a native Windows error box without requiring a console."""

    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        # This fallback is useful if the launcher is opened from a terminal.
        print(f"{title}: {message}", file=sys.stderr)


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    os.chdir(project_root)

    # Ensure imports such as ``from studio.application import run`` work even
    # when the launcher is started through a shortcut from another directory.
    root_text = str(project_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    launch_guard = None
    guard_error_type = None
    try:
        # This stdlib-only module has no studio imports. Acquire the shared
        # application lock before reading UI modules so a second launch cannot
        # load a mixture of versions while an update is replacing files.
        from launcher.update_apply import UpdateError, acquire_running_instance

        guard_error_type = UpdateError
        launch_guard = acquire_running_instance(project_root)
        from studio.application import run

        return int(run(launch_guard=launch_guard))
    except Exception as error:
        if guard_error_type is not None and isinstance(error, guard_error_type):
            _show_error("HoYo Project Studio update", str(error))
            return 1
        error_log = project_root / "launcher_error.log"
        details = traceback.format_exc()

        try:
            error_log.write_text(details, encoding="utf-8")
        except OSError:
            pass

        _show_error(
            "HoYo Project Studio could not start",
            "The application encountered an error while starting.\n\n"
            f"Details were written to:\n{error_log}\n\n"
            "Run 'Setup HoYo Project Studio.cmd' again if dependencies are "
            "missing.",
        )
        return 1
    finally:
        if launch_guard is not None:
            launch_guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
