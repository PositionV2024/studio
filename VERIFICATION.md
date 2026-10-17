# Version 0.4.2 verification

Verified on 17 October 2026 with Python 3.12.14, PySide6 6.12.0, Jedi 0.20.1, and Pygments 2.21.0 in a Linux test environment.

## Automated checks

The combined test suite completed successfully: **77 passed in 6.07 seconds**. This includes the existing file-management and editing checks, previous completion failure regressions, and the extensionless-file workflow shown in the user's screenshot.

| Area | Verified behaviour |
| --- | --- |
| File creation and import | Valid Windows names, exclusive creation, duplicate-name refusal, source preservation, and project path boundaries. |
| Explorer interaction | New files open in the editor; selection and expanded folders survive refresh; blank-space/root destination works after deleting the last file; Delete is scoped to the tree. |
| Open document removal | Cancelled or failed deletion keeps buffers; requested saves complete before a folder move; only affected tabs close; recycling a symlink preserves its target editor. |
| Python completion | Automatic typing/dot suggestions, Tab acceptance, project imports from the unsaved current file, and correct replacement after an emoji and within an existing identifier. |
| File-type creation | The actual New File modal defaults to Python, previews `dwd.py` when `dwd` is entered, creates it exclusively, and opens it through the production MainWindow editor path with completion and F1 help. Explicit extensions, alternative types, invalid names, cancellation and collisions are covered. |
| Existing extensionless file | Selecting Python for an open `dwd` enables real Jedi `math.sqrt` completion and print documentation without renaming the file or losing unsaved text, BOM or CRLF conventions. |
| Language switching | Python, plain text and automatic modes preserve cursor/selection/native undo and codec choice; switching away clears helper UI, invalidates pending work and unregisters the document; switching back uses a fresh registration. |
| Remembered language | A temporary settings file and a recreated MainWindow prove that `dwd` reopens in Python mode. Selecting Auto removes the override. Original bytes, filename and clean state remain intact. |
| Missing or stalled helper | Automatic typing, a real Suggest-button click, and Ctrl+Space produce usable basic suggestions with Jedi disabled; builtin help works; an intentionally blocked worker does not prevent immediate suggestions; the highlighted choice survives a later result update. |
| Interpreter and parser failures | A missing project interpreter recovers through Studio's Python with visible status; an incompatible parser returns basic suggestions; GUI interpreter paths prefer their same-folder console sibling; a secondary signature error preserves successful completions. |
| Safe basic assistance | Keyword, builtin, current-file and static math suggestions are bounded, use Unicode prefixes, avoid unchanged-prefix self-suggestions, and never import or run the edited code. |
| Repair diagnostic | Real print and math completion pass; a clean venv without Jedi produces a failure report; a stalled child check hits its deadline. |
| Python help | Call signatures, current arguments, and F1 documentation from real Jedi queries. |
| Syntax feedback | Compile-scope errors appear with underlines and clickable locations; checks do not execute the edited source. |
| Editing | Indentation, comment toggling, native undo/redo, UTF-8 BOM and declared encoding preservation, CRLF/LF/CR newlines, and highlighter replacement on reload. |
| Background work | Tabs share one Jedi analysis worker; stale replies are suppressed; immediate basic suggestions stay available while project analysis is pending. |
| Project context | Existing tabs retain their original project/interpreter when another project is selected; visual editing history is separate from code undo/redo. |
| Existing settings | The existing settings-store test continues to pass. |

All **35 Python, launcher, and build-spec source files** passed compilation checks. Every file from the previous complete application archive is retained.

## Application inspection

For version 0.4.2, the actual Qt editor was opened on an extensionless file named `dwd`. Before selecting Python, the Language control, disabled helper buttons, and explanation were visible. Selecting Python enabled both helper controls and real Jedi completion for `math.sq`. The editor states and `sqrt` popup were rendered and visually inspected. The New File dialog was also inspected with `dwd.py` shown in its filename preview. Shutdown completed successfully.

The preceding 0.4.1 verification also launched the editor with Jedi disabled and visually checked the working `print` fallback popup. Those behaviours remain covered in the combined test suite.

During the preceding 0.4.0 update, the complete MainWindow was launched with its embedded preview component and inspected at 1440 × 900. The new file controls, source colours, editor toolbar, and syntax status were visible. Those file and editor lifecycle paths remain covered by the passing combined suite.

The 0.4.0 native Qt recycling checks moved temporary files, a folder, and a symlink into the Linux Trash. The symlink target and unrelated files remained intact, and deleting the project root was rejected. This update does not change recycling code.

Relative Python interpreter paths and retaining the configured interpreter during re-import were also checked.

## Practical limits

This environment did not run a Windows desktop session or build a Windows executable. The repair's Python diagnostic and timeout behaviour were exercised on Linux; the CMD entry point and full Windows setup must run on the user's PC. The setup script now includes the diagnostic, and the existing launchers and dependency-aware build specification are retained.

The user's latest screenshot directly showed an open file named `dwd` without an extension and no Python helper controls. This matched and reproduced the filename-suffix gate: the file never entered Python mode. Version 0.4.2 makes the mode explicit and supports an existing extensionless file. The separate repair report remains available for dependency or interpreter errors on the actual installation.

Syntax checking uses the Python version running Studio. Advanced completion tries the project's interpreter, then explicitly reports if Studio's interpreter is used instead. Basic assistance provides a limited safe vocabulary; it does not infer arbitrary package types. Syntax feedback does not replace type checking, comprehensive linting, or running the program.

## Run the suite locally

From the application folder, after installing its requirements:

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
```
