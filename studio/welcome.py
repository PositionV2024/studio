from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .project_library import ProjectRecord


class WelcomePage(QWidget):
    """First screen for creating, opening, or returning to a project."""

    create_project_requested = Signal()
    open_project_requested = Signal()
    recent_project_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("WelcomePage")

        title = QLabel("HoYo Project Studio")
        title.setObjectName("WelcomeTitle")

        subtitle = QLabel(
            "Create a visual-editing project, open an existing Streamlit app, "
            "or continue working from your project library."
        )
        subtitle.setObjectName("WelcomeSubtitle")
        subtitle.setWordWrap(True)

        create_button = QPushButton("Create New Project")
        create_button.setObjectName("PrimaryWelcomeButton")
        create_button.clicked.connect(self.create_project_requested.emit)

        open_button = QPushButton("Open Existing Project")
        open_button.clicked.connect(self.open_project_requested.emit)

        button_row = QHBoxLayout()
        button_row.addWidget(create_button)
        button_row.addWidget(open_button)
        button_row.addStretch(1)

        recent_heading = QLabel("Recent projects")
        recent_heading.setObjectName("WelcomeSectionTitle")

        self.recent_list = QListWidget()
        self.recent_list.setObjectName("RecentProjects")
        self.recent_list.itemDoubleClicked.connect(self._open_item)
        self.recent_list.itemActivated.connect(self._open_item)

        hint = QLabel(
            "Tip: double-click a recent project to open its workspace. "
            "The project explorer will show its folders and files on the left."
        )
        hint.setObjectName("WelcomeHint")
        hint.setWordWrap(True)

        card = QFrame()
        card.setObjectName("WelcomeCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(34, 32, 34, 32)
        card_layout.setSpacing(16)
        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(4)
        card_layout.addLayout(button_row)
        card_layout.addSpacing(14)
        card_layout.addWidget(recent_heading)
        card_layout.addWidget(self.recent_list, 1)
        card_layout.addWidget(hint)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(72, 56, 72, 56)
        outer.addStretch(1)
        outer.addWidget(card, 5, Qt.AlignmentFlag.AlignCenter)
        outer.addStretch(1)

    def set_projects(self, projects: Iterable[ProjectRecord]) -> None:
        self.recent_list.clear()
        for project in projects:
            item = QListWidgetItem(project.name)
            item.setData(Qt.ItemDataRole.UserRole, project.id)
            item.setToolTip(str(project.path))
            item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.recent_list.addItem(item)

        if self.recent_list.count() == 0:
            item = QListWidgetItem(
                "No projects yet — create a new project or open an existing folder."
            )
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.recent_list.addItem(item)

    def _open_item(self, item: QListWidgetItem) -> None:
        project_id = item.data(Qt.ItemDataRole.UserRole)
        if project_id:
            self.recent_project_requested.emit(str(project_id))
