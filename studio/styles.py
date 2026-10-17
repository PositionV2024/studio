APP_STYLE = r"""
QMainWindow, QWidget {
    background: #0b1020;
    color: #eef4ff;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
}
QToolBar {
    background: #111a2d;
    border-bottom: 1px solid #263650;
    spacing: 6px;
    padding: 7px;
}
QToolButton, QPushButton {
    background: #18243b;
    border: 1px solid #304769;
    border-radius: 8px;
    padding: 7px 11px;
    color: #f4f7ff;
}
QToolButton:hover, QPushButton:hover {
    background: #223454;
    border-color: #5a86c7;
}
QToolButton:pressed, QPushButton:pressed { background: #101a2c; }
QToolButton:checked { background: #643d87; border-color: #d275ff; }


/* Compact task-oriented toolbar -------------------------------------- */
QToolBar#MainToolbar {
    min-height: 48px;
    padding: 6px 10px;
    spacing: 7px;
    background: #10192b;
    border: 0;
    border-bottom: 1px solid #263650;
}
QToolBar#MainToolbar::separator {
    width: 1px;
    margin: 7px 5px;
    background: #2a3b59;
}
QToolBar#MainToolbar QToolButton {
    min-height: 32px;
    padding: 6px 11px;
    border-radius: 8px;
    font-weight: 650;
}
QToolButton#HomeToolbarButton {
    background: transparent;
    border-color: transparent;
    color: #b9cae7;
}
QToolButton#HomeToolbarButton:hover {
    color: #ffffff;
    background: #192944;
    border-color: #304769;
}
QToolButton#ToolbarMenuButton {
    min-width: 92px;
    background: #162239;
    border-color: #2d4263;
}
QToolButton#ToolbarMenuButton:hover {
    background: #203453;
    border-color: #5a86c7;
}
QToolButton#RunToolbarButton {
    min-width: 92px;
    font-weight: 800;
}
QToolButton#RunToolbarButton[running="false"] {
    color: #edfff6;
    background: #174232;
    border-color: #34775d;
}
QToolButton#RunToolbarButton[running="false"]:hover {
    background: #205844;
    border-color: #55a27f;
}
QToolButton#RunToolbarButton[running="true"] {
    color: #fff1f3;
    background: #552535;
    border-color: #9a4a63;
}
QToolButton#RunToolbarButton[running="true"]:hover {
    background: #6d3044;
    border-color: #c2647f;
}
QToolButton#EditModeToolbarButton {
    min-width: 112px;
    background: #18243b;
    border-color: #304769;
}
QToolButton#EditModeToolbarButton:hover {
    background: #223454;
    border-color: #5a86c7;
}
QToolButton#EditModeToolbarButton:checked {
    color: #fff6ff;
    background: #55306e;
    border-color: #c06fe8;
    box-shadow: 0 0 12px rgba(192, 111, 232, 0.20);
}
QToolButton#PublishToolbarButton {
    min-width: 96px;
    color: #f8f5ff;
    background: #384d88;
    border-color: #6e8ed7;
    font-weight: 800;
}
QToolButton#PublishToolbarButton:hover {
    background: #4862a5;
    border-color: #96b3f2;
}
QToolButton#PublishToolbarButton:disabled,
QToolButton#RunToolbarButton:disabled,
QToolButton#EditModeToolbarButton:disabled {
    color: #687894;
    background: #121b2d;
    border-color: #253550;
}
QMenu::separator {
    height: 1px;
    margin: 5px 7px;
    background: #2b3d5f;
}
QMenu::item:disabled {
    color: #8294b3;
    font-weight: 700;
}
QPushButton#PrimaryWelcomeButton {
    background: #3e6fbe;
    border-color: #78a9ff;
    font-weight: 800;
    padding: 10px 16px;
}
QPushButton#PrimaryWelcomeButton:hover { background: #4d80d1; }
QListWidget, QTreeView, QPlainTextEdit, QLineEdit, QSpinBox, QScrollArea {
    background: #0f1729;
    border: 1px solid #263650;
    border-radius: 8px;
    padding: 6px;
    selection-background-color: #426da8;
}
QListWidget::item, QTreeView::item {
    padding: 8px 7px;
    border-radius: 7px;
}
QListWidget::item:selected, QTreeView::item:selected { background: #223a61; }
QListWidget::item:hover, QTreeView::item:hover { background: #172844; }
QGroupBox {
    border: 1px solid #263650;
    border-radius: 10px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: 700;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QSplitter::handle {
    background: #22314d;
    border-radius: 3px;
}
QSplitter::handle:hover { background: #4f76ad; }
QSplitter::handle:pressed { background: #6d9be0; }
QSplitter::handle:horizontal {
    width: 8px;
    margin: 3px 1px;
}
QSplitter::handle:vertical {
    height: 8px;
    margin: 1px 3px;
}
QWidget#ProjectSidebar { background: #0b1020; }
QLabel#InspectorTitle { font-size: 18px; font-weight: 800; color: #ffffff; }
QLabel#InspectorDetails { color: #93a6c8; }
QLabel#ExplorerPath { color: #b7c9e8; font-weight: 700; }
QStatusBar { background: #111a2d; color: #9eb2d4; }

QToolButton#PanelsButton {
    background: #18243b;
    border: 1px solid #304769;
    border-radius: 8px;
    padding: 7px 11px;
    color: #f4f7ff;
}
QToolButton#PanelsButton:hover {
    background: #223454;
    border-color: #5a86c7;
}
QPushButton#PanelCollapseButton {
    min-width: 28px;
    max-width: 30px;
    padding: 5px 4px;
    color: #aebfdb;
    background: #111b30;
    border-color: #2d4267;
}
QPushButton#PanelCollapseButton:hover {
    color: #ffffff;
    background: #223454;
    border-color: #5a86c7;
}
QMenu {
    background: #111a2d;
    color: #eef4ff;
    border: 1px solid #304769;
    border-radius: 8px;
    padding: 6px;
}
QMenu::item {
    padding: 7px 28px 7px 10px;
    border-radius: 6px;
}
QMenu::item:selected { background: #223a61; }
QMenu::indicator:checked {
    width: 9px;
    height: 9px;
    border-radius: 4px;
    background: #6ea8ff;
}

QWidget#WelcomePage { background: #090f1d; }
QFrame#WelcomeCard {
    min-width: 760px;
    max-width: 980px;
    min-height: 520px;
    background: #111b30;
    border: 1px solid #2d4267;
    border-radius: 22px;
}
QLabel#WelcomeTitle {
    color: #ffffff;
    font-size: 34px;
    font-weight: 900;
}
QLabel#WelcomeSubtitle {
    color: #aebfdb;
    font-size: 15px;
    line-height: 1.5;
}
QLabel#WelcomeSectionTitle {
    color: #eaf2ff;
    font-size: 17px;
    font-weight: 800;
}
QLabel#WelcomeHint { color: #8296b8; }
QListWidget#RecentProjects {
    min-height: 220px;
    background: #0c1425;
}
QListWidget#RecentProjects::item {
    padding: 13px 11px;
    border-bottom: 1px solid #1c2a45;
}
QPlainTextEdit#OutputPanel {
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px;
}

/* Project Explorer: force a consistent dark palette on Windows. */
QTreeView#ProjectTree {
    background-color: #0d1526;
    alternate-background-color: #0d1526;
    color: #dce8fb;
    border: 1px solid #2a3b59;
    border-radius: 8px;
    padding: 4px;
    outline: 0;
    show-decoration-selected: 1;
}
QTreeView#ProjectTree::item {
    min-height: 24px;
    padding: 4px 6px;
    color: #dce8fb;
    background: transparent;
    border: 0;
}
QTreeView#ProjectTree::item:hover {
    color: #ffffff;
    background: #172a47;
}
QTreeView#ProjectTree::item:selected,
QTreeView#ProjectTree::item:selected:active,
QTreeView#ProjectTree::item:selected:!active {
    color: #ffffff;
    background: #294b7a;
}
QTreeView#ProjectTree:disabled {
    color: #667895;
    background: #0a1120;
}

QScrollBar:vertical {
    width: 12px;
    margin: 2px;
    border: none;
    border-radius: 6px;
    background: #0a1120;
}
QScrollBar::handle:vertical {
    min-height: 28px;
    border-radius: 5px;
    background: #344b70;
}
QScrollBar::handle:vertical:hover { background: #5277ad; }
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
    background: transparent;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical { background: transparent; }

QScrollBar:horizontal {
    height: 12px;
    margin: 2px;
    border: none;
    border-radius: 6px;
    background: #0a1120;
}
QScrollBar::handle:horizontal {
    min-width: 28px;
    border-radius: 5px;
    background: #344b70;
}
QScrollBar::handle:horizontal:hover { background: #5277ad; }
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0;
    background: transparent;
}
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal { background: transparent; }
"""

# Bottom Output / Console tab bar --------------------------------------
APP_STYLE += r"""
QTabWidget#BottomPanelTabs {
    background: #0b1020;
    border: 0;
}
QTabWidget#BottomPanelTabs::pane {
    top: -1px;
    border: 1px solid #263650;
    border-radius: 8px;
    background: #0b1324;
}
QTabWidget#BottomPanelTabs QTabBar::tab {
    min-width: 92px;
    min-height: 27px;
    padding: 6px 14px;
    margin-right: 3px;
    color: #91a6c8;
    background: #101a2d;
    border: 1px solid #263650;
    border-bottom: 0;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}
QTabWidget#BottomPanelTabs QTabBar::tab:selected {
    color: #ffffff;
    background: #182844;
    border-color: #446b9f;
}
QTabWidget#BottomPanelTabs QTabBar::tab:hover:!selected {
    color: #dbe8ff;
    background: #15243d;
}

QWidget#TerminalConsole {
    background: #0b1324;
}
QPlainTextEdit#ConsoleOutput {
    color: #d9e7ff;
    background: #070d18;
    border: 1px solid #1f3150;
    border-radius: 7px;
    padding: 8px;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px;
    selection-background-color: #315f9a;
}
QLabel#ConsolePathLabel {
    color: #8fa8ce;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
}
QLabel#ConsolePrompt {
    min-width: 18px;
    color: #6eb4ff;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 15px;
    font-weight: 900;
}
QLineEdit#ConsoleCommandInput {
    min-height: 30px;
    padding: 5px 9px;
    color: #f3f7ff;
    background: #091120;
    border: 1px solid #2d466b;
    border-radius: 7px;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px;
}
QLineEdit#ConsoleCommandInput:focus {
    border-color: #6aa8f5;
    background: #0c1729;
}
QPushButton#ConsoleToolbarButton {
    min-height: 25px;
    padding: 4px 9px;
    color: #bdd0ed;
    background: #101d33;
    border: 1px solid #2b4366;
    border-radius: 6px;
    font-size: 11px;
}
QPushButton#ConsoleToolbarButton:hover {
    color: #ffffff;
    background: #1c3152;
    border-color: #5682bd;
}
"""


# Workspace document tabs and built-in code editor --------------------
APP_STYLE += r"""
QTabWidget#WorkspaceDocumentTabs {
    background: #080f1d;
    border: 0;
}
QTabWidget#WorkspaceDocumentTabs::pane {
    top: -1px;
    border: 1px solid #263650;
    border-radius: 7px;
    background: #08101d;
}
QTabWidget#WorkspaceDocumentTabs QTabBar::tab {
    min-width: 118px;
    min-height: 28px;
    max-width: 240px;
    padding: 7px 13px;
    margin-right: 3px;
    color: #92a7c8;
    background: #101a2d;
    border: 1px solid #263650;
    border-bottom: 0;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
}
QTabWidget#WorkspaceDocumentTabs QTabBar::tab:selected {
    color: #ffffff;
    background: #182844;
    border-color: #4b76ae;
}
QTabWidget#WorkspaceDocumentTabs QTabBar::tab:hover:!selected {
    color: #dce9ff;
    background: #15243c;
}
QTabWidget#WorkspaceDocumentTabs QTabBar::close-button {
    subcontrol-position: right;
    margin-left: 5px;
}

QLabel#CodeEditorPath {
    color: #91a9cc;
    padding: 4px 7px;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
}
QLabel#CodeEditorStatus {
    color: #7188aa;
    padding: 1px 7px 3px;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 10px;
}
QPushButton#CodeEditorActionButton {
    min-height: 26px;
    padding: 4px 10px;
    color: #c7d8f1;
    background: #111e34;
    border: 1px solid #2c4569;
    border-radius: 6px;
}
QPushButton#CodeEditorActionButton:hover {
    color: #ffffff;
    background: #1d3457;
    border-color: #5d8bc7;
}
QPlainTextEdit#CodeTextEditor {
    padding: 0;
    color: #d9e7ff;
    background: #080e19;
    border: 1px solid #223653;
    border-radius: 6px;
    selection-color: #ffffff;
    selection-background-color: #315f9a;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 13px;
}
"""
