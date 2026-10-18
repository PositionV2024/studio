# Publishing projects — HoYo Project Studio 0.4.18

Open the dropdown beside **Publish** and choose **GitHub**, **Vercel** or **Streamlit Community Cloud**. All three destinations share one publishing window, so you can prepare a project once and continue to hosting.

Opening the window saves modified code tabs belonging to the selected project and writes its current visual settings to `.studio/published.json`. A failed save leaves the changes available and prevents publication preparation. The initial file scan is local. When **Update an existing repository** is active with a connected account, Studio also loads that account's owned repositories. Reviewing and publishing remain separate actions.

## Publish to GitHub

GitHub connection, file review and publishing are built into Studio. **You do not need Git, GitHub CLI, GitHub Desktop or an extra Python package.** Studio communicates directly with GitHub over HTTPS. You authenticate using a personal access token generated in your normal browser; your GitHub password is entered only on GitHub's website.

### Connect your account

1. Select **Publish → Publish to GitHub…**.
2. Choose **Update an existing repository** or **Create a new repository**. Existing mode shows a repository picker that fills after connecting. New mode lets you enter a repository name or `owner/repository`. Select the mode before connecting so the connection form shows the appropriate permission instructions.
3. Click **Sign in with GitHub…**. In the connection form, click **1. Create token on GitHub…**. Sign in on GitHub if asked.
4. For an existing repository, choose its owner under **Resource owner**, choose **Only select repositories**, and select that repository. Keep **Contents: Read and write** enabled. Metadata read access is included by GitHub. Review the expiry date and choose **Generate token**.
5. Copy the generated token, return to Studio, and paste it into **2. Access token**. Click **Connect GitHub**. The field is masked and cleared when submitted. Studio validates the token and displays the connected GitHub username.
6. On Windows, leave **Remember on this computer** selected to store the connection in Windows Credential Manager. Clear it to connect for the current Studio session only. When secure storage is unavailable or saving fails, Studio explains that you need to connect again after closing the application.

The browser link fills in the token name, permissions and a 30-day expiry. GitHub still requires you to choose the repository. The token can grant access only within your own GitHub account's permissions. Organization policies may require owner approval or SSO authorization. See GitHub's [personal access token guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) for token creation, expiry, revocation and fine-grained token limitations.

**Creating a new repository in Studio:** Choose the intended resource owner and **All repositories**, so the token can access the repository after it is created. The connection form requests **Administration: Read and write** in addition to Contents for repository creation. If you prefer to grant access to just one repository, create that repository on GitHub first, then connect with that repository selected and use **Update an existing repository** in Studio.

**GitHub Actions files:** If selected files include `.github/workflows/`, the connection form also requests **Workflows: Read and write** and explains why. A connection created before selecting workflow files may need a replacement token with that permission.

Use **Change connection…** to replace the token or use another account. **Disconnect** clears Studio's current connection and removes its saved credential on Windows. This does not revoke a token on GitHub; use GitHub's token settings to revoke it. Tokens are not written into projects, environment variables, command arguments, upload logs or exported application packages. The connection is shared across Studio's publishing and update dialogs. A saved connection is validated when loading repositories, reviewing or publishing.

### Choose an existing repository

1. Select **Update an existing repository**. Studio loads the repository list in the background using your connected account. Connect GitHub first if the picker asks you to sign in.
2. Open **Your repository** and choose an owned repository such as `PositionV2024/studio`. The dropdown shows the complete owner/name to make the destination clear. Accessible public and private repositories are included. Archived, disabled or known non-writable repositories are identified and cannot be selected for upload.
3. Use **Refresh list** after creating a repository or changing its access. The current choice is retained if it is still available. If it disappears, choose another destination explicitly. A matching project origin can be preselected when it belongs to your connected account and appears in the list.
4. Select your files and commit message, then choose **Review publish**.

**New repository visibility** is completely hidden in existing-repository mode. The review shows the repository's current visibility from GitHub. Switch to **Create a new repository** to enter a new name and choose Private or Public.

The picker lists repositories owned by the connected account that its token can access. An organization or another user's repository does not appear in this owned-repository list. If one of your repositories is missing, check its selection in your token's access settings, connect the updated token and refresh. GitHub documents the owner filter, visibility and pagination in [List repositories for the authenticated user](https://docs.github.com/en/rest/repos/repos?apiVersion=2026-03-10#list-repositories-for-the-authenticated-user).

Loading, refreshing or selecting a repository only reads information. It does not upload files. Changing accounts or replacing the connection clears the old repository list, selection and prepared publication so the next review uses the current account.

### Review and publish

1. For a new repository, enter its name and choose **Private** or **Public**. Private is the default. For an existing repository, choose its entry from the repository picker; its actual visibility is shown in the review.
2. Check the **Files** list, adjust the selected files and commit message, and select **Review publish**.
3. Inspect **Review changes**. It identifies the account, destination, visibility, branch and reviewed snapshot, followed by the actual file differences. Review is read-only; it does not upload files or create a remote commit.
4. Click **Publish to GitHub** to send that reviewed snapshot. Studio rechecks the account, destination and remote base before writing. If the default branch changed after review, refresh and review the new differences.
5. Use **Open repository** or **Review update branch** after success. The repository and branch also fill in the Vercel and Streamlit tabs.

A new repository starts on `main`. An existing repository receives a fresh `studio/update-*` branch based on its default branch. Open **Review update branch** to inspect/merge changes on GitHub, or select the update branch when creating a deployment. Studio preserves unselected remote files and does not automatically merge the branch or delete files missing from your project.

The reviewed files are copied into a private temporary snapshot. Your original project's Git index, staged files, remotes and local commits retain their state. The upload uses the exact reviewed bytes even if you subsequently edit the original source. A changed or damaged snapshot cannot be published. Script execution modes and binary changes are included in the review. Remote path conflicts with directories, symbolic links or submodules require a separate resolution before publication.

**Empty repositories:** GitHub's Git API requires an initial commit. The review explicitly identifies one small selected regular file used to initialize an empty repository; no generated placeholder file is added. A new repository then receives the complete reviewed snapshot on `main`. An existing empty repository retains that initial file on its default branch and receives the complete snapshot on the update branch. If setup is interrupted, this initial file or the new repository may already exist; Studio reports the destination so you can inspect it before reviewing again. See GitHub's [Git database guide](https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-your-git-database).

### File selection and failures

Studio reads project `.gitignore` rules itself, including nested rules, negation and directory patterns. It excludes local virtual environments, caches, common credentials, private keys and local Studio configuration. `.studio/published.json` remains an explicit deployment input in the review. Exclusion does not remove a file already present on GitHub. Check selected source and data before publishing; credentials embedded in ordinary code cannot all be identified automatically.

Files above 20 MB start unchecked. This publisher accepts at most 95 MB per file, 500 MB per selected snapshot and 10,000 candidate source files. It does not configure Git LFS. If a selected file changes after scanning, refresh the file list and review again. Very large remote trees or file previews can exceed GitHub's response limits; Studio reports an incomplete response instead of publishing from a partial review.

Progress and errors remain visible while the interface stays responsive. Expired or revoked tokens require a new connection. Access-denied errors explain the repository, Contents/Workflows permission, organization approval or SSO checks. Rate limits and connection failures are reported without automatically repeating a write request.

If repository creation or an upload is interrupted, inspect the reported GitHub destination before retrying. A completed remote action is not rolled back by cancelling. For a repository created before an upload failure, choose **Update an existing repository** and prepare a new review. For an update branch already created, inspect that branch first.

## Deploy a Streamlit project to Vercel

This workflow prepares files for **Vercel Container Runtime (Beta)**, then opens Vercel's Git import page. Vercel documents automatic detection of a project-root `Dockerfile.vercel` or `Containerfile.vercel` and an HTTP server listening on port 80 or the configured `PORT` environment variable. See [Vercel Container Runtime](https://vercel.com/docs/functions/runtimes/container).

1. Open **Publish → Publish to Vercel…** for your Streamlit project.
2. Confirm the app's Python entry point. For the usual project it is `app.py`.
3. Select **Prepare Streamlit deployment files…**. Review every proposed file and its notes, then click **Add deployment files**.
4. On the **GitHub** tab, refresh/review the file list and publish the project including its new deployment files.
5. Return to **Vercel**. The repository and branch from the GitHub result are filled in. Check the deployment root and use **Copy deployment details** if useful.
6. Select **Open Vercel import**. In your Vercel account, import that GitHub repository, select the intended root/branch and review framework/container detection, environment variables and build settings before choosing Deploy.
7. Check the deployment logs and the hosted app's interactions after the build. For an update branch, merge it into the production branch or deliberately configure the appropriate branch in Vercel.

The generated bundle contains:

| File | Purpose |
| --- | --- |
| `Dockerfile.vercel` | Uses Python 3.12, installs the reviewed dependencies and starts the Streamlit launcher. |
| `requirements-vercel.txt` | Includes `requirements.txt` when present, otherwise copies declared `project.dependencies` from `pyproject.toml`, and includes Streamlit. |
| `studio_vercel_start.py` | Starts the selected app on `0.0.0.0` and the assigned `PORT`, preserving normal Streamlit security settings and process shutdown signals. |
| `.dockerignore` | Keeps local secrets, virtual environments, caches and draft/machine settings out of the image. Published UI settings remain available. |

Preparation only displays a proposal. Files are added after **Add deployment files**. Existing deployment/configuration files are not overwritten; adapt those files if you already maintain a Docker or Vercel setup.

Review the generated Python version and add any Linux system packages or application dependencies your project needs. If no requirements manifest exists, the generator can only include Streamlit. Optional/dynamic/tool-specific dependency groups need explicit review. Private values belong in the hosting service's environment/secrets settings. Code using Streamlit's local `secrets.toml` may need an explicit deployment-secret integration; excluded local secrets are not silently uploaded.

**Hosting limits:** Vercel's container and WebSocket support are beta features. Connections are tied to a function instance while open; duration limits or instance restarts can cause reconnects. A Streamlit session held only in memory can reset when that happens. Durable application data needs persistent storage. See [Vercel WebSockets](https://vercel.com/docs/functions/websockets) and [Function limits](https://vercel.com/docs/functions/limitations). Studio's generated launcher is a deployment starting point; a successful local preview does not establish compatibility with every cloud setting or project dependency.

### Existing frontend or static website

For a project with a separate supported frontend, select its folder as the Vercel root. Studio detects common framework packages or a static `index.html` and opens the same import flow. Review Vercel's detected framework/build settings. Selecting a frontend folder deploys that folder; a separate Python backend still needs its own configured endpoint.

## Streamlit Community Cloud

Publish the project to GitHub first. Open the **Streamlit Cloud** tab, check the repository, branch and app file, and copy the generated GitHub app URL. Select **Open Streamlit Community Cloud**, choose **Create app**, and use that URL or its repository/branch/file fields. Configure project dependencies and secrets in the service before deployment. See the [official Streamlit deployment guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy).

## Published visual changes

**Save** keeps visual edits in the project's Studio draft. **Publish UI Settings**, and preparation for GitHub/hosting, also update `.studio/published.json`. The included demo template reads this file. An imported application's public runtime must also read the relevant settings or integrate `studio_runtime.py` for visual overrides to appear outside Studio.

Visual editing does not rewrite arbitrary Python or HTML source. Publishing the JSON alone cannot make an unrelated application's rendering code consume it. Use stable `data-editor-id` values for elements that must retain their settings through structural changes. Check the hosted page after publishing.

## Validation scope

The app's connection form, secure-storage calls, local file review, direct GitHub API commits, branch behavior, generated files and publishing dialogs are tested with disposable projects and simulated GitHub responses. No GitHub repository was created and no Vercel or Streamlit deployment was performed while building this release. A real account and the project's production dependencies are needed to complete the hosting step. See **VERIFICATION.md** for the release's exact checks.
