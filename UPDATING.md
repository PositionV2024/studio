# Updating HoYo Project Studio

## Check, download, restart

1. Open **Update** at the far right of Studio's top toolbar, then select
   **Check for updates**.
2. If a newer version is ready, **Download update** becomes available. Select
   it and continue working while the download finishes.
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
| Before checking or while a check is running | Greyed out and disabled |
| The installed version is current or newer | Greyed out and disabled |
| No stable release or ready application package is available | Greyed out and disabled |
| A check fails | Greyed out and disabled; Check for updates lets you retry |
| A newer compatible package is ready | Enabled |
| A download is in progress | Disabled until the operation finishes |
| The download has been verified and prepared | Restart to update becomes available |

The dialog shows the installed version, the latest release when available,
release notes, progress and the last-check time. Version numbers are compared
numerically; equal and older versions cannot be installed as updates.

## The earlier missing-file message

Versions 0.4.13 and 0.4.14 required a separate `studio-update.json` attachment
in every GitHub release. This caused the missing-file message even when the
complete application ZIP was already attached.

Version **0.4.15** supports releases containing **just the complete application
ZIP**. GitHub's release information supplies the download size and SHA-256
checksum. The updater verifies these and then checks the package's internal
files, inventory and version before preparing installation. A newer release
without a ready application ZIP leaves Download update disabled.

Install this complete 0.4.15 ZIP once to get the simplified checker: save and
close Studio, extract the ZIP into a **new empty folder**, and open
**HoYo Project Studio.exe**. Keep the previous folder while checking the new
copy, especially if projects were created inside it. Subsequent compatible
releases can use the Update button.

## GitHub access

Public releases can be checked without signing in. If the repository is
private, enable the GitHub-account option and connect an account with access.
The existing Studio connection can be reused. Update access needs
**Contents: Read-only** for this repository. Connection and downloading use
Studio's built-in support; Git or GitHub CLI installation is not required.

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
   and create a release tagged **v0.4.15**.
2. Attach the **complete application ZIP**, add your release notes, and publish
   it as the latest stable release.
3. Users with an older version of the simplified updater can select
   **Check for updates**, then **Download update**.

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
migration automatically. Installing 0.4.15 from its complete ZIP avoids this
one-time compatibility step. New clients also accept correctly formed older
release packages.

GitHub's documentation covers [release asset checksums and downloads](https://docs.github.com/en/rest/releases/assets)
and [publishing a release in the browser](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).
