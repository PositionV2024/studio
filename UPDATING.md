# Updating HoYo Project Studio

## Check, download, restart

1. Open **Update** at the far right of Studio's top toolbar. Studio uses your
   connected GitHub account; connect one here if necessary, then select
   **Check for updates**.
2. Review the version result and **File changes**. Checking fetches the release
   package in the background for comparison. If a newer version is ready,
   **Download update** becomes available. Select it to prepare the update;
   Studio can reuse the verified package from the check.
3. Select **Restart to update** when you are ready to install. Studio asks
   about unsaved changes before it closes and opens the updated application.

The application repository is fixed to
[PositionV2024/studio](https://github.com/PositionV2024/studio). It is displayed
as text and cannot be changed in the update window. Any source saved by an
older version is replaced with this repository. Project publishing destinations
are separate.

Checks start when you press **Check for updates**. The startup-check checkbox
and automatic startup requests have been removed, including the old saved
preference.

| Situation | Download update |
| --- | --- |
| No GitHub account is connected | Greyed out and disabled; connect GitHub first |
| Before checking or while a check is running | Greyed out and disabled |
| The installed version is current or newer | Greyed out and disabled |
| No stable release or ready application package is available | Greyed out and disabled |
| The version check fails | Greyed out and disabled; Check for updates lets you retry |
| The release ZIP is invalid or incomplete | Greyed out and disabled; File changes explains the package problem |
| A newer compatible package is ready | Enabled |
| A download is in progress | Disabled until the operation finishes |
| The download has been verified and prepared | Restart to update becomes available |

The dialog shows the installed version, the latest release when available,
file changes, release notes, progress and the last-check time. Version numbers
are compared numerically; equal and older versions cannot be installed as
updates.

The fetched release version and notes remain visible while the file comparison
runs. A package or download failure keeps that known release visible and shows
its reason inside **File changes**, instead of resetting the release to
**Not checked**. A new check is needed after a failed download or preparation.

## Compare the release with your desktop files

**File changes** is the default tab. The comparison direction is from the
saved desktop application to the latest published release package:

| File status | Meaning |
| --- | --- |
| Added | The release includes a managed application file absent from this copy |
| Modified | The desktop and release file contents differ |
| Removed | An application file owned by this copy is absent from the release |
| Unavailable | A local file could not be read or compared safely |

The summary gives change counts and the number of unchanged files. An
identical package explicitly shows **No file differences**. A failed or
cancelled comparison explains what happened; it does not show a blank result
or imply that the files are identical.

Select a text file to see its line differences. Red lines belong to the
desktop copy; green lines belong to the release. Unchanged lines provide
context. Binary files, large files and files that cannot be read have a
short explanation instead of line differences. Files are compared using
their actual saved contents, so saved edits to application source files are
included. Unsaved editor text is not part of the comparison.

Only application files are compared. Projects, Python environments, logs,
workspace settings and unrelated files are excluded. The comparison does not
change or delete local files, and does not prepare an installer. A listed
local application edit can still block automatic installation until its
conflict is resolved.

The comparison remains available when the online release is the same version
or older than the desktop version. For example, desktop 0.4.16 can be compared
with release 0.4.13, but **Download update** remains disabled. An unavailable
comparison is explained separately from the version result; it is never
displayed as an empty list meaning no changes.

Checking needs the complete release ZIP to compare its files, so the dialog
shows transfer and verification progress and lets you cancel. A newer
package already downloaded for comparison can be reused after its size and
checksum are rechecked. Choosing **Download update** then prepares that
package, and **Restart to update** remains the explicit installation step.
Changing the connected account invalidates the reviewed result and its cache.

## ZIP names and enclosing folders

From **0.4.18**, the ZIP filename does not need to contain the application's
name. Names such as `Studio.zip`, `My application (1).zip` and `0.4.18.zip`
are accepted when they contain the complete application. The contents can
appear directly inside the ZIP or under one unambiguous enclosing folder,
including nested enclosing folders. That folder does not need to be named
`hoyo_project_studio`.

Inside the application, keep **HoYo Project Studio.exe** beside the complete
**app** folder. Its included package inventory and checksums must match the
files, and the package version must match the release tag. The updater checks
these contents before comparing or preparing an installation. Renaming the
ZIP or its enclosing folder does not change the application identity.

Use **app/Build Update Package.cmd** after changing application source files.
It creates the complete ZIP and updates the included file inventory. Upload
that ZIP as a release attachment; the source-code downloads automatically
provided by GitHub are not the packaged desktop application. A ZIP containing
only source files, only the app folder, or only the executable is incomplete.
The error now identifies missing files and explains how to package the release.

Prefer one complete ZIP attachment. If a release contains several ZIPs, Studio
first recognizes the normal versioned build filename, then the complete ZIP
supplied in this conversation. It does not guess between multiple other ZIPs
from words in their filenames. Keep unrelated files and other application
copies outside the release ZIP.

Versions through **0.4.17** still use the old enclosing-folder rule. Install
this complete **0.4.18** package once to receive the corrected updater.

## The earlier missing-file message

Versions 0.4.13 and 0.4.14 required a separate `studio-update.json` attachment
in every GitHub release. This caused the missing-file message even when the
complete application ZIP was already attached.

Versions **0.4.15 and later** support releases containing **just the complete application
ZIP**. GitHub's release information supplies the download size and SHA-256
checksum. The updater verifies these and then checks the package's internal
files, inventory and version before preparing installation. A newer release
without a ready application ZIP leaves Download update disabled.

Install this complete 0.4.18 ZIP to get the corrected ZIP handling, publishing picker and file
comparison: save and
close Studio, extract the ZIP into a **new empty folder**, and open
**HoYo Project Studio.exe**. Keep the previous folder while checking the new
copy, especially if projects were created inside it. Subsequent compatible
releases can use the Update button.

## GitHub access

Update checks and downloads require Studio's connected GitHub account,
including for public releases. The optional account checkbox has been removed.
An existing connection from publishing is reused automatically; there is no
separate updater login or anonymous fallback. Check for updates and Download
update are disabled while disconnected. A disconnected, expired or changed
connection requires signing in again or checking again before continuing.

The account needs access to **PositionV2024/studio**. For a fine-grained token,
select this repository and grant **Contents: Read-only** for updating. A
publishing token that already grants this repository read and write access
can be reused. Connection and downloading use Studio's built-in support;
Git or GitHub CLI installation is not required. Update requests only read
from the application repository.

## What installation keeps

Updates preserve projects, the project library, workspace settings, remembered
GitHub credentials and the existing Python environment in `app/.venv`.
Startup rechecks the updated requirements and prepares any newly required
packages using the installed Python interpreter.

Save/Discard/Cancel prompts still apply. Cancel keeps Studio open. Approving
the restart closes Preview, script tests and the console, and lets active
background workers finish cancellation before installation starts. Close
other Studio windows before restarting to update.

Only files recorded as part of the application are replaced. A locally edited
application file or a collision with an untracked file stops automatic
replacement and explains the conflict. Backups and an installation journal
support recovery if replacement is interrupted.

Automatic installation requires the compact Windows x64 distribution with
**HoYo Project Studio.exe** beside **app**. Development checkouts, other
operating systems and the optional fully bundled PyInstaller distribution can
download a verified ZIP and receive instructions for opening a separate copy.

## Publish a release from your repository

The complete application ZIP supplied with each Studio update is ready to
attach to a GitHub release. For this version:

1. Open [your repository's Releases page](https://github.com/PositionV2024/studio/releases)
   and create a release tagged **v0.4.18**.
2. Attach the **complete application ZIP**, add your release notes, and publish
   it as the latest stable release.
3. Users with an older version of the simplified updater can select
   connect GitHub, select **Check for updates**, review the file changes, then
   choose **Download update**.

Use the actual application version for each release tag. Pushing source
changes to a branch alone does not publish a desktop update. GitHub's
automatically generated Source code downloads do not contain the prepared
Windows application package.

If you change the application's source yourself, update `studio/__init__.py`
and `pyproject.toml` to the same higher three-part version. Run
**app/Build Update Package.cmd** after checking your changes. It prepares one
complete ZIP in `app/release/vVERSION/` and selects that ZIP in File Explorer.
Attach it to the matching GitHub release. Existing outputs are kept in their
own version folders. The command reuses Studio's prepared Python environment.

The terminal equivalent, run inside the application content folder, is:

```text
python launcher/build_update_package.py --project-root .
```

The builder excludes user projects, environments, logs and build output. It
creates the package inventory inside the ZIP automatically. Keep each
published version's assets unchanged; publish a new version for later fixes.

### Compatibility with the older updater

The optional `--legacy-metadata` builder flag also generates
`studio-update.json` for clients still running 0.4.13 or 0.4.14. Upload both
outputs from that build if you want those older clients to receive the
migration automatically. Installing 0.4.18 from its complete ZIP avoids this
one-time compatibility step. New clients also accept correctly formed older
release packages.

GitHub's documentation covers [release asset checksums and downloads](https://docs.github.com/en/rest/releases/assets)
and [publishing a release in the browser](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).
