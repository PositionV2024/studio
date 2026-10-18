from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import __version__


def run(*, launch_guard=None) -> int:
    QCoreApplication.setOrganizationName("Clarence Studio")
    QCoreApplication.setApplicationName("HoYo Project Studio")
    QCoreApplication.setApplicationVersion(__version__)
    background_setup = "--background-setup" in sys.argv
    app = QApplication([argument for argument in sys.argv if argument != "--background-setup"])
    app.setStyle("Fusion")
    root = Path(__file__).resolve().parent.parent

    icon_path = root / "resources" / "hoyo_project_studio.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    from .update_install import UpdateInstallError, register_running_instance

    try:
        running_instance = launch_guard if launch_guard is not None else register_running_instance(root)
    except UpdateInstallError as error:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(None, "Studio update in progress", str(error))
        return 1
    try:
        # The native source launcher acquires its guard before importing any
        # studio module. Direct entry points also defer the UI imports until
        # after obtaining their running-instance guard.
        from .main_window import MainWindow

        window = MainWindow(root)
        if icon_path.exists():
            window.setWindowIcon(QIcon(str(icon_path)))
        window.show()
        if background_setup and not getattr(sys, "frozen", False):
            QTimer.singleShot(0, window.start_background_setup)
        return app.exec()
    finally:
        if running_instance is not None:
            running_instance.close()
