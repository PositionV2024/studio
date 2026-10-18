# Opening HoYo Project Studio — version 0.4.15

## Open this file

Double-click **HoYo Project Studio.exe** in the extracted `hoyo_project_studio` folder.

That folder contains only two items:

| Item | Purpose |
| --- | --- |
| **HoYo Project Studio.exe** | Open the application. This executable is already built. |
| **app** | Required application files. Keep this folder beside the executable. |

All source code, icons, templates, guides, tests and optional maintenance tools are retained inside app. You do not need to open that folder for normal use.

## Install this update

1. Close Studio and save any pending changes.
2. Download the **complete application ZIP** for version 0.4.15.
3. Extract it into a **new, empty folder** and open the extracted `hoyo_project_studio` folder.
4. Double-click **HoYo Project Studio.exe**.

Extracting over the previous layout would leave its old files visible, so this update uses a fresh folder. Older replacement ZIPs are for the previous layout.

Your saved project library, workspace settings and remembered GitHub connection retain their existing locations. Projects open from their original saved paths. Keep the old installation in place while checking the new copy, especially if your projects or their Python environments are stored inside that folder. This package does not move or delete them.

## What happens at startup

A small setup window appears first. It finds your existing compatible 64-bit Python 3.11 or newer, prepares the interface and editor essentials if needed, and opens Studio automatically. The remaining Preview support then finishes installing in the background while you browse, edit and save. Studio shows progress, **Details** and **Retry** when needed. The initial setup window closes after handing off to Studio.

The new copy keeps its environment and setup logs inside app, so the outer folder remains simple. Preparing a new environment may download its required packages. Setup never downloads another Python interpreter. On subsequent launches, suitable installed packages in this copy are reused.

After successful setup, the standard desktop shortcut created by Studio is refreshed to open this copy. An existing custom shortcut is preserved. You can also keep opening the executable directly.

## If a download fails or you close Studio

Editing stays available when remaining Preview setup fails; use **Retry** to try again. Run and Test Script for Studio's shared environment become available after setup confirms readiness. Projects with their own usable independent environments can run during the background stage.

Closing Studio retains the usual Save/Discard/Cancel prompts. Cancelling the exit keeps both the app and setup running. Approving the exit stops setup safely before closing. The next launch checks the packages and prepares what is still missing.

If the executable reports missing support files, extract the complete ZIP and keep app beside the executable. Do not run the executable directly from inside the ZIP.

## Optional maintenance tools

All paths below start from the extracted `hoyo_project_studio` folder.

| Path | Purpose |
| --- | --- |
| `app/Create Desktop Shortcut.cmd` | Explicitly create or replace the standard desktop shortcut |
| `app/Setup HoYo Project Studio.cmd` | Open the same setup and automatic launch flow |
| `app/Repair Python Completion.cmd` | Repair editor completion packages in this copy's environment |
| `app/Build Windows EXE.cmd` | Build a separate distribution containing the editor's Python and Qt runtime files |
| `app/README.md` and `app/EDITOR_UPGRADE.md` | Application and editor instructions |
| `app/PUBLISHING.md` | GitHub and hosting instructions |
| `app/launcher/native/` | Native launcher source and maintainer rebuild instructions |

The optional full build creates `app/dist/HoYo Project Studio/HoYo Project Studio.exe`. Its complete generated folder must stay together because the editor needs the accompanying runtime files. For normal startup, use the executable already present in the outer folder.

For manual Python commands or source-based maintenance, open a terminal **inside app**. The existing `main.py`, `requirements.txt`, `launcher` and `studio` paths are all relative to that directory.
