# File controls and Python editor — version 0.4.2

## Update your existing installation

1. Close HoYo Project Studio.
2. Extract the replacement ZIP into your **HoYo Project Studio application folder**. This is the folder containing `main.py`, `requirements.txt`, and the `studio` folder.
3. Merge the folders and replace the matching application files when Windows asks.
4. Reopen **Launch HoYo Project Studio.vbs** from that same folder. The window title should read **HoYo Project Studio 0.4.2**. If a desktop shortcut opens an older version, run **Create Desktop Shortcut.cmd** from the updated folder.
5. Open the existing `dwd` file and choose **Python** from the **Language** selector above the editor. Suggest and Python Help become available. Studio remembers this choice when you reopen the file.

This version adds no dependencies. If the advanced helper reports an installation problem, close Studio and run **Repair Python Completion.cmd**. It updates Jedi and Pygments in the application's existing `.venv`, checks completion, and shows a PASS or FAIL result.

The application keeps its project list in the existing application-data location. Updating the application files does not change your managed projects. New File, Add Files, and Delete change a managed project only when you choose those actions in the app.

For a fresh installation, extract the complete application ZIP, open its `hoyo_project_studio` folder, and run **Setup HoYo Project Studio.cmd**. Setup now tests completion after installing the dependencies.

If you prefer PowerShell, open it in the application folder and run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launcher\check_python_completion.py
.\.venv\Scripts\python.exe main.py
```

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

The app remembers an explicit language choice for that file. Choosing Auto restores filename-based behaviour. Changing the language does not rename, reload, or save the file; your unsaved text and undo history remain available. When Python assistance is off, its controls stay visible and the status tells you how to enable it.

Basic Python suggestions appear as soon as the short typing delay ends, without waiting for an external helper. These include keywords, builtins, names from the current file, standard-library import names, and a small catalogue of math members. This mode is deliberately limited: it does not infer arbitrary object types or inspect third-party packages.

Advanced suggestions use **Jedi**, with the current unsaved file contents and the file's original project context. The editor uses the Python interpreter selected for that project, including its existing `.venv` when available. Imported-package suggestions depend on what is installed in that interpreter. If the project interpreter fails, the helper tries Studio's Python and identifies that fallback beneath the editor. The saved project interpreter setting is kept.

Python analysis runs on a shared background worker. Typing a new revision or closing a tab invalidates stale results. The edited program is not run to produce suggestions or syntax checks.

### Useful controls

| Control | Use |
| --- | --- |
| **Suggest** or **Ctrl+Space** | Request Python completions at the cursor. Suggestions also appear automatically after a dot or while typing a name. |
| **Tab** or **Enter** while suggestions are open | Insert the selected suggestion. Use the arrow keys to choose an item, or **Esc** to dismiss the popup. |
| **Python Help**, **F1**, or **Ctrl+Q** | Read documentation for the name, function, or class under the cursor. |
| Type inside a function call | See the call signature and current argument beneath the editor. |
| Click a red **Syntax check** message | Jump to the syntax error. Its location is also underlined in the editor. |
| **Tab** / **Shift+Tab** | Indent or outdent lines. Enter carries indentation forward and adds indentation after Python block headers. |
| **Ctrl+/** | Comment or uncomment selected lines. |
| **Ctrl+S** / **Ctrl+Alt+S** | Save the current file / all open files. |
| **Ctrl+Z** / **Ctrl+Y** | Undo / redo code edits on Windows. |
| **Ctrl+Alt+N** | Create a new project file. |

To try completion, create **practice.py**, type **pri**, and pause or click **Suggest**. Choose **print** and press **Tab**; the text should become **print**. Next, type `import math`, then on the following line type `math.` and pause. The popup should list available members. Put the cursor on a function such as `print` and press **F1** to read its documentation.

Always **save your changes before Run or Restart**: those commands execute the files on disk.

## Dependencies and troubleshooting

Jedi and Pygments belong in Studio's Python environment. **Repair Python Completion.cmd** always uses the `.venv\Scripts\python.exe` beside the application, so it cannot accidentally install the editor packages into a different Python selected through PATH. It updates only these editor packages and their required dependencies. It does not recreate the environment.

The repair script checks actual `print` and `math` completion and saves **completion_diagnostic.txt** in the application folder. If that location is not writable, it prints the alternative report location. A failed install or test is reported as FAIL, including when an older installation still has some working packages. The probe has a time limit so a stuck interpreter is reported.

If the editor says **Basic Python suggestions**, the basic popup still works. Hover over that message for the advanced-helper error and interpreter path. Run the repair script after closing Studio to restore full assistance. If only a particular imported package has no suggestions, check that the package is installed in the Python environment used by that managed project. You can configure an interpreter through `python_executable` in the project's `.studio/project.json`; relative paths are resolved from the project folder.

If completion is still absent, first check that the app title says **0.4.2** and that the file either has a **.py** extension or has **Python** selected in the Language control. Try typing `pri` and pressing Ctrl+Space. If the helper still reports an error, share the diagnostic report and the message below the code editor so the failing environment can be identified precisely.

Syntax checking uses the Python version running HoYo Project Studio. It checks whether the current text can be compiled, including invalid indentation and misplaced statements. It does not perform full type checking, runtime testing, or comprehensive linting.

Files over 5 MiB and binary files are not opened in the text editor. Python assistance is paused for buffers over 500,000 characters; those files can still be edited within the editor's file-size limit.

## Implementation references

- [Jedi API](https://jedi.readthedocs.io/en/latest/docs/api.html)
- [Qt syntax highlighter](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QSyntaxHighlighter.html)
- [Qt file recycling](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QFile.html#PySide6.QtCore.QFile.moveToTrash)
- [Pygments](https://pygments.org/docs/)
