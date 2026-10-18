# File controls, Python editor and visual editing — version 0.4.15

## Install the updater-enabled application

1. Close HoYo Project Studio and save any pending changes.
2. Download the **complete application ZIP** for version 0.4.15.
3. Extract it into a **new, empty folder**. Open its `hoyo_project_studio` folder.
4. You should see only **HoYo Project Studio.exe** and the **app** folder.
5. Double-click **HoYo Project Studio.exe**. After setup, the title should read **HoYo Project Studio 0.4.15**.

Version 0.4.15 fixes the update source to **PositionV2024/studio** and simplifies checking and downloading. Install the downloaded complete ZIP in a new folder once to get this checker. A complete application ZIP is then sufficient for future releases. See [UPDATING.md](UPDATING.md) for the update workflow and release instructions. Older replacement ZIPs belong to the earlier layout.

Your saved project list and workspace settings use their existing application-data location. Remembered GitHub credentials keep their existing Windows Credential Manager entry. Saved projects retain their original folder paths. Keep the old application folder in place while checking the new copy, particularly if projects or their Python environments are stored inside it. This update does not move or delete them.

The new copy prepares its own environment inside `app` using an already installed Python. Required packages may need to download again for this new environment. Another Python interpreter is never downloaded. Existing compatible packages are reused on subsequent launches of this copy.

## Which item to open

**HoYo Project Studio.exe** is the normal launcher. It handles setup, opens Studio when the editor essentials are ready, and lets remaining Preview support install in the background. Keep the **app** folder beside it; you do not need to open that folder for normal use.

All previous support files are retained inside `app`, including guides, tests, repair commands and the optional **Build Windows EXE.cmd** command. The normal `.exe` is already built.

After a successful setup, the standard Studio desktop shortcut is refreshed to open this copy. An existing custom shortcut is preserved; use `app/Create Desktop Shortcut.cmd` if you explicitly want to replace it with the standard shortcut.

For a manual check, open PowerShell **inside app** and run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launcher\check_python_completion.py
.\.venv\Scripts\python.exe main.py
```

## Use Studio while Preview support finishes installing

The first setup window prepares the interface, completion and highlighting before opening Studio. Your installed Python is reused; a separate interpreter is never downloaded. A fresh PC still needs those interface packages before the full editor can appear.

Once the editor opens, you can browse, create, edit and save files while the top banner says **Preparing Preview support. You can continue editing files.** Select **Details** to view the setup output. Run, Install Dependencies and Test Script temporarily wait when they would use Studio's Python environment. The same controls remain available for projects with an independent Python environment. After verified setup success, the affected controls enable automatically.

A failed download leaves the editor open and shows **Retry**. Existing editor and shared packages are pinned during background installation, so the running interface is not upgraded underneath you. A dependency conflict explains how to close Studio and repair its requirements before reopening it.

Closing Studio still asks about unsaved files and visual changes. Cancel keeps both Studio and setup running. Save or Discard completes that choice, stops the background setup process safely and closes the app. On the next launch, existing packages are checked and missing ones can be installed again. Completed packages are retained; pip controls reuse of any partially downloaded files.

## Build the Windows EXE using installed Python

Open the **app** folder and double-click **Build Windows EXE.cmd**. You do not have to run Setup first just to build the EXE.

For the new experience of editing during setup, start with **HoYo Project Studio.exe**. Build Windows EXE is the separate packaging command. Once the setup banner reports readiness, you can leave Studio open and build the EXE. If setup or another build is already using the installer, the new build explains that you should wait instead of starting a second installer or overwriting the active build log.

The builder reuses the application's working `.venv`. When there is no `.venv`, it finds your existing 64-bit Python 3.11 or newer and creates a local environment from that installation. No Python interpreter is downloaded. Already suitable application/build packages are reused; only missing or incompatible packages need installation. A first build can therefore still download PySide6, Streamlit, PyInstaller or their package dependencies.

After success, File Explorer selects `app\dist\HoYo Project Studio\HoYo Project Studio.exe` and the build window closes automatically. If you ran the command inside an existing terminal, it returns to that terminal. On failure, the window stays open and shows the error. The full log is `build_windows_exe.log` inside the app folder.

Keep the complete `app\dist\HoYo Project Studio` folder together when moving or sharing the built app; its EXE needs the adjacent runtime files. An incomplete or broken `.venv` is left untouched and reported with recovery instructions.

If your installed Python is in an unusual location and cannot be discovered, you can provide its real executable path explicitly from PowerShell opened inside app:

```powershell
& .\launcher\build_exe.ps1 -PythonExecutable 'C:\path\to\Python\python.exe'
```

## Type directly in the Preview

Turn on **Edit Mode**, then click editable text such as a heading, paragraph or button label. An editor appears over the selected text so you can type there. The matching Text field in **Properties** and the unsaved status update as you type. Text editing preserves decorative icons and links; it does not replace nested input controls.

- **Enter** or clicking outside the text finishes that edit.
- **Shift+Enter** inserts a line break while typing.
- **Escape** restores the exact text and draft state from before that edit.
- **Undo** after finishing reverses the entire typing session; Redo brings it back.
- **Save** also finishes an active text edit before writing, so its last character is included.

Use Properties for colour, dimensions, spacing, images and other settings. Direct typing edits supported labels and content. To use actual textboxes, buttons or dropdowns as part of the running app, turn Edit Mode off.

## Save visual changes

The **Properties** panel keeps **Save** visible while its fields scroll. Editing text in the Preview or a field marks the project as **Unsaved changes** immediately. Click Save to commit the latest values and persist all pending UI changes for the selected project. **Ctrl+S** does the same while the Preview tab is active. The code editor's Save button and Ctrl+S continue to save source files.

Save writes the project's `.studio/draft.json`. **Publish** saves first, then updates `.studio/published.json` for projects that load published settings. Saving visual properties does not rewrite Python or HTML source.

Changing projects, closing the Preview tab, and stopping or restarting the server retain pending UI changes in the current Studio session. The Save button still works when the selected project's preview has stopped. Returning to a project restores its draft and undo history.

### Only applicable properties

Unsupported fields and their labels are hidden. Groups with no applicable fields are hidden as well. A heading shows editable text; an image shows image source without text/font fields. Inline text does not show width/height fields that would have no effect. For a container with nested labels or controls, select the text label itself. Clearing selection shows a short selection hint and retains the Save footer.

## Publish to GitHub or a hosting service

Open the dropdown beside **Publish**, then choose **Publish to GitHub…** or **Publish to Vercel…**. Studio saves modified source tabs belonging to the selected project and prepares its published visual settings before showing the local file review. Opening this window does not upload the project.

For GitHub, use **Sign in with GitHub…** to open the built-in connection form. Create an access token through its browser link, grant your selected repository **Contents: Read and write**, paste the token and choose **Connect GitHub**. Windows can remember the connection securely. Choose the repository and files, select **Review publish**, inspect the destination and actual changes, then click **Publish to GitHub**. New repositories default to Private; existing repositories receive a new update branch for review.

For a Streamlit app on Vercel, select **Prepare Streamlit deployment files…**, inspect them, and click **Add deployment files**. Publish those files to GitHub, then select **Open Vercel import** and complete the deployment in your Vercel account. This route uses Vercel's beta container support; connection or instance restarts can reset an in-memory Streamlit session. **Streamlit Community Cloud** is also available from the same Publish menu.

See [PUBLISHING.md](PUBLISHING.md) for exact steps, project dependencies, settings integration and existing-repository behavior.

### Leaving with unsaved changes

When you close Studio with pending UI edits, it asks **“Save your UI changes before leaving?”** and lists the affected projects.

| Choice | Result |
| --- | --- |
| **Save** / **Save All** | Saves every listed project's pending UI changes, then closes. Save All appears when more than one project has changes. |
| **Discard** | Closes and leaves each project's last saved draft unchanged. |
| **Cancel** or Escape | Keeps Studio open with the pending changes and running processes intact. |
| Save fails | Shows the failure and keeps Studio open with the affected changes available for another attempt. |

The existing save prompts for modified source files still appear. To check the visual warning, change a heading after saving, close the app, choose Cancel, and verify that the pending text remains. Save it, reopen Studio, and run the project to confirm that the saved visual edit returns.

## Preview loading and stopping

Version 0.4.5 fixes a browser-status conversion error that caused Studio to report a blank preview even when the page had rendered. The browser now returns its status as JSON, and Studio waits for Streamlit's rendered interface without repeatedly reloading a page that is still starting.

The Output panel distinguishes the Streamlit server becoming available from the embedded preview being displayed. Selecting another project while a server starts keeps the preview attached to the project being run. Re-selecting that running project does not reload it just because its URL has a trailing slash.

The preview also loads its Edit Mode helper from Qt's bundled client. Turn on **Edit Mode**, then click an element in the running page to show its text and settings in Properties.

| Action or event | Preview behaviour |
| --- | --- |
| **Run** | Opens the Preview tab and displays the selected Streamlit app when it is ready. |
| **Stop**, a normal server exit, or a process crash | Closes the Preview tab and cancels pending browser loading work. Open code files remain available. |
| **Restart Project** or **Switch and Run** | Closes the old preview as its server exits, then opens the next run's preview. |
| Close the Preview tab yourself | Stops that browser's pending work while keeping Streamlit running. A late readiness signal does not reopen the tab. |
| **Run → Open Running Preview** | Reopens the existing server's preview. |
| **Refresh Preview** or **F6** | Loads the running project's URL again, including after an in-preview loading error. |

If the preview cannot finish loading, read the error and the Streamlit logs in **Output**, then use **Refresh Preview** after addressing the problem. A Python error displayed inside a running Streamlit app stays visible in the preview; automatic tab closure follows the server process exiting.

The **Test Script** button remains above each Python editor. Use it or **F7** to test the selected file as a Python script. Use **Run** for the full Streamlit app and its browser preview.

## Add and remove files

Select your project in **Project Library**, then use the buttons above **Project Explorer**:

| Control | What it does |
| --- | --- |
| **New File** | Opens a file-type and filename dialog. Python (.py) is selected by default; `helpers` previews and creates `helpers.py`. An explicit extension is preserved. |
| **New Folder** | Creates a folder inside the selected destination. |
| **Add Files…** | Opens a file picker and copies one or more existing files into the project. |
| **Delete** | Asks before moving the selected file or folder to the operating system's Recycle Bin / Trash. |

The **Add to:** line shows where new and copied files will go. Select a folder to use it as the destination. Selecting a file uses that file's parent folder. Clear the selection to use the project root. These commands are also available from the right-click menu and the top **Project** menu.

Existing files are never silently overwritten when adding files. Name collisions and invalid Windows names are reported. A failed copy is cleaned up. The project root itself cannot be deleted through these controls.

When a file or folder has open editor tabs, deletion asks about unsaved changes first. A cancelled or failed deletion keeps those tabs open. Successful deletion closes only affected tabs. The app does not fall back to permanent deletion when recycling fails.

## Python editor essentials

Open a `.py`, `.pyw`, or `.pyi` file to enable Python assistance automatically. For an existing extensionless file such as **dwd**, choose **Python** in the **Language** selector above the editor. Source highlighting distinguishes keywords, strings, comments, numbers, functions, and built-ins. Multiline strings and docstrings retain their colouring. Pygments provides highlighting for other recognised project languages.

The selector has three choices:

| Language | Behaviour |
| --- | --- |
| **Auto (file name)** | Uses the filename extension. Python assistance is enabled for `.py`, `.pyw`, and `.pyi` files. |
| **Python** | Enables Python assistance and highlighting for the open file, including files without an extension. |
| **Plain text** | Turns off code highlighting and Python assistance for the open file. |

The app remembers an explicit language choice for that file. Choosing Auto restores filename-based behaviour. Changing the language does not rename, reload, or save the file; your unsaved text and undo history remain available. When Python assistance is off, the status tells you how to enable it with the Language selector.

Basic Python suggestions appear as soon as the short typing delay ends, without waiting for an external helper. These include keywords, builtins, names from the current file, standard-library import names, and a small catalogue of math members. This mode is deliberately limited: it does not infer arbitrary object types or inspect third-party packages.

Advanced suggestions use **Jedi**, with the current unsaved file contents and the file's original project context. The editor uses the Python interpreter selected for that project, including its existing `.venv` when available. Imported-package suggestions depend on what is installed in that interpreter. If the project interpreter fails, the helper tries Studio's Python and identifies that fallback beneath the editor. The saved project interpreter setting is kept.

Python analysis runs on a shared background worker. Typing a new revision or closing a tab invalidates stale results. The edited program is not run to produce suggestions or syntax checks.

### Test the selected script

1. Select a Python script in **Project Explorer** to open it, or select its existing editor tab.
2. Click **Test Script** above the editor, or press **F7**.
3. Studio saves any changes in that file, then runs the file with its own project's Python interpreter and working folder. A failed save prevents the test from starting.
4. Read the output in the **Script Test** tab at the bottom of the workspace. It identifies the script and shows printed output, Python tracebacks, and the final exit result.
5. If the program asks for input, type a reply into the input box and press Enter or **Send**. Use **End Input** when the program expects the end of its input stream.
6. Use **Stop** in that tab or **Shift+F7** to stop a long-running test.

For a quick example, create a Python file with:

```python
name = input("What is your name? ")
print(f"Hello, {name}!")
print("The script reached its final line.")
```

Click Test Script, enter your name in the Script Test input box, and press Send. The greeting and final line should appear, followed by a finished result with exit code 0.

**Test Script executes the selected file as a normal Python script.** A finished result with exit code 0 means Python exited without reporting an error; check the output against what you expect. Existing test functions run only if the script calls them. Use **Run** to launch the full Streamlit app and its browser preview.

One script test can run at a time. Switching the selected project or editor tab does not change an active test's file or environment. The Stop control remains available in Script Test even if the source editor tab is closed. Closing Studio stops the test process.

The selected script retains the Python environment associated with its original project. When that project specifies an interpreter and it is missing or cannot start, Studio reports the failure in Script Test. Install required packages in that interpreter's environment or correct its path in `.studio/project.json`.

### Useful controls

| Control | Use |
| --- | --- |
| Type a name or a dot; **Ctrl+Space** | Suggestions appear automatically after a short pause. Ctrl+Space requests them immediately at the cursor. |
| Hover over a suggestion; **Up** / **Down** | Select and visibly highlight an item. Its signature and documentation appear in the adjacent pane within the same popup. |
| Click a suggestion; **Tab** / **Enter** | Insert the highlighted item. |
| **F1** / **Ctrl+Q** | Show the highlighted suggestion's details, or documentation for the name under the cursor, in the same popup. |
| **Esc** | Close the suggestion list and documentation. |
| Type inside a function call | See the call signature and current argument beneath the editor. |
| Click a red **Syntax check** message | Jump to the syntax error. Its location is also underlined in the editor. |
| **Tab** / **Shift+Tab** | Indent or outdent lines. Enter carries indentation forward and adds indentation after Python block headers. |
| **Ctrl+/** | Comment or uncomment selected lines. |
| **Ctrl+S** / **Ctrl+Alt+S** | Save the current file (or UI changes when Preview is active) / all open code files. |
| **Ctrl+Z** / **Ctrl+Y** | Undo / redo code edits on Windows. |
| **Ctrl+Alt+N** | Create a new project file. |
| **Test Script** / **F7** | Save modifications in the active Python file and run that script. Output and errors appear in Script Test. |
| **Stop** in Script Test / **Shift+F7** | Stop the active script test. |

The separate **Suggest** and **Python Help** buttons have been removed from the editor toolbar. Completion and documentation now share a single popup. You can scroll long documentation in its details pane while keeping the suggestion list open. The footer shows the available keyboard controls.

To try completion, create **practice.py**, type **pri**, and pause or press **Ctrl+Space**. Hover over **print** to highlight it and read its signature and help, then press **Tab**; the text should become **print**. Next, type `import math`, then on the following line type `math.` and pause. The popup should list available members. Put the cursor on a complete function name such as `print` and press **F1** to read its documentation in the same popup.

For the Streamlit example, enter:

```python
import streamlit as st
st.
```

Hover over **bar_chart**. The highlight follows the mouse, and the detail pane shows the callable signature and the package's own help text when Streamlit is available in the analysis environment. Use Up or Down to browse another item without inserting it. The helper loads documentation only for the selected item; a brief loading message can appear while it resolves a package symbol. When documentation cannot be resolved, the pane says so.

Always **save your changes before Run or Restart**: those commands execute the files on disk.

## Dependencies and troubleshooting

Jedi and Pygments belong in Studio's Python environment. **app/Repair Python Completion.cmd** always uses the `.venv\Scripts\python.exe` inside the app folder, so it cannot accidentally install the editor packages into a different Python selected through PATH. It updates only these editor packages and their required dependencies. It does not recreate the environment.

The repair script checks actual `print` and `math` completion and saves **completion_diagnostic.txt** inside the app folder. If that location is not writable, it prints the alternative report location. A failed install or test is reported as FAIL, including when an older installation still has some working packages. The probe has a time limit so a stuck interpreter is reported.

If the editor says **Basic Python suggestions**, the basic popup still works. Hover over that message for the advanced-helper error and interpreter path. Run the repair script after closing Studio to restore full assistance. If only a particular imported package has no suggestions, check that the package is installed in the Python environment used by that managed project. You can configure an interpreter through `python_executable` in the project's `.studio/project.json`; relative paths are resolved from the project folder.

If completion is still absent, first check that the app title says **0.4.15** and that the file either has a **.py** extension or has **Python** selected in the Language control. Try typing `pri` and pressing Ctrl+Space. If the helper still reports an error, share the diagnostic report and the message below the code editor so the failing environment can be identified precisely.

Syntax checking uses the Python version running HoYo Project Studio. It checks whether the current text can be compiled, including invalid indentation and misplaced statements. It does not perform full type checking, runtime testing, or comprehensive linting.

Files over 5 MiB and binary files are not opened in the text editor. Python assistance is paused for buffers over 500,000 characters; those files can still be edited within the editor's file-size limit.

## Implementation references

- [Jedi API](https://jedi.readthedocs.io/en/latest/docs/api.html)
- [Qt syntax highlighter](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QSyntaxHighlighter.html)
- [Qt file recycling](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QFile.html#PySide6.QtCore.QFile.moveToTrash)
- [Qt process execution](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QProcess.html)
- [Qt embedded browser and JavaScript callbacks](https://doc.qt.io/qtforpython-6/PySide6/QtWebEngineCore/QWebEnginePage.html)
- [Python command-line execution](https://docs.python.org/3/using/cmdline.html)
- [Pygments](https://pygments.org/docs/)
