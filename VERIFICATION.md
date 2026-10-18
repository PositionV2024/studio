# Version 0.4.15 verification

Verified on 18 October 2026. This release fixes the application repository to **PositionV2024/studio**, removes automatic startup checks, makes unavailable downloads visibly grey and disabled, and supports GitHub releases containing one complete application ZIP.

The full application regression suite passed **1,179 tests in 38.78 seconds**, with no errors, failures or skips. Real Qt WebEngine and temporary local Streamlit preview fixtures were enabled. The run covers the existing editor, Preview, Properties, publishing, launcher and installer along with the simplified update flow.

After that run, review identified a filename-filter edge case: a version component such as 0.4.32 could be mistaken for a 32-bit platform. That issue was corrected and **262 final service tests passed**, covering canonical and generic package names and legacy release metadata. These final focused checks overlap the full suite and are reported separately.

The update dialog and main-window integration passed **63 real Qt checks**. They verify fixed-source migration and typing rejection, no startup request beyond the old timer interval, absent/current/older/unavailable/error states, stale-result invalidation, worker cancellation, account changes and restart/unsaved-change handling. Disabled-button checks inspect actual rendered pixels and exercise hovering and clicking. Idle, unavailable and available dialog screenshots were visually inspected at 730 by 620 pixels.

The release builder passed **13 focused checks** for one-ZIP output, optional legacy metadata, version-specific output folders, Explorer selection, preserved older outputs, compact layout, internal inventory, reproducibility and excluded user data. A separate integration check generated a real complete 0.4.15 ZIP, checked its ZIP-only GitHub release information, streamed its actual bytes through the production downloader and validated the resulting archive. It used three offline HTTP-fixture requests; no external metadata file was created or requested.

The final delivered ZIP is also checked against the production archive validator and the previous complete 0.4.14 package. Every previously delivered application file is retained. The extracted root remains **HoYo Project Studio.exe** and **app**. The builder automatically records every managed file and checksum inside the package.

The Windows launcher was rebuilt twice with identical bytes and inspected as a complete Windows x64 GUI executable. Its native behavior, icon and DLL imports are unchanged. It is 75,264 bytes with SHA-256:

```text
20b85a398338a64744be4ce2c3f426cce9326c9705c686114f7638d0cd294e8c
```

All GitHub service checks used deterministic offline fixtures. The user's live release and private account were not accessed or changed. Windows installation and native relaunch were not executed on an actual Windows desktop in this environment. The established installer validation, filesystem transactions and recovery checks remain part of the regression suite.

---

# Version 0.4.14 verification

Verified on 18 October 2026. This release sets **PositionV2024/studio** as the default update repository for new installations and copies with no saved source. An existing source and startup-check preference are preserved.

**124 focused checks passed:** 52 updater-dialog and main-window integration checks, plus 72 release-package, compact-layout and native-launcher checks. The updated dialog cases cover missing, empty and whitespace-only settings; prefilled and editable sources; preservation of a custom repository; and enabled or disabled startup checking. Network behavior used offline release fixtures.

The complete ZIP was checked against the production update-archive validator, its embedded inventory and the previous 0.4.13 ZIP. All previous application files remain present. The outer folder still contains only **HoYo Project Studio.exe** and **app**. The application version, package metadata and executable resources agree on 0.4.14.

The native launcher was rebuilt twice with identical bytes. Its native behavior, icon and Windows DLL imports are unchanged. It is 75,264 bytes with SHA-256:

```text
6243e2dbe73900e4429e536f8c29d6f8c10b86572a74afa26a8cb2ce7b38f6e9
```

This focused follow-up did not rerun the full application suite or execute a Windows installation. Earlier full-suite evidence remains below as historical verification. The user's repository is configured in this build; publication of release assets and a live check against that repository were not performed.

---

# Version 0.4.13 verification

Verified on 18 October 2026. This release adds the top-right GitHub Update control, release version comparison, verified background downloads, restart installation and interrupted-update recovery. The complete ZIP retains every previously delivered application file and the two-item root: **HoYo Project Studio.exe** and **app**.

## Regression and final verification

The full regression suite passed **1,059 tests in 39.31 seconds**, with no failures, errors or skips. Real Qt WebEngine and temporary localhost Streamlit preview fixtures were enabled. The run covers editor completion and documentation, syntax highlighting, file operations, script testing, Preview lifecycle, direct visual editing, Properties Save, unsaved-change prompts, built-in GitHub publishing, startup, native launcher structure and the new update system.

After the full run, focused checks covered the final updater changes: Python bytecode-cache invalidation, preservation of the bundled template metadata and the exact finished release format. **138 final installer/startup/package/bootstrap checks passed**. These overlap with the full regression suite and are reported separately, not added to its test count.

A separate smoke check exercised the complete Studio window with isolated settings and offline release data. Its **five checks passed**: the right-aligned button is enabled without a project; it opens the actual update dialog; a worker updates the version labels, notes and toolbar; reopening retains the review; approved shutdown closes the updater and blocks new work. The updater dialog fits its controls without clipping.

| Area | Evidence and practical scope |
| --- | --- |
| Version checking | Numeric major/minor/patch comparison, current/ahead/available states, stable tags, package metadata and asset consistency, public and explicitly connected private sessions. |
| Network transport | Production HTTPS boundary tests cover cancellation, deadlines, bounded downloads, malformed responses, changed assets and SHA-256. Optional authorization is sent only to api.github.com, never to redirected asset hosts. |
| User interface | Real Qt event-loop tests cover responsiveness, progress, cancellation, changed sources/accounts, startup behavior, private sign-in shutdown, manual-archive validation and failed-helper retry. |
| Application close | Production MainWindow methods preserve Save/Discard/Cancel. A cancelled save keeps the prepared update; approved close drains workers and background setup before handoff. No updater dialog keeps a closed Studio session alive. |
| Package verification | ZIP paths, inventory, Windows naming, checksums, internal versions, allowed layout and native launcher are checked before automatic or manual installation guidance. The actual finished ZIP is passed through the production archive verifier. |
| Installation and recovery | Real filesystem transactions and subprocess checks exercise application/setup lock exclusion, parent-exit ordering, backups, handled rollback, interrupted-update recovery, journal tampering, local conflicts and preservation of unrelated user files. |
| Cached Python code | A real subprocess regression first reproduces stale same-size/same-second bytecode, then confirms the installed change imports correctly without deleting user caches. |
| Compact distribution | Every file from the complete 0.4.12 ZIP remains present. Required templates, compatibility scripts, updater, docs and release builder stay inside app. Environments, logs, generated output and QA files are excluded. |
| Native launcher | The actual Windows x64 GUI executable was rebuilt twice with matching bytes. PE structure, imports, manifest, version and production C path/argument helpers are checked. |

The native launcher is **75,264 bytes** with SHA-256:

```text
42f555c3f0b2a37fd417494051a2f88c31979288d6d0d31fc1da89976dc46a68
```

Its native build uses the existing verified MinGW-w64 toolchain. Qt testing used PySide6 6.12.0, Streamlit 1.66.0, Jedi 0.20.1, Pygments 2.21.0 and pytest 9.1.1 on Linux. These are the test environment's versions; normal setup retains the application's stated compatibility ranges.

## Platform and live-service limits

An actual Windows desktop was not available. Windows file-range locks, parent-process handles and native relaunch were reviewed and tested through substituted OS boundaries; the transaction and recovery subprocesses ran on Linux. The optional complete PyInstaller build was not produced in this run.

No user repository URL or credentials were supplied. GitHub behavior was tested with deterministic HTTPS fixtures. A read-only probe of an unrelated public release could not resolve api.github.com in this environment, so a live GitHub asset transfer is not claimed. Configure the application's own repository in the Update dialog and publish compatible release assets as described in **UPDATING.md**.

Version 0.4.13 is the first package with the updater inventory. Install it from the complete ZIP into a new folder when upgrading from an earlier version. Future automatic updates preserve the existing Python environment and application-data locations. Modified application files stop automatic replacement; unrelated user projects remain in place.

---

## Historical release verification

# Version 0.4.12 verification

Verified on 18 October 2026. The complete release now has exactly two top-level items: **HoYo Project Studio.exe** and **app**. All previously delivered application files are retained, with support files moved together under app in the download.

## Current release results

**134 focused checks passed:** 111 pytest checks in 1.02 seconds and 23 PowerShell checks, with no failures, errors or skipped pytest cases.

The pytest run used the actual staged compact package from an unrelated working directory. It includes 58 actual PE/production C helper checks, 25 startup checks, 22 Windows build decision checks and 6 compact-package bootstrap/path checks. The native binary tested is **75,264 bytes**, with SHA-256:

```text
ee8e2cf6caed8f68efafeabbdbdd91dc3f571a186a67d28fec4517f0e146c032
```

Two native builds in independent temporary folders produced identical bytes. An additional real rebuild from `app/launcher/native` under a path containing Unicode, spaces and an ampersand created the outer executable, produced no duplicate executable inside app and matched the release bytes. The Windows GUI PE imports only KERNEL32/USER32 and embeds the existing application icon plus version 0.4.12 resources and manifest.

| Area | Current verification |
| --- | --- |
| Folder contents | The package root contains only the executable and app; every previous application file is retained at its new mapped path. |
| Native content selection | Production C helper tests cover compact and legacy roots, Unicode, drive/UNC paths and bounded buffers. The executable selects app when present and reports incomplete or inaccessible content rather than silently running older outer files. |
| Python entry point | Execute the actual packaged launcher.pyw and application.run after copying to an independent Unicode/space path and starting from an unrelated directory. Verify imports, working directory, icon path, argument handling and deferred background setup. Qt window objects are substituted at the UI boundary. |
| Resources and templates | Resolve required icons, Preview helper, starter files and the PyInstaller specification's build inputs from inside app. The PyInstaller build API is substituted for the input check. |
| Setup and build paths | Real helper entry points keep logs and lock paths inside app; current setup/build regressions retain Python reuse, readiness checks, cancellation and installation coordination. |
| Shortcuts | Compact, flat and legacy targets resolve correctly. Standard native/VBS shortcuts from older folders refresh to the new executable. Six custom-link variants are preserved. Explicit shortcut replacement remains available. |
| Startup handoff | Actual PowerShell handoff functions keep the selected content root, pass --background-setup, close setup after launching the editor and tolerate shortcut failures. Windows process/COM/WinForms boundaries are substituted. |
| Existing features | Byte comparison with 0.4.11 shows all studio modules unchanged except the version in studio/__init__.py. Completion, file controls, Test Script, Preview, Properties Save and GitHub publishing retain their implementation. |
| Stored user state | Audit confirms the project library, workspace settings and remembered GitHub credentials retain their existing per-user identities and locations; project records retain absolute paths. |

PowerShell verification used an official, checksum-verified PowerShell 7.4.2 Linux runtime. All four production scripts passed parsing and 23 shortcut/startup semantic checks. Changed syntax and .NET APIs were reviewed for Windows PowerShell 5.1 compatibility.

Packaging compiles every Python/launcher/build-spec source, checks Windows script line endings, compares tested native/PowerShell source hashes, validates ZIP CRC and exact content, and verifies that all previous application files are included. Toolchains, environments, generated logs, object files and internal QA reports are excluded from the ZIP.

## Installation and data boundaries

This release is delivered as a **complete ZIP for a new empty folder**. It does not reorganize, remove or overwrite the user's old installation. The older replacement ZIPs target the earlier layout and are not used for this compact release.

Projects can exist inside the old installation folder, so that folder must be kept while checking the new copy. Existing project paths and environments are not moved. The new app copy prepares its own environment using installed Python and may download required packages for it. It never downloads another Python interpreter. Setup-generated environments and logs stay inside app.

## Platform limits

A native Windows desktop launch was not performed. The actual Windows executable was cross-compiled and inspected, while its C path helpers ran on Linux. The bootstrap tests substituted Qt window objects; PowerShell tests substituted native COM, process-start and WinForms boundaries. Windows PowerShell 5.1 and a full Windows PyInstaller build were not executed. The included executable is unsigned.

The first full Studio window still needs the core interface/editor dependencies. The initial setup window opens first, then remaining Preview support installs after Studio opens. No live GitHub repository or hosting deployment was changed during verification.

## Historical verification

The results below document earlier releases. They are retained as historical evidence and are not counted as new checks for version 0.4.12.

---

# Version 0.4.11 verification

Verified on 18 October 2026 in Linux with CPython 3.12.14, pytest 9.1.1, GCC 13 MinGW-w64 and Binutils 2.41.90. The Windows launcher was actually cross-compiled and is included in both release ZIPs.

## Current release checks

**89 targeted tests passed in 0.72 seconds, with no failures, errors or skips.** These include 47 existing startup/build regressions and 42 new native-launcher integration cases. Existing editor, Preview, Properties, publishing and background-setup application bodies are byte-for-byte unchanged from version 0.4.10; the application version is updated.

The delivered **HoYo Project Studio.exe** is a **74,240-byte AMD64 PE32+ Windows GUI executable**. Two builds in independent temporary directories produced identical bytes. Its SHA-256 is:

```text
95f6d3dbe771f35b77b22ac5f17dc50df2524f84e661d8a4b46b8912059a8d1a
```

The compiler packages came from the official Ubuntu 24.04 archive. The archive signature and package hashes were verified before extraction. The compiler was used only to produce the included executable; end users do not need a compiler or a separate launcher runtime.

| Area | Verified result |
| --- | --- |
| Actual Windows binary | Valid x64 GUI executable with an entry point in executable code, no writable code section, and ASLR/NX/high-entropy address randomization enabled. |
| Runtime dependencies | The shipped binary imports only `KERNEL32.dll` and `USER32.dll`, has no delayed external imports and has no CLR header. No Python, MinGW or Visual C++ runtime DLL is needed by the launcher itself. |
| Explorer presentation | The complete original application icon is embedded. File/product version resources and the manifest match application version 0.4.11. |
| User permissions | The embedded manifest requests the current user's permissions, without an administrator elevation request. |
| Real argument helpers | Host-compiled C tests execute the same header used by the Windows build. They cover Unicode and surrogate units, spaces, shell metacharacters, embedded quotes, backslash runs, drive roots, UNC paths, exact capacities and guarded overflow. |
| Startup handoff | Reviewed the native path to system PowerShell, explicit application root, `-STA`, no inherited handles and console-free process creation. A fresh startup structure does not hide the setup form's first window. |
| Existing setup flow | Current regressions retain installed-Python reuse, selective package preparation, verified core readiness, protected editor dependencies, cancellation, installation locking and failure handling. |
| Existing launch entries | CMD, VBS and desktop-shortcut paths prefer the included executable. Their older script path is retained only when the executable is absent. |
| Reproducible output | Matching source, icon, resources and compiler produce identical executable bytes; build timestamps are zero. |

The native launch uses `CREATE_NO_WINDOW` to avoid a console. It does not combine this with `STARTF_USESHOWWINDOW/SW_HIDE`, which could affect the first setup-window display. This design follows Microsoft's [process-creation flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags) and [ShowWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-showwindow) documentation. Argument and artifact checks use Microsoft's [argument parsing rules](https://learn.microsoft.com/en-us/cpp/c-language/parsing-c-command-line-arguments?view=msvc-170) and [PE format](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format).

Both packages include the new executable and its source/rebuild helper. Packaging checks compile all Python/launcher/build-spec sources, validate ZIP CRCs and content, retain all previous application files and verify that the cumulative replacement ZIP reproduces the full current application when applied to the original package. Temporary toolchains, environments, logs, object files and internal QA reports are excluded.

## Current platform boundaries

A Windows desktop session was not available. The actual executable was compiled and inspected, but the Windows Forms window, native process launch and real Windows package installation were not run here. The optional full PyInstaller application build was also not performed. The native launcher is unsigned.

The first full Studio window still requires the interface/editor packages to be installed. The initial setup window can open before those Python packages are ready; remaining Preview support then installs while Studio is open. Existing GitHub and hosting behavior is retained; this update performs no live publication.

## Retained application verification

The earlier **685-test application suite** and **8-check GitHub GUI smoke** below were performed for **0.4.10**, before this launcher-only update. They are historical evidence, not additional tests counted in the 89 current checks. Earlier 0.4.9 startup GUI and PowerShell results are also labeled as historical.

---

# Version 0.4.10 verification

Verified on 17 October 2026 with Python 3.12.14, PySide6 6.12.0, Jedi 0.20.1, Pygments 2.21.0 and Streamlit 1.66.0 in a Linux test environment.

## Release results

The complete combined suite passed: **685 tests in 34.41 seconds**, with real Qt WebEngine and generated Streamlit launcher checks enabled. There were no failures or skipped tests. The GitHub changes include **88 connection/credential tests**, **72 local ignore/origin tests**, **52 new direct API publishing tests**, **34 ported publishing behavior cases** and **10 new Qt connection-dialog cases**. Earlier editor, Properties, Preview, publishing-dialog, startup and build regressions remain in the combined suite.

A separate smoke test exercised the production MainWindow, PublishDialog, GitHubConnectDialog, native Qt mouse events, real worker threads and the production HTTPS client: **8 checks passed in 0.648 seconds**, with version **0.4.10** and process exit code 0. The HTTPS connection boundary supplied controlled GitHub responses. PATH was empty and the native publishing command runner was configured to fail if called. No external commands, real account access or live publication occurred.

The smoke covered local scanning and origin detection, excluded private/ignored files, token expiry and retry, connected account state, read-only review, exact reviewed uploads, preservation of the default branch and unselected files, original Git/index preservation, deployment handoff, token exclusion, snapshot cleanup and Disconnect. Five PNGs were generated and checked for integrity; the connection, review and successful publication windows were visually inspected.

The **0.4.9** startup GUI verification remains available as historical evidence: **10 checks in 2.827 seconds** with five harmless setup child processes and an independently executed Python script during setup. Its **20 PowerShell checks** used official Microsoft PowerShell 7.4.2 on Linux. The Windows launch/build scripts are unchanged in 0.4.10; their automated backend regressions ran again in the current full suite.

All packaged Python, launcher and build-spec sources compile. Both ZIPs pass CRC validation; the complete package retains previous files, and applying the cumulative replacement ZIP to the original complete package reproduces the current complete package exactly. Environments, setup/build logs, runtime lock files, generated executables and internal QA artifacts are excluded.

## Built-in GitHub connection and publication

| Area | Verified behavior |
| --- | --- |
| No external applications | Scan, review and publication complete with PATH empty and native process creation forbidden. No Git, GitHub CLI, browser cookie or ambient token is required. |
| Connection UX | Explicit browser action opens GitHub's token form. Masked token validation runs on a worker thread, clears the field, reports invalid tokens, permits retry, and waits for cancellation before closing. |
| Secure transport | Authentication uses verified TLS to api.github.com and an Authorization header. Cross-host paths, credential-bearing query strings, redirects and malformed payloads are rejected. Raw response/transport errors cannot disclose the token. |
| Network control | Request/response limits, DNS and socket deadlines, owned-socket cancellation, large upload chunks, and late DNS completion are tested. Write requests are never automatically retried. |
| Windows credentials | Real ctypes structure and WinCred argument layout are checked with fake native functions. Save, load, deletion, buffer clearing, expiry, failed storage, account replacement and session-only behavior are exercised. No plaintext fallback is used. |
| Shared session | A restored connection remains unverified until checked. Review updates account status without an extra authentication request. Revoked credentials clear account state; disconnect and newer connections invalidate older clients. |
| File exclusions | Nested ignore rules, recursive patterns, negation, escaping and parent-directory exclusions work without Git. Origin detection reads only bounded local metadata, including linked worktrees, and does not run includes or credential helpers. |
| Read-only review | Review performs GETs only and constructs an immutable file snapshot. Text changes, binary/large files, sizes and modes are represented; source changes after scanning require a fresh review. |
| Exact tree | The remote base tree plus selected reviewed changes determines an expected Git tree hash. Returned trees, blob hashes, commit parents and final branch revisions are verified. A separate local-Git comparison confirms the tree algorithm for nested and Unicode names, binary files, scripts and symlinks. |
| Existing repositories | Unselected remote files and default-branch history remain intact. A fresh update branch receives the reviewed snapshot. Windows preserves an existing remote script's executable mode. |
| Changing destinations | Account ID/name, repository ID/address, visibility, default branch and base commit are checked again before the final branch publication. Changes require a new review. |
| Empty repositories | One actual reviewed regular file initializes the repository, followed by the reviewed complete tree. Partial failures explicitly identify possible created repositories, commits or branches. An unexpected initial branch choice stops before uploading files. |
| Conflict handling | Symbolic links, submodules, file/folder collisions, truncated remote trees, LFS requirements, forged paths, changed snapshots and permissions errors stop publication with an actionable message. |

Official contracts: [GitHub REST authentication](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api), [API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions), [personal access token permissions](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens), [Git database workflow](https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-your-git-database), and [Windows CREDENTIALW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw).

## Staged startup and background setup

| Area | Verified behavior |
| --- | --- |
| Existing Python | Reuse a valid application environment or create a missing one locally from an already installed compatible interpreter. No interpreter installer or version selector is invoked. |
| Core stage | Prepare PySide6, Jedi and Pygments first; verify QtWebEngine, QtWebChannel and editor imports before emitting core readiness. Preserve incomplete or incorrectly owned environments. |
| Native startup window | Parse and review the responsive WinForms timer/process flow, details, Retry and cancellation. A matching core-ready message and successful process exit are both required before handing off to Studio. |
| Usable editor | Type and save through the real code editor while the asynchronous setup worker remains active. Completion/highlighting packages are part of the earlier core stage. |
| Shared environment | Temporarily restrict Run, Install Dependencies and Test Script when they would use Studio's environment. Handler checks enforce the same restrictions as the buttons. |
| Independent environment | A project with its own virtual environment retains its controls and successfully runs a script during background setup. Switching back restores the shared-environment restrictions. |
| Package protection | Pass exact constraints for existing editor and shared packages to pip. Only non-GUI Streamlit/Requests entry points may update. Clear inherited pip flags that could force upgrades, reinstalls or global targets. |
| Readiness | Do not unlock features from an early message while the child is still active. Exit zero without a matching ready event remains a failure. Reject stale/wrong-phase messages. |
| Failure and Retry | Preserve file editing and show Retry after a failed download. A retry starts a fresh worker with fresh receipt/error state. Status-bar text agrees with the banner. |
| Cancellation and exit | Cancel in the unsaved-changes prompt preserves the editor and worker. Approved Save/Discard finishes that choice, requests installer cancellation and waits for cleanup before closing. |
| Process ownership | Real POSIX process tests stop a worker and its descendants without affecting an unrelated process. Windows cancellation targets the owned process tree. Delayed force-stop completion validates both the run generation and worker PID before acting. |
| Installation lock | Actual cross-process contention prevents overlapping setup/build workers. The lock covers venv creation, installation and EXE packaging. A rejected second build preserves the active build's log. |
| State and logs | Stream JSON lines without blocking the Qt event loop, handle split output and final lines without newlines, retain readable logs, and write the full readiness marker atomically only on verified success. |

Four startup screenshots from 0.4.9 were checked for PNG integrity and visual layout. The banner, progress indicator, code editor, ready controls, error state and Retry button fit the production window. The independent-project image shows an actual successful script run during background setup.

## Retained Windows build behavior

The build helper retains installed Python reuse, selective package installation, UTF-8 output, nonzero failure propagation, rejection of missing/empty output, protection against a stale EXE hiding a failed build, and Explorer handoff after success. The CMD success path returns without a pause; failures remain visible. A failed second build cannot truncate a running build's log.

The builder requires `PyInstaller>=6.21,<7`. The official [PyInstaller change log](https://pyinstaller.org/en/stable/CHANGES.html) records Python 3.15 support in 6.21. The [Python Windows documentation](https://docs.python.org/3/using/windows.html) documents installed-runtime listing and the manager/legacy launcher installation controls used during discovery.

## Platform and test boundaries

The new backend tests simulate Windows interpreter and installer endpoints, with real package-parser, subprocess and locking tests where supported. PowerShell parser/runtime checks ran on Linux. **A native Windows Forms desktop session, Windows package installation and Windows EXE compilation were not performed here.** Those final platform-specific operations require a Windows PC.

The GUI smoke uses harmless controlled child commands, not live package downloads. The first-ever interface download must still finish before the full Studio window opens. Exact package constraints can expose a dependency conflict; the error provides an explicit close-Studio repair command instead of changing packages loaded by the editor.

Cloud responses are simulated. No GitHub repository was created or pushed and no Vercel/Streamlit deployment was performed. The following application regression areas are included in the current 685-test suite. Earlier standalone visual inspection is labeled separately.

## Direct editing and Properties

| Area | Verified behavior |
| --- | --- |
| Single-click text editing | A native browser click opens the text editor over a heading. Typing changes the preview immediately and updates Properties and dirty state without a duplicate DOM patch on each key. |
| Final characters | Clicking Save while the browser editor remains active flushes the last queued character before writing. Enter/blur commit, and nested component-frame edits are included. |
| Escape | Cancel restores the exact previous draft, including absence of a prior text override, existing unsaved property changes and the previous redo chain. |
| Undo and no-op edits | A typing session is one visual Undo step. Redo restores its final text. Typing back to the original full text removes a temporary override and retains the prior history/dirty state. |
| Input behavior | Plain-text paste does not insert HTML markup. Enter during composition does not prematurely close the editor. Shift+Enter permits a line break. |
| DOM preservation | Nested heading anchors, SVG icons, button listeners and unrelated live input state remain intact. Unsupported containers and live controls cannot be replaced through text edits. |
| Frame/document ownership | An old frame finishes before the next starts. Nested-frame flushes complete, and stale document tokens cannot write into a new preview. |
| Project ownership | Changing projects flushes to the original project's store. Late element callbacks cannot finish another element's session or write into the newly selected project. |
| Properties filtering | The fixed heading reads Properties. Unsupported controls and labels, plus groups with no applicable fields, are hidden. Image selection hides text/font fields and shows the applicable source/layout settings. |
| Properties synchronization | Inline text updates do not disturb a pending color field. Final draft text corrects an accepted stale selection description. Text longer than the native 32,767-character default is preserved through selection, typing and Save. |
| Saved draft persistence | Native Save updates the footer and disk. A fresh SettingsStore and a fresh full application both load the saved result. |
| Failure handling | A failed browser flush prevents Save, Publish, manual navigation/closure and application shutdown. Failed or conflicting disk writes keep the working draft available. |

The Properties and MainWindow direct-edit suites contain **29 and 21 tests** respectively. Additional browser/bridge tests and **12 SettingsStore transaction tests** cover asynchronous messages, history and persistence.

## Publishing

The combined GitHub and publishing areas contain **275 tests**: 86 backend cases, 88 connection/credential cases, 72 ignore/origin cases, 21 Qt dialog cases and 8 MainWindow integration cases.

| Area | Verified behavior |
| --- | --- |
| Selected project preparation | Real modified source documents for the selected project are saved before the local scan. Another project's dirty source and UI buffers remain unsaved. Clean source tabs are not rewritten. |
| Visual settings input | The final Preview character and focused Properties value reach the published settings before dialog construction/scanning. Source, draft, published-file or browser-flush failures prevent opening the dialog. |
| Review before upload | Local scanning starts without GitHub account/network access. The final Publish button stays disabled until a prepared snapshot exists. Changing destination or file selection invalidates that review. |
| New repository | A reviewed private/public repository plan contains the selected snapshot. Private is the UI default. Repository creation, file/tree/commit requests and branch publication are exercised through simulated GitHub responses. |
| Existing repository | A new update branch retains the reviewed default-branch history and unselected remote files. An existing nonempty default branch is never overwritten. |
| Original Git state | Read-only local scanning and temporary snapshots preserve the project's own staged index, configuration, remotes and commits. |
| Exclusions | Project ignore rules, private/local configuration, credentials, private keys, caches, virtual environments and unsafe paths are excluded. Published UI settings remain an explicit reviewed input. |
| Snapshot validation | Changed source hashes require a new review. Final account, snapshot, repository, visibility and base-commit changes prevent publishing the wrong snapshot or destination. |
| Progress and cancellation | Workers keep the GUI responsive. Failure/cancellation reports possible completed creation side effects, preserves a reviewable destination and does not silently retry remote actions. |
| Deployment handoffs | GitHub/Vercel/Streamlit options open the intended local tab. Invalid destinations and browser-opening failures are reported. Vercel receives reviewable repository, branch and root details. |
| Streamlit entry point | Vercel has a visible entry-point field synchronized with the Streamlit Cloud field. Deployment-file preparation uses that value. Nested-root preparation cannot silently write into the project root. |

## Vercel deployment-file preparation

The container module contains **55 tests**, including a real generated Streamlit process.

Preparation only reads the project and displays all four proposed files. Cancelling the review creates nothing. Adding the reviewed files creates them exclusively and rescans them into the GitHub selection. The Vercel UI then detects Container Runtime (Beta).

Checks cover existing deployment files and custom ignore files, symlink/path boundaries, traversal and argument-like entry points, Unicode/non-BMP filenames, requirements and pyproject dependencies, exact reviewed installation, and rollback after partial failure. A deployment conflict appearing during the writes is detected and preserved. Rollback does not delete a file concurrently replaced by another writer.

The generated launcher starts real Streamlit on a temporary assigned `PORT`, answers `/_stcore/health`, and terminates without leaving a process behind. Unit checks verify valid/default/invalid port handling, an argument-list `execv` launch, and preservation of normal CORS/XSRF settings.

Vercel's official [Container Runtime](https://vercel.com/docs/functions/runtimes/container) documentation supplies the detected Dockerfile name and HTTP-port contract. Its [WebSocket documentation](https://vercel.com/docs/functions/websockets) explains connection lifetime and instance behavior. The Streamlit container route is an implementation based on those server contracts, not a remotely verified deployment of the user's application.

## Retained regressions

The final suite also includes the preceding releases' file creation/import/recycling, source-buffer save and undo, Python language selection, highlighting, completion hover/documentation, background helper cancellation, selected-script testing, preview loading and process lifecycle checks.

Preview coverage retains delayed Streamlit rendering, browser JSON conversion, refresh/recovery, document ownership, automatic tab closure on stop/exit/failure, manual-close behavior, restart and project switching. The browser fixture uses generated loopback content and confirms actual Streamlit interaction through the embedded view.

Script checks run real child scripts with stdout/stderr, input, failure, stop and cleanup behavior. Completion checks exercise installed Jedi and Streamlit symbol documentation as well as missing/stalled helper fallbacks. Visual save tests retain atomic writes, external-conflict detection, saved/dirty state, per-project caches, Save/Discard/Cancel exit choices and failure protection.

## Previous visual inspection — 0.4.7

The visual inspection in this section predates the new startup flow. Current startup-window inspection is recorded above.

The actual MainWindow was inspected at 1620 × 1050 with the inline editor, selected heading, image Properties, fixed Save footer and reopened saved page. Properties was also checked in a compact panel. Unsupported fields leave no visible empty rows or labels.

The GitHub, Vercel and Streamlit dialogs were opened through production MainWindow actions and inspected. File/review/progress areas fit their windows, and disabled publishing controls are visibly muted. Remote commands and browser handoffs were guarded during these app-level inspections.

Browser sandbox-disabling flags appear only in isolated test harnesses where the container requires them. Production code does not alter Chromium security defaults.

## Practical limits

This environment did not run a native Windows desktop session, execute Windows Credential Manager against a real user credential, build a Windows executable, or publish to a real GitHub/hosting account. Version 0.4.10 replaces the GitHub CLI/Git requirement with built-in authentication and direct API publication. Windows credential calls and GitHub responses are simulated; their actual platform/service execution remains a final user-environment check. The user's own project dependencies, private data and external services were unavailable for an end-to-end production run.

Vercel Container Runtime and WebSockets are beta features. Reconnection or instance restart can reset an in-memory Streamlit session. The generated Python 3.12 image may need project-specific Python versions, Linux packages and dependencies. Private values require the hosting service's secrets/environment setup.

Visual edits are settings overrides, not arbitrary source-code rewriting. The included demo reads `.studio/published.json`; an imported application must consume those settings or integrate the runtime helper for overrides to appear outside Studio. Stable element IDs are advisable when page structure changes.

Test Script executes its selected file as a normal Python program. Exit code 0 is a process result, not proof of correct output or automatically discovered pytest coverage. Syntax checking does not replace runtime testing or type checking. Advanced completion depends on the selected project's installed packages; limited basic suggestions remain available when it cannot resolve them.

## Run the checks locally

The focused startup and build helper checks use simulated Windows installation endpoints and real harmless child processes:

```powershell
python -m pytest -q tests/test_startup.py tests/test_background_setup.py tests/test_windows_build.py
```

For the complete development test suite, install the application requirements and pytest. A small number of developer reference tests use native Git to compare commit/tree behavior. Install Git only if you want to run those reference tests; Git is not required for using Studio or its GitHub publisher. Run from the application folder:

```powershell
.\.venv\Scripts\python.exe -m pip install pytest
$env:HPS_RUN_WEBENGINE_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -q
```

The opt-in flag enables real Chromium/local HTTP and generated Streamlit-launcher checks. Without it, the standard suite skips those optional runtime fixtures. Cloud responses are simulated in both modes; running the suite does not publish remotely.
