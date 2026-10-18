# Version 0.4.18 — Flexible update ZIPs and visible comparison feedback

- Identify the complete Windows application by its contents. Accept a package directly at the ZIP root or inside an arbitrarily named, unambiguous chain of enclosing folders.
- Remove filename guesses about application name, platform, source or patch contents. A single safe ZIP name, including names with spaces, parentheses or a numeric version, can be checked; archive contents and version remain authoritative.
- Preserve the known release version and notes while reading the package and after download/preparation failures.
- Show concrete loading, cancelled, failed and identical-package states inside **File changes**, together with the existing changed-file list and colored text differences on success.
- Disable Download for an invalid or incomplete checked package. Keep local comparison failures separate from release-package rejection and retain mandatory installation validation.
- Report missing package contents instead of asking for the enclosing folder to be renamed. Preserve manifest, checksum, path, account, cancellation and transaction/recovery checks.
- Keep the connected GitHub account requirement, fixed `PositionV2024/studio` source, manual checks, disabled downloads for current/older releases, and explicit restart with unsaved-change handling.
- Retain every application file and the simple **HoYo Project Studio.exe** plus **app** folder layout. No new dependency is required.

# Version 0.4.17 — Choose an existing GitHub repository

- Replace the typed destination in **Update an existing repository** mode with a dropdown of repositories owned by the connected GitHub account. Load accessible public and private destinations in the background and provide **Refresh list**.
- Hide the entire **New repository visibility** row in existing-repository mode. Creating a repository still shows its name and Private/Public controls. Existing visibility comes from GitHub in the publication review.
- Show clear loading, sign-in, empty-list and retry states. Identify archived, disabled or known non-writable destinations without offering them for publication.
- Preserve a selected repository when it remains available after refresh. A missing repository or changed account requires a fresh choice; Studio does not silently choose a different upload destination.
- Invalidate the repository list and publication review when the connected account changes, including a new token for the same account. Discard cancelled or stale results and safely drain workers before closing.
- Keep selecting files, **Review publish** and the explicit **Publish to GitHub** action. Repository discovery only reads GitHub data and uses Studio's existing connection without new dependencies or external software.
- Retain the complete compact application, including the account-based updater, release file comparison, editor, Preview and Properties.

# Version 0.4.16 — Required GitHub account and release file changes

- Require Studio's connected GitHub account for every update check and download. Reuse the existing publishing connection, remove the optional account checkbox and disable update actions while disconnected.
- Keep the application repository fixed to **PositionV2024/studio**, with manual checking and no startup requests.
- Add a **File changes** tab showing added, modified and removed application files between the saved desktop application and the latest published release package. Select a text file to inspect colored line differences; binary, large and unavailable files show an explanation.
- Compare actual saved file bytes, including local application edits, even when the online release is the same version or older. Exclude projects, environments, settings and unrelated files.
- Fetch and validate the exact release package in the background for comparison. Checking changes no installed files; a verified cached ZIP can be reused when **Download update** is selected.
- Keep downloads disabled for equal or older releases and for releases without a ready package. Preserve version results when comparison is unavailable, and explain the comparison issue separately.
- Invalidate reviewed results and cached packages when the connected account changes, including a new token for the same account. Preserve cancellation, Save/Discard/Cancel and explicit restart installation.
- Retain the complete two-item application folder, all previous support files, the built-in GitHub connection and the existing Python environment behavior. No new dependency is required.

# Version 0.4.15 — Simpler updates

- Fix the application repository to **PositionV2024/studio** and display it as text. Remove the editable source and ignore repository overrides saved by older versions.
- Remove the startup-check checkbox, its stored preference and automatic startup checks. Use **Check for updates** when wanted.
- Make unavailable Download update buttons visibly grey and inactive. Enable downloading only after a check confirms a newer ready package; clear stale results when rechecking or changing GitHub access.
- Accept a complete application ZIP as the only release attachment using GitHub's asset checksum. Preserve download and internal package verification, and compatibility with correctly formed older releases.
- Explain no-release and not-ready-package states without asking ordinary users to create metadata files. Keep failed checks separate from confirmed up-to-date results.
- Make **Build Update Package.cmd** prepare one ZIP in a folder for its version and select the ZIP in File Explorer. Retain an optional legacy metadata output for older updater clients.
- Preserve existing download progress, cancellation, Save/Discard/Cancel, restart installation, project data and the two-item application folder.

---

# Version 0.4.14 — Default update repository

- Set **PositionV2024/studio** as the desktop application's default update repository.
- Fill the source automatically for new installations and older copies with no saved repository. Preserve a previously selected repository and the automatic-check preference.
- Keep the source editable in **Update**. Background checks use the configured source; downloading and restarting remain explicit actions.
- Retain the two-item application folder and update the application's package and launcher versions to 0.4.14.

---

# Version 0.4.13 — Updates from your GitHub repository

- Add an **Update** control at the far right of the main toolbar, available with or without a project open.
- Remember the desktop application's update repository independently from project publishing destinations. Accept an owner/repository name or GitHub repository URL.
- Check the latest stable GitHub Release against the installed version using numeric version comparison and the release's package metadata. Show release notes, last-check time, current, available and ahead states; never offer a downgrade.
- Keep checks and downloads off the UI thread, with progress, cancellation, retry and optional background checks at startup. No repository configured means no network request.
- Support anonymous public checks and downloads, plus private repositories through the existing built-in GitHub connection with read-only update permissions. No GitHub CLI, Git or new application dependency is required.
- Verify the release tag, package version, platform, layout, asset metadata, byte count and SHA-256 checksum. Validate every managed file before preparing an installation.
- Offer explicit Restart to update for compact Windows installations. Preserve Save/Discard/Cancel behavior, wait for application shutdown, retain the existing environment and user files, and keep recovery backups during installation.
- Include **Build Update Package.cmd**, the Python release builder and **UPDATING.md** for publishing compatible update assets through GitHub's website.
- Retain the two-item extracted application folder: **HoYo Project Studio.exe** and **app**. The first installation of 0.4.13 uses the full ZIP in a new folder; the new inventory enables subsequent automatic updates.

---

# Version 0.4.12 — Simple application folder

- Show only **HoYo Project Studio.exe** and **app** in the extracted application folder. The executable is the normal entry point; all supporting files stay together inside app.
- Retain the complete editor, Preview, Properties, publishing, templates, documentation, tests, repair tools and optional full-build command. Runtime environments and setup/build logs also stay inside app.
- Update the native launcher to select the app content folder explicitly. An incomplete app folder reports an error instead of opening stale files from the outer folder. Flat source layouts remain supported for maintainers.
- Update compatibility launch scripts, desktop shortcut targets and the native maintainer rebuild output for the new layout. Refresh Studio-created desktop shortcuts after successful setup; preserve custom shortcuts.
- Keep the existing installed-Python discovery and background setup flow. A new extracted copy prepares its own environment and can download required packages; no additional Python interpreter is downloaded.
- Deliver the complete ZIP for extraction into a new empty folder. The release does not move or delete the previous installation, saved projects or environments. Keep the old folder while checking the new copy because projects may be stored there.
- Preserve the existing project library, workspace settings and remembered GitHub credential locations.

---

# Version 0.4.11 — Included Windows executable launcher

- Include a compiled **HoYo Project Studio.exe** in the application folder and cumulative replacement ZIP. Double-click it immediately after extraction; no local EXE build or separate launcher runtime installation is required.
- Open the existing responsive setup window through Windows' own PowerShell. The launcher supports Unicode and spaces in its folder path, runs without a command window and shows an error dialog if required application files are missing or setup cannot start.
- Retain staged setup: reuse an installed compatible Python, prepare the editor essentials first, open Studio, then finish Preview support in the background. Existing suitable packages are reused and another Python interpreter is never downloaded.
- Point new desktop shortcuts directly to the included executable. Existing CMD/VBS launch files also prefer it, while keeping their older setup path available if the EXE has not been added yet.
- Include the native launcher's source, icon/version resources, manifest and rebuild helper for maintainers. The separate **Build Windows EXE.cmd** command still builds the larger distribution containing Python and Qt.
- Retain built-in GitHub connection/publication, completion and inline help, file controls, Test Script, Preview lifecycle, direct UI editing, Properties Save and unsaved-change prompts.

---

# Version 0.4.10 — Built-in GitHub connection and publishing

- Remove the external GitHub CLI and Git installation requirement from GitHub sign-in, local file review and upload. Publishing uses the GitHub API directly through Python’s built-in networking support.
- Add a guided connection dialog with a browser link to GitHub’s token form, a masked access-token field, repository-specific permission guidance, and connection validation outside the UI thread.
- Offer Windows Credential Manager storage with **Remember on this computer**. Show connected account/session status and **Disconnect**. Tokens are excluded from project files, command arguments, logs and release packages.
- Preserve file selection and the explicit review-before-publish workflow. Existing repositories receive a fresh update branch; unselected remote files and the project’s local Git state are preserved.
- Compute the review from an immutable snapshot. Verify the account, destination, visibility and remote base again before publishing; a changed destination or base requires another review.
- Read project ignore rules and a local origin URL directly, including when Git is absent. Continue excluding private files and local environments.
- Keep staged startup, background Preview setup, completion/help, Test Script, direct text editing, Properties/Save, and Windows build improvements from 0.4.9.

# Version 0.4.9 — Open Studio while Preview setup finishes

- Replace the interactive first-run terminal with a responsive native Windows setup window. Show indeterminate progress, expandable details, Retry and safe cancellation while finding installed Python and preparing the editor.
- Install or reuse the interface, Jedi completion and Pygments highlighting first. Verify those imports before automatically opening Studio. Launch and Setup use the same staged flow with no extra launch question.
- Finish Streamlit/Preview support in a background process after the main window appears. Keep browsing, editing and saving available, with an in-app progress banner, Details and Retry.
- Enable Run, Install Dependencies and Test Script automatically after the shared environment is verified ready. Preserve these controls for independent project environments while Studio's environment is still being prepared.
- Protect loaded editor and shared dependencies with exact version constraints during background installation. Download only missing/incompatible feature requirements; reuse suitable packages and existing Python.
- Require a matching readiness message and successful child exit before reporting success. Keep failures visible and prevent late completion from an older cancellation from stopping a retry.
- Preserve Save/Discard/Cancel handling before exit. Cancel leaves setup running; approved exit cooperatively stops the worker and its child installer before closing. Existing completed packages remain available on the next launch.
- Share one installed-Python discovery module between startup and EXE builds. Serialize installers/builds with an OS-managed lock and preserve the active build log when another build is rejected.
- Retain the existing completion, direct text editing, Properties, file management, visual Save, script testing, Preview lifecycle and publishing improvements. This release adds no application package requirements.

See **EDITOR_UPGRADE.md** for the new launch flow and **VERIFICATION.md** for release results and platform limits.

---

# Version 0.4.8 — Reuse installed Python for Windows builds

- Build Windows EXE no longer runs interactive Setup. It reuses a working application environment or creates a missing `.venv` from a compatible Python already installed on the PC.
- Discover actual executable paths through the Python launcher/install manager, PATH and known installation locations. Disable Python Install Manager automatic installation, clear legacy launcher install flags, and avoid Microsoft Store Python aliases.
- Check application and PyInstaller versions before package installation. Reuse suitable packages, install only missing or incompatible requirements, and avoid unconditional pip/PyInstaller upgrades. A first build may download packages but does not download a Python interpreter.
- Use PyInstaller 6.21 or newer so installed Python 3.15 is supported by the builder. Preserve incomplete/broken environments and report how to recover instead of replacing them automatically.
- Close the owned build command window after success and select the completed EXE in File Explorer. Running from an existing terminal returns control to that terminal. Failed builds keep their errors visible and preserve the subprocess failure status.
- Save progress and errors in `build_windows_exe.log`. Confirm a nonempty expected EXE only after PyInstaller succeeds, so an old output cannot hide a failed build.
- Retain the preceding editor, direct text editing, Properties, Save, Preview and publishing changes.

See **EDITOR_UPGRADE.md** for build/update steps and **VERIFICATION.md** for the checks and Windows testing limit.

---

# Version 0.4.7 — Direct text editing, Properties and publishing

- Click editable text in Edit Mode and type over it in the Preview. Enter or blur finishes the session; Escape restores the exact previous draft. A text session forms one visual Undo step, with live Properties and unsaved-status updates.
- Flush the browser's final text before Save, project changes, manual Preview closure, refresh, Publish and exit. A failed flush keeps changes open and prevents a partial save or shutdown.
- Preserve decorative children, input values and listeners while editing labels. Use plain-text paste, respect composition input, serialize edits across component frames, and reject stale browser-document messages.
- Rename the right panel and its panel-menu controls to **Properties**. Hide unsupported fields, their labels and empty groups. Keep the fixed Save footer and existing save/exit protections.
- Add **Publish to GitHub**, **Publish to Vercel** and **Publish to Streamlit Community Cloud** to the Publish menu.
- GitHub publication reviews selected files and an isolated commit before upload. New repositories default to private. Existing repositories receive a fresh update branch while the project's own index, remotes and local history remain intact.
- Add reviewed deployment-file generation for Streamlit on Vercel Container Runtime (Beta), respecting existing deployment files and configurable HTTP ports. Support Vercel import for existing compatible frontend/static folders too. Hosting remains an explicit account/browser action.
- Save selected-project source tabs and published UI settings before scanning files for publication. Exclude local environments, caches, private configuration and common secret files from new uploads.
- Retain prior file management, completion, integrated help, script testing and Preview lifecycle improvements. The editor adds no Python packages; GitHub publication requires installed Git and GitHub CLI.

See **EDITOR_UPGRADE.md**, **PUBLISHING.md** and **VERIFICATION.md** for usage and validation.

---

# Version 0.4.6 — Visual Save, unavailable properties, and exit protection

- Add a fixed Save button to the edit panel with Saved / Unsaved changes status. Pending typing enables Save immediately; clicking it commits the active text, colour, image source, or numeric field before writing.
- Keep visual edits in a working draft until explicit Save. Cache drafts and undo history per project so sidebar changes, preview closure, Stop, and Restart retain pending changes. Ctrl+S saves UI changes when Preview is active and saves source files in code tabs.
- Show a visible circle-slash indicator, forbidden cursor, accessible description, and explanatory tooltip for each unavailable property. The browser reports actual element capabilities; blocked fields cannot emit changes. Clear stale values when no element is selected.
- Preserve nested heading icons while editing text, and block changes that would replace child controls or modify live input values. Unsupported image/font/inline-dimension properties are identified accurately.
- Ask Save / Discard / Cancel before closing Studio with unsaved UI edits. Save All covers dirty projects beyond the selected one; Cancel keeps windows, processes, and buffers open; Discard leaves saved files untouched. Existing code-editor prompts remain in place.
- Save complete JSON atomically and detect external draft changes. Failed writes retain pending edits and prevent exit. Publish saves the working draft first and distinguishes a saved draft from a failed publish.
- Bind edits to the selected element's project and ignore stale selections from a different, stopped, or hidden preview.
- Restore removed visual overrides during Undo without reloading Streamlit. Original text nodes, image sources, CSS priorities, decorative children, and unrelated live state are preserved.
- Keep the prior preview lifecycle, script testing, completion, and file-management improvements. No new dependencies are required.

# Version 0.4.5 — Reliable preview loading and automatic tab closure

- Fixed a reproduced Qt/Python conversion problem: returning a JavaScript page-status object could produce an empty Python string, which the old loader misread as a blank document. Readiness now uses an explicit JSON result.
- Wait for Streamlit's rendered interface without repeatedly navigating during frontend startup. Cancel obsolete readiness callbacks and retries when loading another preview or closing the tab.
- Refresh reconnects to the actual project URL after a preview error. URL comparisons no longer reload a healthy preview solely because of a trailing slash.
- Close the Preview tab automatically when its server stops, exits normally, crashes, or fails to start. Code editor tabs and Script Test remain available.
- Run opens a new preview; queued Restart and Switch and Run open the next server's preview. Manually closing a preview keeps the server running and remains respected when startup finishes.
- Keep a starting preview attached to its running project when another library item is selected. Ignore readiness from a project that no longer owns the running process.
- Distinguish server availability from a displayed preview in Output and status messages.
- Fixed the preview's blocked WebChannel client request by injecting Qt's bundled JavaScript with the editor. Actual page selection now reaches the Python property inspector without changing Chromium security settings.
- This update adds no dependencies. See **EDITOR_UPGRADE.md** for update steps and a quick Run/Stop check.

---

# Version 0.4.4 — Test the selected Python script

- Added **Test Script** to the Python editor toolbar, with **F7** as its shortcut. The selected file's modified contents are saved before launch; a failed save prevents execution.
- Added a dedicated **Script Test** tab showing the script, streamed output, Python errors, and the final exit result.
- **Stop** and **Shift+F7** stop the active test. Input, Send, and End Input support scripts that read from standard input.
- Script execution uses a separate asynchronous process, so the UI stays responsive and the existing Streamlit preview can remain running.
- A test uses the open file's original project folder and interpreter even after selecting another project. A configured interpreter that is missing or unusable produces an actionable error.
- Only one script test runs at a time. Its stop control remains available when changing or closing editor tabs, and Studio stops the test process during application shutdown.
- Python-mode extensionless files are supported. The button is disabled for non-Python files. Completion, highlighting and the integrated documentation popup remain available.
- This update adds no dependencies. See **EDITOR_UPGRADE.md** for update steps and an interactive example.

---

# Version 0.4.3 — Highlighted completions with integrated documentation

- Moving the mouse over a suggestion selects and visibly highlights the row, including when the editor retains keyboard focus. Up and Down continue from the hovered selection.
- The completion popup now contains a documentation pane. Selecting an item shows its function signature and docstring beside the list. Documentation is requested only for the selected suggestion and follows both mouse and keyboard selection.
- Streamlit re-exports such as `st.bar_chart` resolve to their callable definitions, so a suggestion initially classified as a statement can still show its actual function signature and documentation.
- Removed the separate **Suggest** and **Python Help** toolbar buttons and generic row tooltips. **Ctrl+Space** still opens suggestions; **F1** and **Ctrl+Q** show help inside the same popup.
- The documentation pane supports scrolling without inserting a suggestion or moving focus away from the editor. The popup stays within the available screen area.
- Clicking a suggestion or pressing Tab/Enter inserts the highlighted item. Esc dismisses the entire popup; late completion or documentation results cannot reopen it or show help for a different selection.
- Retained immediate basic completion, builtin help when advanced analysis is unavailable, language selection and persistence, file controls, and editor save/undo behaviour. No new application dependencies.

See **EDITOR_UPGRADE.md** for update steps and the `st.bar_chart` example.

---

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
