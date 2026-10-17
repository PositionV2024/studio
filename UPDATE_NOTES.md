# Version 0.4.2 — Recognise and select Python files

The screenshot showed an open file named `dwd`, without a `.py` extension. The previous editor treated it as a general text file and hid Python assistance, regardless of whether Jedi was installed.

- The editor now displays **Language: Auto (file name) / Python / Plain text**. Choose Python for an extensionless file to activate completion, documentation, syntax checks, and Python colouring.
- Suggest and Python Help remain visible with a clear disabled state outside Python. The status explains how to enable them.
- Changing the language preserves unsaved text, undo history, encoding, newline style, and the file name. Mode switches invalidate stale assistance and clear old diagnostics.
- Studio remembers each file's explicit language choice. Selecting Auto removes that override.
- **New File** defaults to **Python (.py)**. A live preview shows that `dwd` will become `dwd.py` before creation. Explicit extensions remain unchanged; Plain text and Other choices support other file types.
- Name validation and collision feedback stay in the creation dialog. An existing file is never overwritten.
- This update adds no new dependencies. The existing repair script remains available for advanced-helper installation problems.

See **EDITOR_UPGRADE.md** for the exact steps for the existing `dwd` file.

---

# Version 0.4.1 — Python completion reliability

- Basic suggestions appear before project analysis finishes. A slow or unavailable advanced helper no longer prevents keyword, builtin, current-file name, or common math suggestions.
- Suggest, Ctrl+Space, and automatic completion remain enabled when Jedi is missing. Builtin documentation and basic call signatures remain available.
- The helper uses the console interpreter beside a Windows `pythonw.exe` when present, preserving the selected virtual environment. If a project interpreter fails, it tries Studio's interpreter and reports the fallback.
- Function-signature errors no longer discard a successful completion result.
- Refining suggestions preserves the highlighted choice. Python assistance displays its active mode and puts diagnostic details in the status tooltip.
- **Repair Python Completion.cmd** repairs the editor dependencies in Studio's own environment and runs a bounded completion check. It records failures in **completion_diagnostic.txt**.
- Setup runs the completion check too. The application title includes **0.4.1** so an older shortcut target is easy to identify.

See **EDITOR_UPGRADE.md** for update steps and a quick completion test.

---

# Version 0.4.0 — File controls and Python editor

## Project Explorer

- Visible New File, New Folder, Add Files, and Delete buttons, plus right-click and Project-menu commands.
- A destination label shows where new/copied files will be placed.
- Exclusive creation, Windows filename checks, and copy collision reporting protect existing files.
- Deletion uses the operating system's Recycle Bin / Trash after confirmation. It checks unsaved tabs, retains buffers on failure, and protects the project root and outside paths.
- Tree selection and expanded folders survive refresh. Delete is a tree-only shortcut.

## Code editor

- Improved Python colours, including multiline strings and docstrings; Pygments highlighting for other recognised languages.
- Jedi completion using the current buffer and original project context.
- Function signatures, Python documentation help, and syntax feedback.
- Serial background analysis with stale-request cancellation.
- Native text Undo/Redo while editing, stable per-tab interpreter context, and explicit cleanup when closing tabs.
- The project runner resolves relative interpreter paths from the project folder. Re-importing a project retains its configured Python interpreter.

## Updating

Close the app, copy the replacement files into the application folder, and rerun **Setup HoYo Project Studio.cmd**. The new requirements include Jedi and Pygments. See **EDITOR_UPGRADE.md** for the full update guide.

---

# Previous update — Closable preview tabs and built-in code editor

## Streamlit preview reliability

- Project Studio now checks the actual Streamlit HTML asynchronously instead of treating an open TCP port as a finished page.
- The embedded Chromium preview uses a dark background during navigation and retries early blank-page loads.
- Failed preview navigation shows an in-editor error page instead of a plain white surface.

## Preview tab

- The live website now opens in a normal closable workspace tab.
- Closing the Preview tab does not stop Streamlit.
- Restore the existing server through **Run → Open Running Preview**.
- Selecting another project no longer replaces the running project's preview.

## Built-in code editor

- Clicking a supported source file in Project Explorer opens it in Project Studio.
- Multiple files can remain open in movable, closable tabs.
- Python, JSON, HTML, CSS, JavaScript, Markdown, TOML, YAML, XML, shell scripts, and many other text formats are supported.
- The editor includes line numbers, syntax highlighting, current-line highlighting, Save, Reload, Save All, and unsaved-change protection.
- Binary and unsupported files can still be opened with the system application.
