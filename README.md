# HoYo Project Studio

HoYo Project Studio is a PySide6 desktop workspace for managing Streamlit projects, running them without a terminal, previewing them in an embedded browser, and visually changing page elements while the website is running.

## Python file recognition — version 0.4.2

The editor now shows a **Language** selector. For an existing file without an extension, such as `dwd`, choose **Python** to enable completion, documentation, syntax checks, and Python colouring. Studio remembers that file's language choice across reopening. Switching the language keeps your unsaved text and file name.

**New File** defaults to **Python (.py)** and previews the final filename. Entering `dwd` creates `dwd.py`; an explicit extension such as `.json` or `.txt` is preserved. You can also select **Plain text (.txt)** or **Other (keep name)**.

Apply the replacement package to the application folder, reopen the launcher from that folder, and check for **0.4.2** in the window title. This update adds no dependencies. See [EDITOR_UPGRADE.md](EDITOR_UPGRADE.md) for the steps to enable Python on an existing file.

## Python completion reliability

Basic Python suggestions now appear immediately, even if Jedi is missing or the project's interpreter is slow or unavailable. The advanced helper refines the suggestions when it is ready. The editor reports when it is using basic assistance or Studio's Python, and keeps the selected suggestion stable while results update.

If advanced assistance reports a missing dependency, run **Repair Python Completion.cmd** from the application folder after closing Studio. This repairs the editor packages in Studio's own `.venv`, tests real completion, and writes a diagnostic report. Fresh installations should use **Setup HoYo Project Studio.cmd**, which also verifies completion.

## File controls and Python editor

Version **0.4.0** adds **New File**, **New Folder**, **Add Files**, and **Delete** to Project Explorer. The editor now includes Python completion, function signatures, documentation help, and syntax checking, alongside improved source highlighting and indentation controls.

See [EDITOR_UPGRADE.md](EDITOR_UPGRADE.md) for installation, completion checks, and usage.

## Panel and Explorer controls

Project Explorer folders now have chevrons beside them. Select the chevron to expand or collapse a folder without opening its files. The Explorer context menu also provides **Expand all folders** and **Collapse all folders**.

The top **Panels** menu is the safe dock for auxiliary panes. Project Library, Project Explorer, Output, and Inspector can be collapsed independently and restored from that menu at any time. **Focus Preview** hides all auxiliary panes without removing them, and **Show All Panels** restores them. Panel visibility and splitter sizes are saved between launches.


## Current features

- Welcome page for creating a new project, opening an existing project, or reopening a recent project.
- Persistent project library.
- Project Explorer showing the selected project's folders and files, with creation, import, and recycling controls.
- Click supported source and configuration files to open them in the built-in tabbed code editor.
- Compact task-oriented toolbar with Project, Run, Edit, Publish, and View controls.
- Secondary actions such as dependency installation, restart, and preview refresh are grouped inside menus.
- Embedded Streamlit preview in a closable tab. Closing the tab does not stop the running Streamlit process.
- Resilient preview loading that waits for the actual Streamlit HTML and retries blank or early page loads.
- Built-in tabbed code editor for Python, JSON, HTML, CSS, JavaScript, Markdown, TOML, YAML, shell scripts, and other text files.
- Python completion using the unsaved file and its project context, function signatures, documentation help, and background syntax checks.
- Smooth project selection: changing the selected project no longer blocks the interface or terminates Streamlit synchronously.
- Asynchronous project switching: pressing **Switch and Run** gracefully closes the old preview and starts the new one.
- Integrated bottom panel with separate **Output** and **Console** tabs.
- Interactive command-line console with project-aware working directories, command history, process listing, and shell restart controls.
- Right-click the output panel and select **Clear Output**.
- Edit Mode that can select any visible HTML element, including elements inside Streamlit component frames.
- Hover outlines and click selection.
- Live editing of:
  - text
  - image source
  - font size
  - width and height
  - padding and margin
  - text, background, and border colours
  - border width and radius
  - opacity
  - visibility
- Draft settings saved to `.studio/draft.json`.
- Undo, redo, and publish to `.studio/published.json`.

## Toolbar organization

The top toolbar keeps only the actions used most often:

```text
Home | Project ▾ | Run/Stop ▾ | Edit Mode ▾ | Publish | View ▾
```

- **Project** contains project management, New File, New Folder, Add Files, Delete Selected File / Folder, and editor save/close commands.
- **Run/Stop** adapts to the current state. Its menu also contains Restart Project, Refresh Preview, and Install Dependencies.
- **Edit Mode** directly enables visual selection. Its menu contains Undo and Redo.
- **Publish** remains a direct action because it is the final step in the visual-editing workflow.
- **View** contains panel visibility, Focus Preview, Show All Panels, and Reset Workspace Layout.
- **Clear Output** is intentionally available only from the Output panel's right-click menu instead of occupying permanent toolbar space.

## Installation

On Windows, double-click **Setup HoYo Project Studio.cmd**, then launch with **Launch HoYo Project Studio.vbs** or your desktop shortcut.

For manual setup in PowerShell, create a virtual environment:

```powershell
py -m venv .venv
```

Install the desktop dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the desktop application:

```powershell
.\.venv\Scripts\python.exe main.py
```

## Getting started

1. Open HoYo Project Studio.
2. On the welcome page, select **Create New Project** or **Open Existing Project**.
3. Select the project in the left-side Project Library.
4. Browse its files in Project Explorer.
5. Select **Run**.
6. Wait for the website to appear in the **Preview** tab. The tab can be closed without stopping Streamlit and restored through **Run → Open Running Preview**.
7. Use the **Output** tab for Streamlit logs or the **Console** tab for commands such as `git status`, `python --version`, `pip list`, and `tasklist`.
8. Turn on **Edit Mode**.
9. Point at any visible element and click it.
10. Change its properties in the inspector on the right.
11. Use **Undo**, **Redo**, or **Publish** as needed.

## Project Explorer

The Project Explorer is rooted at the selected project's folder. It includes normal and hidden project files.

- Double-click a folder to expand or collapse it.
- Click a supported text file to open it in the built-in code editor.
- Unsupported or binary files can still be opened with the system application from the right-click menu.
- Use **New File**, **New Folder**, or **Add Files…** above the tree. The **Add to:** line shows the destination. Selecting a file uses its parent folder.
- Use **Delete** or the tree's Delete key to move the selected file/folder to the Recycle Bin / Trash after confirmation. Unsaved changes are handled before removal; a failed move keeps the open editors intact.
- Right-click a file or folder for the same file commands, or to open it, reveal its location, copy its path, and refresh the tree.
- Adding files copies the originals and reports any existing names instead of overwriting them.


## Smooth project switching

Selecting a different item in Project Library now changes the Explorer and workspace immediately without blocking the Qt interface. A running Streamlit preview remains alive until you choose **Stop** or press **Switch and Run** for another project.

When switching previews, Project Studio uses an asynchronous process lifecycle:

```text
Select another project
→ current preview remains alive
→ press Switch and Run
→ old server receives a graceful stop request
→ new server starts when the old process exits
→ preview changes automatically when the new port is ready
```

An intentional Stop, Restart, application shutdown, or project switch is not reported as a crash. Genuine unexpected exits still appear in Output with their exit code.

## Built-in code editor

The centre workspace is a closable document-tab area. It can contain the live **Preview** and multiple source-code files at the same time. Supported text files open directly inside Project Studio with line numbers, syntax highlighting, current-line highlighting, Save, Reload, unsaved-change prompts, and keyboard shortcuts.

Python files additionally provide code completion, function signatures, documentation, and syntax checking. Each tab retains its own project and interpreter context when you browse another project. Undo and Redo act on the active editor's text. See [EDITOR_UPGRADE.md](EDITOR_UPGRADE.md) for detailed controls and troubleshooting.

Save code changes before running or restarting the project; those actions execute saved files on disk.

```text
Ctrl + S       Save current file
Ctrl + Alt + S Save all open files
Ctrl + W       Close current tab
```

Closing the Preview tab only closes the embedded browser surface. It does not terminate the Streamlit process. Restore it with **Run → Open Running Preview**.

## Integrated command-line console

The bottom workspace contains two tabs:

```text
Output   → Streamlit and Project Studio process logs
Console  → interactive system command line
```

The Console automatically changes to the selected project's folder. It supports everyday commands such as:

```text
dir
git status
python --version
pip list
tasklist
```

Use the **Processes** button to list operating-system processes. The Up and Down arrow keys move through command history. Right-click the Console to clear it, list processes, or restart its shell.

## Visual editing

Elements with an explicit ID remain the most reliable:

```html
<div data-editor-id="profile.uid-card" data-editor-type="container">
```

However, Edit Mode can also select ordinary elements that do not have `data-editor-id`. The editor generates a CSS-based selector and saves the patch under that selector.

Streamlit may rebuild its HTML after a rerun. HoYo Project Studio watches for those changes and reapplies the current draft automatically. Explicit `data-editor-id` values are still recommended for production projects because they are more stable than generated selectors.

## Draft and published settings

Each managed project uses:

```text
.studio/
├── project.json
├── defaults.json
├── draft.json
└── published.json
```

- `project.json` tells the editor how to run the project.
- `draft.json` contains current visual changes.
- `published.json` contains approved changes.
- `defaults.json` contains the original design values.

Changes are always visible inside HoYo Project Studio because the editor injects them into the preview. For a deployed website to use the same settings, the project should read `.studio/published.json` or use the included `studio_runtime.py` helper.

## Project structure

```text
hoyo-project-studio/
├── main.py
├── requirements.txt
├── studio_runtime.py
├── studio/
│   ├── application.py
│   ├── main_window.py
│   ├── welcome.py
│   ├── project_library.py
│   ├── project_explorer.py
│   ├── project_manifest.py
│   ├── project_runner.py
│   ├── code_editor.py
│   ├── terminal_console.py
│   ├── preview.py
│   ├── bridge.py
│   ├── inspector.py
│   ├── import_dialog.py
│   ├── new_project_dialog.py
│   ├── settings_store.py
│   └── styles.py
├── resources/
│   └── editor.js
└── templates/
    └── streamlit_demo/
```

## Next milestones

- Responsive desktop, tablet, and mobile preview sizes.
- Asset library.
- Drag-and-drop section reordering.
- Git status, commit, branch, pull, and push controls.
- Project templates and plugin support.
- More layout controls such as flex, grid, alignment, and positioning.

## Adjustable workspace panels

The Project Library, Project Explorer, live preview, Output/Console panel, and property inspector are separated by visible draggable dividers. Drag a divider to change the width or height of a pane. Project Studio remembers the layout between launches. Use **View → Reset Workspace Layout** to return to the default sizes.

The Project Explorer uses a consistent dark theme on Windows, including rows, selections, and scrollbars.

## Launching without a terminal

HoYo Project Studio includes Windows launcher files in the project root.

### First-time setup

Double-click:

```text
Setup HoYo Project Studio.cmd
```

It creates an isolated `.venv`, installs the required packages, and creates a **HoYo Project Studio** shortcut on the Windows desktop.

### Normal launch

After setup, double-click either:

```text
Launch HoYo Project Studio.vbs
```

for a console-free launch, or:

```text
Launch HoYo Project Studio.cmd
```

for a normal launcher. The desktop shortcut created during setup uses the console-free launcher.

### Build a Windows executable

To build a distributable Windows application, double-click:

```text
Build Windows EXE.cmd
```

The build must be performed on Windows. The result is created at:

```text
dist\HoYo Project Studio\HoYo Project Studio.exe
```

The build uses an **onedir** package because Qt WebEngine contains several required runtime files. Distribute the complete `HoYo Project Studio` folder inside `dist`, not only the `.exe` file.
