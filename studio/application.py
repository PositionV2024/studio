from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import __version__
from .main_window import MainWindow


def run() -> int:
    QCoreApplication.setOrganizationName("Clarence Studio")
    QCoreApplication.setApplicationName("HoYo Project Studio")
    QCoreApplication.setApplicationVersion(__version__)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    root = Path(__file__).resolve().parent.parent

    icon_path = root / "resources" / "hoyo_project_studio.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow(root)
    if icon_path.exists():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()
    return app.exec()
