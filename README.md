# HoYo Project Studio

HoYo Project Studio is a PySide6 desktop workspace for managing Streamlit projects, running them without a terminal, previewing them in an embedded browser, and visually changing page elements while the website is running.

## Simple application updates — version 0.4.15

Open **Update** at the far right of the top toolbar, then select **Check for updates**. The application repository is fixed to [PositionV2024/studio](https://github.com/PositionV2024/studio) and displayed as text. Download update stays greyed out until a newer downloadable release is confirmed. There is no startup-check setting or automatic startup request. Public releases need no sign-in; private releases can use the built-in GitHub connection.

Select **Download update** when it becomes available, then choose **Restart to update** when ready. Progress appears while you continue working. The normal unsaved-change prompts remain in place. Updates preserve your projects, settings and existing Python environment.

Releases can now contain **just the complete application ZIP**. The earlier separate metadata-file requirement has been removed from the normal workflow. Attach the supplied complete ZIP to the matching stable GitHub release, or use **app/Build Update Package.cmd** after making your own application changes. See [UPDATING.md](UPDATING.md) for release instructions and compatibility with the earlier updater. Install this ZIP into a new empty folder once to get the simplified checker.

## Simple application folder

The extracted `hoyo_project_studio` folder now contains just **two items**:

| Item | What to do |
| --- | --- |
| **HoYo Project Studio.exe** | Double-click to open Studio. Setup starts automatically if required. |
| **app** | Keep this folder beside the executable. It contains the complete application and supporting files. |

Download the **complete application ZIP** for this update and extract it into a **new, empty folder**. This creates the clean layout without moving or deleting your existing installation. The older replacement ZIPs use the previous layout and are not the update method for this release.

Your existing project list, workspace settings and remembered GitHub connection use their existing locations. Projects are still opened from their saved folder paths. Keep your old application folder in place until the new copy is working, especially if you created projects inside it. Setup creates a new local environment inside `app` when needed, using Python already installed on your PC; it never downloads another Python interpreter. Preparing that new environment may download its required packages again.

A small setup window prepares the editor essentials first. Studio then opens while remaining Preview support installs in the background. The setup window closes after handing off to Studio. On later launches, suitable packages in this copy's environment are reused.

The standard Studio desktop shortcut is refreshed to open the new executable after setup succeeds. Custom shortcuts are preserved. Code, resources, templates, documentation, tests, repair scripts and optional build tools are all retained inside `app`.

## Built-in GitHub connection — version 0.4.10

Open **Publish → Publish to GitHub…**, then choose **Sign in with GitHub…**. Studio opens a connection form with a link to create a GitHub access token in your browser. For an existing repository, select that repository and allow **Contents: Read and write**. Paste the token into the masked field and click **Connect GitHub**. On Windows, **Remember on this computer** stores the connection in Windows Credential Manager; otherwise it lasts for the current Studio session.

Review your chosen files and publish directly from Studio. Git and GitHub CLI are no longer required, and there are no extra sign-in applications to install. The built-in publisher preserves unselected repository files, reads your project’s ignore rules itself, and sends updates to a new branch for review. Existing local Git configuration and staged files remain untouched. See [PUBLISHING.md](PUBLISHING.md) for token permissions, new repositories, and retry behavior.

## Open the editor while setup finishes

Double-click **HoYo Project Studio.exe**, including on a fresh installation. A small Windows setup window reuses your installed Python and prepares the interface, code completion and highlighting first. Studio then opens automatically so you can browse, edit and save files while the remaining Preview support installs in the background.

A banner inside Studio shows **Preparing Preview support. You can continue editing files.** Choose **Details** to see progress. Run and Test Script for Studio's shared Python environment become available after setup confirms success. Projects with their own independent Python environments remain usable. If a download fails, editing stays available and **Retry** appears in the banner.

The first launch still needs to finish installing the interface and editor essentials before the full window can appear. Suitable installed packages are reused. Neither setup nor building the EXE downloads another Python interpreter. Closing Studio retains its Save/Discard/Cancel prompts and stops background setup safely after you approve closing.

## Windows build improvements

**app/Build Windows EXE.cmd** reuses installed Python and closes its command window automatically after a successful build. It first reuses the application's working `.venv`. If that environment is absent, it finds an existing 64-bit Python 3.11 or newer and creates the environment from that installation. It does not download another Python interpreter or run the interactive Setup workflow.

Already suitable application and build packages are reused. A first build, or a missing/incompatible package, may still require package downloads. When finished, File Explorer selects the built EXE. A failed build keeps its error window open and writes `app/build_windows_exe.log`.

## Direct text editing, Properties and publishing

Turn on **Edit Mode**, click an editable heading, paragraph or label in the Preview, and type directly over it. **Enter** or clicking elsewhere finishes the edit; **Escape** restores the text from before that edit. A finished typing session is one visual Undo step. Text and the unsaved status update in **Properties** while you type.

The right panel is now named **Properties**. It shows only settings supported by the selected element and hides empty groups. A heading shows its text and applicable appearance settings; an image shows its source and applicable image/layout settings. Live form inputs retain their normal behavior with Edit Mode off. The fixed **Save** footer stays visible while the fields scroll.

Click **Save** to persist the selected project's visual changes, including the last character still in the Preview editor. **Ctrl+S** also saves UI changes while Preview is active. Switching projects, closing Preview, or stopping the server retains pending drafts in Studio. Closing Studio with unsaved UI edits asks **Save, Discard, or Cancel**, with Save All for multiple projects.

Open the **Publish** menu for **GitHub**, **Vercel**, or **Streamlit Community Cloud**. GitHub publication lets you select files and review the exact changes before uploading. Vercel can prepare reviewed Streamlit container files and open its Git import flow; existing supported frontend/static folders can also be selected. See [PUBLISHING.md](PUBLISHING.md) for the complete workflow and hosting limits.

Extract the complete application into a new folder, open **HoYo Project Studio.exe**, and check for **0.4.15** in the title. The editor changes add no Python dependencies. GitHub connection, publishing and updating are built in and add no Python package dependencies. See [EDITOR_UPGRADE.md](EDITOR_UPGRADE.md) for installation and a quick check.

## Reliable preview loading and automatic closure

The embedded preview now reads the browser's page status reliably and waits for Streamlit to render. The previous check could mistake an already rendered page for a blank one and repeatedly reload it. Slow startup now keeps the same page open while it finishes loading. **Refresh Preview** reconnects to the project's actual URL, including after a loading error.

The **Preview** tab closes automatically when its Streamlit process stops or exits. **Run** opens it again, and Restart or Switch and Run opens the next project's preview. Closing the tab yourself keeps the server running; use **Run → Open Running Preview** to restore it. Open code files and the Script Test panel keep their contents when the preview closes.

The preview's Edit Mode helper now loads directly from Qt's bundled resources, so selecting an element reaches the property inspector reliably.

## Test the selected Python script

The editor now has a **Test Script** button. Select a Python file in Project Explorer to open it, then click Test Script or press **F7**. Studio saves changes in that file and runs it using its project folder and Python interpreter. The new **Script Test** tab shows output, Python errors, and the exit result.

Use **Stop** in the Script Test tab or **Shift+F7** to stop a running test. If the script asks for input, enter a reply in the input box and press Enter or **Send**; **End Input** closes the input stream. Script tests run independently of the Streamlit preview. Use the existing **Run** control to launch the full Streamlit app.

## Completion highlighting and inline help

Moving the mouse over a completion now selects and visibly highlights that row. The same popup shows the selected item's function signature and documentation beside the suggestion list. Up and Down update both the selection and its details; click a row or press Tab or Enter to insert it.

The separate **Suggest** and **Python Help** buttons have been removed. Suggestions open automatically while typing, and **Ctrl+Space** opens them on demand. **F1** or **Ctrl+Q** displays documentation in the same popup. **Esc** closes it. Documentation loads only for the selected item, including inferred Streamlit functions such as `st.bar_chart`.

## Python file recognition

The editor now shows a **Language** selector. For an existing file without an extension, such as `dwd`, choose **Python** to enable completion, documentation, syntax checks, and Python colouring. Studio remembers that file's language choice across reopening. Switching the language keeps your unsaved text and file name.

**New File** defaults to **Python (.py)** and previews the final filename. Entering `dwd` creates `dwd.py`; an explicit extension such as `.json` or `.txt` is preserved. You can also select **Plain text (.txt)** or **Other (keep name)**.

## Python completion reliability

Basic Python suggestions now appear immediately, even if Jedi is missing or the project's interpreter is slow or unavailable. The advanced helper refines the suggestions when it is ready. The editor reports when it is using basic assistance or Studio's Python, and keeps the selected suggestion stable while results update.

If advanced assistance reports a missing dependency, run **app/Repair Python Completion.cmd** from the application folder after closing Studio. This repairs the editor packages in Studio's own `.venv`, tests real completion, and writes a diagnostic report. For a fresh installation, open **HoYo Project Studio.exe**; its setup flow also verifies completion.

## File controls and Python editor

Version **0.4.0** adds **New File**, **New Folder**, **Add Files**, and **Delete** to Project Explorer. The editor now includes Python completion, function signatures, documentation help, and syntax checking, alongside improved source highlighting and indentation controls.

See [EDITOR_UPGRADE.md](EDITOR_UPGRADE.md) for installation, completion checks, and usage.

## Panel and Explorer controls

Project Explorer folders now have chevrons beside them. Select the chevron to expand or collapse a folder without opening its files. The Explorer context menu also provides **Expand all folders** and **Collapse all folders**.

The top **Panels** menu controls auxiliary panes. Project Library, Project Explorer, Output, and Properties can be collapsed independently and restored from that menu at any time. **Focus Preview** hides all auxiliary panes, and **Show All Panels** restores them. Panel visibility and splitter sizes are saved between launches.


## Current features

- Welcome page for creating a new project, opening an existing project, or reopening a recent project.
- Persistent project library.
- Project Explorer showing the selected project's folders and files, with creation, import, and recycling controls.
- Click supported source and configuration files to open them in the built-in tabbed code editor.
- Compact task-oriented toolbar with Project, Run, Edit, Publish, and View controls.
- Secondary actions such as dependency installation, restart, and preview refresh are grouped inside menus.
- Embedded Streamlit preview in a closable tab that closes automatically when its server exits. Closing the tab yourself keeps the running Streamlit process available.
- Preview loading checks rendered Streamlit content, waits through slow startup, and offers a working refresh after a loading error.
- Built-in tabbed code editor for Python, JSON, HTML, CSS, JavaScript, Markdown, TOML, YAML, shell scripts, and other text files.
- Python completion using the unsaved file and its project context, visible hover selection, function signatures and documentation within the completion popup, and background syntax checks.
- Test the active Python script with one button, streamed output, tracebacks, input, exit results, and a separate stop control.
- Smooth project selection: changing the selected project no longer blocks the interface or terminates Streamlit synchronously.
- Asynchronous project switching: pressing **Switch and Run** gracefully closes the old preview and starts the new one.
- Integrated bottom panel with separate **Output**, **Console**, and **Script Test** tabs.
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
- Click editable text in Preview and type in place, with Enter to finish, Escape to cancel and one-step visual Undo.
- Explicit Save in Properties, a visible unsaved status, and Save / Discard / Cancel on exit.
- Properties shows only fields that can be edited for the selected element.
- Per-project working drafts saved to `.studio/draft.json` when requested.
- Undo, redo, and publish to `.studio/published.json`.
- Reviewed GitHub publication, Vercel deployment preparation/import, and Streamlit Community Cloud handoff.

## Toolbar organization

The top toolbar keeps only the actions used most often:

```text
Home | Project ▾ | Run/Stop ▾ | Edit Mode ▾ | Publish ▾ | View ▾
```

- **Project** contains project management, New File, New Folder, Add Files, Delete Selected File / Folder, script testing, and editor save/close commands.
- **Run/Stop** adapts to the current state. Its menu also contains Restart Project, Refresh Preview, and Install Dependencies.
- **Edit Mode** directly enables visual selection. Its menu contains Undo and Redo.
- **Publish** writes the project's published UI settings. Its dropdown opens GitHub, Vercel and Streamlit Community Cloud options.
- **View** contains panel visibility, Focus Preview, Show All Panels, and Reset Workspace Layout.
- **Clear Output** is intentionally available only from the Output panel's right-click menu instead of occupying permanent toolbar space.

## Installation

On Windows, extract the complete application ZIP into a new, empty folder and double-click **HoYo Project Studio.exe** in the extracted `hoyo_project_studio` folder. It handles first-time setup and later launches. You can also use the desktop shortcut created during setup.

For manual setup, open PowerShell **inside the app folder**, then create a virtual environment:

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
6. Wait for the website to appear in the **Preview** tab. Closing that tab keeps Streamlit running; restore it through **Run → Open Running Preview**. Pressing Stop closes the tab when the process exits.
7. Use the **Output** tab for Streamlit logs or the **Console** tab for commands such as `git status`, `python --version`, `pip list`, and `tasklist`.
8. Turn on **Edit Mode**.
9. Click a heading or text label and type directly in the Preview. Press Enter to finish, or Escape to cancel that edit.
10. Use **Properties** on the right for other applicable settings such as colour, spacing and image source.
11. Click **Save** in Properties to keep the visual changes with the project.
12. Use **Undo**, **Redo**, or the **Publish** menu as needed. Publishing prepares the selected project's saved files and UI settings for review.

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

Python files additionally provide code completion, function signatures, documentation, syntax checking, and a **Test Script** button. Each tab retains its own project and interpreter context when you browse another project. Undo and Redo act on the active editor's text. See [EDITOR_UPGRADE.md](EDITOR_UPGRADE.md) for detailed controls and troubleshooting.

Save code changes before running or restarting the project; those actions execute saved files on disk. **Test Script** saves modifications in its selected file before starting it.

```text
Ctrl + S       Save current file, or UI changes when Preview is active
Ctrl + Alt + S Save all open files
Ctrl + W       Close current tab
F7             Test current Python script
Shift + F7     Stop script test
```

Closing the Preview tab only closes the embedded browser surface. It does not terminate the Streamlit process. Restore it with **Run → Open Running Preview**.

## Integrated command-line console

The bottom workspace contains three tabs:

```text
Output       → Streamlit and Project Studio process logs
Console      → interactive system command line
Script Test  → selected Python script output, errors, input and stop controls
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
- `draft.json` contains the last explicitly saved visual changes.
- `published.json` contains approved changes.
- `defaults.json` contains the original design values.

Working changes appear inside HoYo Project Studio as you type directly in Preview or commit Properties fields. **Save** writes them to `draft.json`; until then they remain unsaved in the current Studio session. Changing projects or stopping Preview retains those pending edits. The exit warning covers every project with unsaved UI changes, including one whose preview is closed. **Discard** at exit leaves the previous saved files unchanged, and **Cancel** keeps Studio open.

**Publish** first saves the current project's working draft and then updates `published.json`. For a deployed website to use the same settings, the project should read `.studio/published.json` or use the included `studio_runtime.py` helper. Saving UI properties does not rewrite the project's Python or HTML source.

## Application files

All paths in this table are relative to the extracted `hoyo_project_studio` folder.

| Path | Purpose |
| --- | --- |
| `HoYo Project Studio.exe` | The normal application launcher |
| `app/main.py` and `app/studio/` | Complete desktop application code |
| `app/launcher/` | Automatic setup, Python discovery and build support |
| `app/resources/` | Icons and the Preview editing helper |
| `app/templates/` | Starter projects |
| `app/tests/` | Automated regression checks |
| `app/README.md`, `app/EDITOR_UPGRADE.md`, `app/PUBLISHING.md` | Instructions and feature guides |
| `app/Build Windows EXE.cmd`, `app/Repair Python Completion.cmd` | Optional build and repair tools |

## Next milestones

- Responsive desktop, tablet, and mobile preview sizes.
- Asset library.
- Drag-and-drop section reordering.
- Git status, commit, branch, pull, and push controls.
- Project templates and plugin support.
- More layout controls such as flex, grid, alignment, and positioning.

## Adjustable workspace panels

The Project Library, Project Explorer, live preview, bottom output tabs, and property inspector are separated by visible draggable dividers. Drag a divider to change the width or height of a pane. Project Studio remembers the layout between launches. Use **View → Reset Workspace Layout** to return to the default sizes.

The Project Explorer uses a consistent dark theme on Windows, including rows, selections, and scrollbars.

## Launching without a terminal

The top-level folder contains one executable and the required app folder. Open **HoYo Project Studio.exe** for both initial setup and later use.

### First launch and setup

Double-click:

```text
HoYo Project Studio.exe
```

The native progress window finds an already installed 64-bit Python 3.11 or newer and creates an isolated `.venv` only when needed. It prepares PySide6, Jedi and Pygments, verifies they can load, then opens Studio. Streamlit/Preview support continues in the background, with visible progress and Retry inside the app. The desktop shortcut uses this same launcher. **app/Setup HoYo Project Studio.cmd** opens the same preparation window and automatically launches Studio; there is no separate launch confirmation.

### Normal launch

On later launches, suitable installed packages are reused. Double-click:

```text
HoYo Project Studio.exe
```

for a console-free launch. When all packages are ready, the checks finish without downloading them again. The standard Studio desktop shortcut is refreshed to use this executable. Compatibility launch scripts remain available inside `app` if needed.

If you close Studio while background setup is active, first choose what to do with any unsaved files or UI changes. Cancelling that prompt keeps both the app and setup running. Approving closure stops the setup worker and its owned installation process before the window closes. The next launch checks the existing packages and installs whatever remains missing.

Setup progress is recorded in `app/setup_hoyo_studio.log`. Startup-window failures are also recorded in `app/startup_window.log`. A package conflict that would require changing shared editor dependencies is reported with repair instructions instead of upgrading the interface while it is in use.

### Build a Windows executable

To build a distributable Windows application, double-click:

```text
app/Build Windows EXE.cmd
```

The build must be performed on Windows. You can run it directly after extraction without running Setup first. It reuses a valid application `.venv`; otherwise it creates one using a compatible Python already installed on your PC. Both the traditional Python launcher and Python Install Manager can be used to discover installed runtimes, with automatic interpreter installation disabled for the build.

To edit files during initial setup, use **HoYo Project Studio.exe**. **app/Build Windows EXE.cmd** remains the separate packaging command. If another setup or build is still installing into the same environment, wait for it to finish before starting the build; a shared installation lock prevents overlapping installers. Once setup is ready, Studio can remain open while the EXE is built.

The builder checks installed package versions before installing anything. Suitable dependencies and PyInstaller are kept. Missing or incompatible packages are installed into the application's `.venv`; this may download packages, but never another Python interpreter. PyInstaller 6.21 or newer is required for the builder's Python 3.15 support. See the official [PyInstaller change log](https://pyinstaller.org/en/stable/CHANGES.html).

After a successful build, File Explorer selects the EXE and the build command window closes automatically. If you started the command from an existing terminal, control returns to that terminal. Failed builds keep the command window open so you can read the error, with a copy in `build_windows_exe.log`. An existing broken environment is reported and left in place.

The result is created at:

```text
app\dist\HoYo Project Studio\HoYo Project Studio.exe
```

The build uses an **onedir** package because Qt WebEngine contains several required runtime files. Distribute the complete `HoYo Project Studio` folder inside `dist`, not only the `.exe` file.
