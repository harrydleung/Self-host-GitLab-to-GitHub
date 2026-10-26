# GitLab to a personal GitHub account

This package automates option B: every GitHub repository is owned by your personal account.
It reads GitLab through your LAN and uploads to GitHub using outbound connections.
No public exposure of your GitLab, inbound firewall rule or port forwarding is needed.

Examples:

| GitLab project | GitHub destination |
| --- | --- |
| root/tools | YOUR_USERNAME/tools |
| root/firmware | YOUR_USERNAME/firmware |
| company-a/firmware | YOUR_USERNAME/company-a-firmware |
| company-b/firmware | YOUR_USERNAME/company-b-firmware |
| company-a/product-x/hardware | YOUR_USERNAME/company-a-product-x-hardware |

The plan command reads the authenticated GitLab account's username from the /user API.
For your account, the username is root and the display name is Halry. These identify the
same account. The display name is never used as a repository prefix or suffix.

Projects in your actual personal namespace keep only their project name:
root/firmware becomes firmware, with no root- prefix, -root suffix or Halry suffix.
This works with other account usernames too; it does not hardcode root or Halry.
Company/group and nested subgroup names remain prefixes, such as company-a-firmware.
Other users' namespaces are retained, because they are separate from your own account.

Names use GitHub's allowed characters and 100-character maximum. In rare collisions caused
by flattening, sanitising or truncating names, a numeric project ID is the final fallback.
A username or display name is never appended to resolve a collision. Personal names are
reserved ahead of colliding flattened group names. Review the preview JSON before uploading.

## What it transfers

- All branches, tags, Git notes and their reachable commit history, without rewriting commits.
- Author and committer details, original timestamps, merge history and original Git object hashes.
- All source Git LFS objects, unless you explicitly choose --skip-lfs.
- The original default branch, when it exists.
- A local mirror of all Git references advertised by GitLab. Internal GitLab merge-request
  references stay in this local mirror and are not published as GitHub branches.

Every destination is created private and empty. The script verifies the destination branch,
tag and note hashes after pushing. It uses an atomic push without force or deletion.
It refuses an existing destination unless its GitHub repository ID was recorded by this
migration. It will not reuse a same-name repository based on its name alone.

## What it does not convert

This is not a GitLab issue/MR-to-GitHub issue/PR importer. Git push cannot transfer issues,
comments, merge requests, releases, wiki pages, access controls, CI configuration or CI logs.
The archive command downloads GitLab project exports to preserve the project records
supported by your installed GitLab version. These exports are local archives, not native
GitHub issues/PRs and not a complete GitLab instance backup.

GitLab project exports exclude some data, including CI job traces and artifacts, registry
contents, variables and certain activity records. For your requirement to preserve all
records, retain a restorable full GitLab backup as well as configuration, secrets and any
external object storage. Back up any separate registry metadata database where applicable.
Keep the exact GitLab version/edition or installation image needed to restore it. Keep the
original instance until migration and restore verification are complete.

For a Linux package installation, the standard application backup command is:

```bash
sudo gitlab-backup create
```

Also save /etc/gitlab/gitlab.rb and /etc/gitlab/gitlab-secrets.json separately.
For Docker or Kubernetes, follow the installation-specific backup instructions linked below.
The application backup alone is not a complete machine snapshot.

## Requirements

- Python 3.10 or newer. No pip packages are needed.
- Your existing Git installation.
- Git LFS for repositories that use LFS.

GitHub Desktop and GitHub CLI (gh) are not required. This version uses Python's
built-in HTTPS client to create private repositories through GitHub's REST API,
then uses your installed Git to upload history.

Check:

```powershell
python --version
git --version
git lfs version
```

If none of your selected repositories uses LFS, you can pass --skip-lfs to migrate.

## 1. Create a GitHub personal access token

Open https://github.com/settings/tokens in your browser.

For a personal access token (classic), choose Generate new token (classic), set an
expiration and select repo. Also select workflow if any source repository already
contains GitHub Actions workflow files.

A fine-grained token can also be used. Select your personal account as Resource owner
and All repositories so the token covers newly created repositories. Give repository
permissions Administration: read and write and Contents: read and write. Add
Workflows: read and write if GitHub Actions workflow files are present.

The script prompts for this token only when running migrate. It checks that the
token belongs to the personal owner in your plan. It does not save the token or
embed it in repository URLs. For unattended use, GITHUB_TOKEN can be provided in
the local process environment.

No gh login, GitHub Desktop sign-in, SSH-key setup or global Git authentication
configuration is required for GitHub uploads.

## 2. Create a GitLab personal access token

For plan/migrate, use read_api and read_repository scopes.
For archive, exporting also requires the api scope and the project Maintainer/Owner role.
An api-scoped token can be used for the whole workflow. Project export must be enabled
on your GitLab instance.

The script prompts for the GitLab token without displaying it. It does not save it to your plan,
logs, repository URLs or Git configuration. Each invocation prompts again unless you already
provide a GITLAB_TOKEN environment variable. Do not put a token in a URL or in the plan.

## 3. Discover projects and preview the mapping

Extract this ZIP into a writable folder and open PowerShell there.
Replace the example LAN URL and YOUR_GITHUB_USERNAME:

```powershell
python .\gitlab_to_github.py plan --gitlab http://192.168.1.10 --owner YOUR_GITHUB_USERNAME
```

This lists every project accessible to the GitLab token, using paginated API requests.
That includes projects in company groups and nested subgroups, not only personal projects.
It saves:

- migration-plan.json: the authoritative editable plan
- migration-plan.csv: a convenient preview of source and destination names

Nothing is created or uploaded on GitHub in this step. To restrict discovery to particular
GitLab group paths, repeat --group:

```powershell
python .\gitlab_to_github.py plan --gitlab http://192.168.1.10 --owner YOUR_GITHUB_USERNAME --group company-a --group company-b
```

A group match includes its nested subgroups. By default, accessible public projects on your
instance are also included. Review the plan, use --group or choose --membership-only if needed.
The script will not overwrite an existing plan; use --plan another-plan.json for a fresh one.

In migration-plan.json, set include to false to exclude a project, or edit github_name.
Keep the source IDs and source URLs intact. Changing the CSV has no effect.

Existing plans keep their saved destination names. To apply the current authenticated-user namespace rule,
generate a fresh plan using --plan migration-plan-updated.json. Pass that same --plan argument
to archive and migrate. This script update does not rename repositories already on GitHub.
A work directory that records a different destination for a project will reject the changed
mapping; review any existing migration before starting a new destination.

## 4. Preserve GitLab project archives

Pause changes to GitLab for the final export and migration so the snapshots remain consistent.
Keep your full server backup as described above. Then, to download project exports in bulk:

```powershell
python .\gitlab_to_github.py archive
```

Archives are saved under migration-data\exports. This command needs an api-scoped token
with export permissions. It reports per-project failures and continues to the next project.
Repeat it to retry failures. Previously downloaded exports are skipped; add --refresh for
a fresh snapshot while retaining earlier archive files.

Exports remain in GitLab's own export format. You can restore supported project records into
a compatible GitLab installation later. This command does not back up group/instance settings
or data excluded by GitLab exports.

## 5. Migrate all selected repositories

```powershell
python .\gitlab_to_github.py migrate
```

The default clone protocol is HTTPS (or HTTP if your LAN GitLab URL uses HTTP). The GitLab
token is passed only to child Git processes and scoped to your GitLab URL.
If your GitLab SSH key already works and you prefer SSH:

```powershell
python .\gitlab_to_github.py migrate --clone-protocol ssh
```

Choose one clone protocol and keep it on retries. If your GitLab uses a private certificate
authority, pass --ca-file C:\path\internal-ca.pem to each command that connects to GitLab.
The CA must be trusted for the GitLab hostname. TLS verification is not disabled.

If you are certain none of the selected repositories uses LFS, --skip-lfs avoids requiring
Git LFS. Do not use this for repositories containing LFS pointer files.

## 6. Review and retry

The work directory contains:

- report.csv: pending/completed/failed status and errors for each project
- state.json: records which GitHub repository IDs were created
- mirrors\project-ID.git: local Git mirrors, with GitLab internal references retained
- exports\: downloaded GitLab project archives

Keep this directory. Rerun the same migrate command after fixing a network or permissions
problem. Failed entries resume using the recorded destination. Completed entries are skipped.
Replaced or public destinations are rejected. Existing unrelated GitHub repositories are
never adopted automatically. If an interruption happened precisely after remote creation
but before state was saved, it will refuse that unrecorded destination; inspect it manually.

This is a one-time migration, not ongoing mirroring. Completed projects are skipped even
if GitLab changes later. Freeze source changes for cutover. Do not start working on a
partially migrated GitHub repository; divergent destination history is rejected.

An interrupted clone can leave an incomplete local mirror. If its retry fails, move that
one project-ID.git directory aside and retry. The script never deletes source repositories,
existing destinations or local mirrors automatically.

Run only one archive/migrate command against a work directory at a time.

## After migration

- Inspect the report and compare representative branches, annotated tags and old commits.
- Check LFS files with a fresh clone before retiring GitLab.
- Recreate collaborators, branch protection, webhooks and CI automation as required.
- GitLab CI YAML remains a versioned file; it is not converted into GitHub Actions.
- A wiki is a separate repository. It is preserved by supported project exports; the
  code migration does not publish it. Private wiki availability depends on the GitHub plan.
- Source submodule repositories must be migrated separately. Update .gitmodules, package
  references and old GitLab URLs in a new commit afterwards; the script does not rewrite
  historical commits or submodule pointers.
- Add your historical commit email addresses to your GitHub account for attribution.
- Change the origin remote in your normal working copies to the destination URLs.
- The source archived flag is included in the plan but is not applied to GitHub automatically.

GitHub file, repository, LFS and account limits still apply. A historical ordinary Git blob
larger than GitHub's push limit causes that repository to fail rather than silently rewriting
its history. The report records the error. Keep your original GitLab backup.

## Validation of this package

The included behavior is checked with offline tests covering name collisions, pagination,
credential scoping, retry state, commit/reference preservation and refusal to force-update
destination work. The GitLab/GitHub cloud APIs have not been exercised with your credentials.
Read VALIDATION.txt for the exact checks completed in this environment.

## Official references

- GitLab project discovery: https://docs.gitlab.com/api/projects/
- GitLab token scopes: https://docs.gitlab.com/user/profile/personal_access_tokens/
- GitLab project exports: https://docs.gitlab.com/api/project_import_export/
- Exported/excluded records: https://docs.gitlab.com/user/project/settings/import_export/
- GitLab full backups: https://docs.gitlab.com/administration/backup_restore/backup_gitlab/
- GitHub repository duplication and LFS: https://docs.github.com/en/repositories/creating-and-managing-repositories/duplicating-a-repository
- GitHub personal-repository creation API: https://docs.github.com/en/rest/repos/repos#create-a-repository-for-the-authenticated-user
- GitHub token setup: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- GitHub repository limits: https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits
