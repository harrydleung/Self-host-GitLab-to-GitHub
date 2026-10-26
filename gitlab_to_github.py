#!/usr/bin/env python3
"""Batch GitLab -> personal GitHub migration. Python 3.10+, Git and optional Git LFS. No GitHub CLI."""
from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import json
import math
from email.utils import parsedate_to_datetime
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

SCOPES = ("refs/heads/", "refs/tags/", "refs/notes/")
SECRETS: list[str] = []


class MigrationError(Exception):
    pass


def clean(message):
    text = str(message)
    for value in SECRETS:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def log(message):
    print(f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}] {clean(message)}",
          flush=True)


def run(argv, *, env=None, input_text=None):
    # Binary stdin preserves LF for Git's update-ref protocol on Windows.
    result = subprocess.run([str(x) for x in argv],
                            input=None if input_text is None else input_text.encode("utf-8"),
                            capture_output=True, env=env)
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode:
        detail = (stderr or stdout).strip()
        raise MigrationError(clean(f"{argv[0]} failed ({result.returncode}): {detail[-6000:]}"))
    return stdout.strip()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def origin(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.hostname, parts.port


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected and origin(request.full_url) != origin(newurl):
            for header in list(redirected.headers):
                if header.lower() in ("private-token", "authorization", "cookie"):
                    redirected.remove_header(header)
        return redirected


def wait_seconds(seconds):
    # Short chunks keep long server-requested waits interruptible.
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        step = min(remaining, 30.0)
        time.sleep(step)
        remaining -= step


def retry_delay(headers, fallback, now=None):
    """Honour Retry-After (seconds or HTTP date) and rate-limit reset headers."""
    headers = headers or {}
    now = time.time() if now is None else now
    value = str(headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    if value:
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return max(1.0, seconds)
        except ValueError:
            pass
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(1.0, stamp.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            pass
    for name in ("RateLimit-Reset", "X-RateLimit-Reset",
                 "ratelimit-reset", "x-ratelimit-reset"):
        try:
            reset = float(headers.get(name, ""))
            if math.isfinite(reset) and reset > now:
                return reset - now + 1.0
        except (ValueError, TypeError):
            pass
    return float(fallback)


class GitLab:
    def __init__(self, base, token, ca_file=None, *, export_download_interval=0):
        self.base = base.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise MigrationError("--gitlab must be an http:// or https:// URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise MigrationError("Use a GitLab URL without credentials, query or fragment.")
        self.token = token
        self.export_download_interval = float(export_download_interval)
        if (not math.isfinite(self.export_download_interval) or
                self.export_download_interval < 0):
            raise MigrationError("Export download interval must be finite and non-negative.")
        self._next_export_download = 0.0
        self.ca_file = str(Path(ca_file).resolve()) if ca_file else None
        self.opener = urllib.request.build_opener(
            SafeRedirect(), urllib.request.HTTPSHandler(
                context=ssl.create_default_context(cafile=self.ca_file)))
        SECRETS.append(token)

    def open(self, endpoint, *, method="GET", params=None, timeout=120):
        url = self.base + "/api/v4/" + endpoint.lstrip("/")
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, method=method,
            headers={"PRIVATE-TOKEN": self.token, "Accept": "application/json",
                     "User-Agent": "local-gitlab-to-github-migrator"})
        rate_retries = 0
        read_retries = 0
        while True:
            if method == "GET" and re.fullmatch(r"projects/[^/]+/export/download", endpoint):
                delay = max(0.0, self._next_export_download - time.monotonic())
                if delay:
                    log(f"  Pacing GitLab export downloads: waiting {delay:.0f}s")
                    wait_seconds(delay)
                self._next_export_download = time.monotonic() + self.export_download_interval
            try:
                return self.opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                # A rejected 429 request is retried indefinitely, including export POSTs.
                if exc.code == 429:
                    rate_retries += 1
                    log(f"  GitLab HTTP 429 on {method} {endpoint}; "
                        f"waiting 30s before retry {rate_retries} (no retry limit)")
                    exc.close()
                    wait_seconds(30)
                    continue
                # Other POST failures may have been processed, so do not replay them.
                if method == "GET" and exc.code >= 500 and read_retries < 6:
                    delay = retry_delay(exc.headers, min(2 ** read_retries, 30))
                    read_retries += 1
                    log(f"  GitLab HTTP {exc.code} (temporary server error) on "
                        f"{method} {endpoint}; waiting {delay:.0f}s "
                        f"before retry {read_retries}/6")
                    exc.close()
                    wait_seconds(delay)
                    continue
                if exc.code in (401, 403):
                    detail = "Check token scopes and project access permissions."
                else:
                    detail = "Check GitLab project/export settings and server availability."
                exc.close()
                raise MigrationError(
                    f"GitLab {method} {endpoint}: HTTP {exc.code}. {detail}") from exc
            except urllib.error.URLError as exc:
                raise MigrationError(f"Cannot reach GitLab: {clean(exc.reason)}") from exc

    def request(self, endpoint, *, method="GET", params=None):
        with self.open(endpoint, method=method, params=params) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}

    def projects(self, membership_only=False):
        # Keyset pagination supports large instances and avoids the offset ceiling.
        params = {"per_page": 100, "pagination": "keyset",
                  "order_by": "id", "sort": "asc", "simple": "true"}
        if membership_only:
            params["membership"] = "true"
        last_id = 0
        while True:
            if last_id:
                params["id_after"] = last_id
            batch = self.request("projects", params=params)
            if not isinstance(batch, list):
                raise MigrationError("Unexpected GitLab projects API response.")
            if not batch:
                return
            new_last_id = max(int(p["id"]) for p in batch)
            if new_last_id <= last_id:
                raise MigrationError("GitLab pagination did not advance; check version/API support.")
            yield from batch
            last_id = new_last_id
            if len(batch) < 100:
                return

    def download_export(self, project_id, destination):
        destination = Path(destination)
        partial = destination.with_suffix(destination.suffix + ".part")
        with self.open(f"projects/{project_id}/export/download", timeout=600) as response:
            with partial.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        if partial.stat().st_size == 0:
            raise MigrationError("GitLab returned an empty project export.")
        os.replace(partial, destination)


def token_for_gitlab():
    token = os.environ.get("GITLAB_TOKEN") or getpass.getpass("GitLab access token: ")
    if not token:
        raise MigrationError("A GitLab access token is required.")
    return token




def personal_username(project, current_username=None):
    """Match the authenticated account's username, never its display name."""
    first, separator, _rest = project["path_with_namespace"].partition("/")
    if not separator or not current_username:
        return None
    namespace = project.get("namespace") or {}
    if namespace.get("kind") not in (None, "user"):
        return None
    namespace_path = namespace.get("path") or first
    if (first.casefold() == current_username.casefold() and
            namespace_path.casefold() == current_username.casefold()):
        return first
    return None


def github_name(path, project_id, personal_namespace=None):
    segments = path.split("/")
    if (len(segments) > 1 and personal_namespace and
            segments[0].casefold() == personal_namespace.casefold()):
        path = "/".join(segments[1:])
    ascii_path = unicodedata.normalize("NFKD", path.replace("/", "-"))
    ascii_path = ascii_path.encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._-]", "-", ascii_path).strip(".-")
    name = name[:100].rstrip(".-")
    if name.lower().endswith(".git"):
        name = name[:-4].rstrip(".-")
    return name if name else f"gitlab-{project_id}"


def name_with_suffix(base, suffix):
    tail = "-" + suffix[:98]
    return (base[:100 - len(tail)].rstrip(".-") or "p") + tail


def allocate_names(projects, current_username=None):
    rows = []
    for project in projects:
        project_id = int(project["id"])
        personal = personal_username(project, current_username)
        base = github_name(project["path_with_namespace"], project_id,
                           personal_namespace=personal)
        rows.append((project_id, base, bool(personal)))
    # Reserve personal project names before any colliding flattened group name.
    rows.sort(key=lambda row: (not row[2], row[0]))
    names, used = {}, set()
    for project_id, base, _personal in rows:
        candidate, attempt = base, 0
        while candidate.casefold() in used:
            suffix = f"gl{project_id}" + (f"-{attempt}" if attempt else "")
            candidate = name_with_suffix(base, suffix)
            attempt += 1
        used.add(candidate.casefold())
        names[project_id] = candidate
    return names


def make_plan(args):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", args.owner):
        raise MigrationError("Enter your personal GitHub username, without a URL.")
    path = Path(args.plan)
    if path.exists():
        raise MigrationError("The plan already exists. Choose a new --plan filename.")
    api = GitLab(args.gitlab, token_for_gitlab(), args.ca_file)
    source_account = api.request("user")
    current_username = source_account.get("username")
    if not isinstance(current_username, str) or not current_username:
        raise MigrationError("GitLab did not return the authenticated account's username.")
    projects = list(api.projects(args.membership_only))
    if args.group:
        groups = [g.strip("/") for g in args.group]
        projects = [p for p in projects if any(
            p["path_with_namespace"].startswith(g + "/") for g in groups)]
    if not projects:
        raise MigrationError("No matching projects. Check token access and --group paths.")
    names = allocate_names(projects, current_username)
    entries = []
    for p in sorted(projects, key=lambda p: int(p["id"])):
        entry = {k: p.get(k) for k in (
            "id", "path_with_namespace", "http_url_to_repo", "ssh_url_to_repo",
            "default_branch", "description", "archived", "namespace", "empty_repo")}
        entry.update({"include": True, "github_name": names[int(p["id"])],
                      "source_username": personal_username(p, current_username)})
        entries.append(entry)
    plan = {"format": 1, "gitlab": api.base, "github_owner": args.owner,
            "gitlab_username": current_username,
            "gitlab_display_name": source_account.get("name"),
            "created_at": datetime.now(timezone.utc).isoformat(), "projects": entries}
    save_json(path, plan)
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(["gitlab_project_id", "gitlab_path", "github_target"])
        for p in entries:
            writer.writerow([p["id"], p["path_with_namespace"],
                             f"{args.owner}/{p['github_name']}"])
            log(f"{p['path_with_namespace']} -> {args.owner}/{p['github_name']}")
    log(f"Plan saved to {path.resolve()}. Nothing has been uploaded to GitHub.")
    log("Review the JSON. Change github_name or set include to false as needed.")
    return 0


def validate_plan(plan):
    if plan.get("format") != 1:
        raise MigrationError("Unsupported plan format.")
    owner = plan.get("github_owner", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
        raise MigrationError("Invalid personal GitHub owner in plan.")
    used, ids = set(), set()
    selected = []
    for p in plan["projects"]:
        if not isinstance(p.get("include"), bool):
            raise MigrationError("Each include value must be JSON true or false.")
        if not p["include"]:
            continue
        name, project_id = p["github_name"], int(p["id"])
        if project_id <= 0 or project_id in ids:
            raise MigrationError("Duplicate or invalid project ID in plan.")
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}", name):
            raise MigrationError(f"Invalid GitHub repository name: {name}")
        if name.lower().endswith(".git") or name.casefold() in used:
            raise MigrationError(f"Duplicate or unsupported GitHub repository name: {name}")
        ids.add(project_id)
        used.add(name.casefold())
        selected.append(p)
    if not selected:
        raise MigrationError("The plan has no included projects.")
    return selected


def load_state(path, plan):
    state = load_json(path) if path.exists() else {
        "gitlab": plan["gitlab"], "github_owner": plan["github_owner"],
        "repositories": {}, "exports": {}}
    if state["gitlab"] != plan["gitlab"]:
        raise MigrationError("This work directory belongs to a different GitLab instance.")
    if state["github_owner"].casefold() != plan["github_owner"].casefold():
        if state["repositories"]:
            raise MigrationError(
                "The plan's GitHub owner differs from this work directory, which already "
                "records repository migration attempts. Restore the original owner or use "
                "a separate --work-dir for a different destination. Keep the existing "
                "state.json and archives.")
        previous_owner = state["github_owner"]
        state["github_owner"] = plan["github_owner"]
        save_json(path, state)
        log(f"Updated checkpoint GitHub owner from {previous_owner!r} to "
            f"{plan['github_owner']!r}; no repository migrations were recorded. "
            "Existing project exports are retained.")
    return state



def token_for_github():
    token = os.environ.get("GITHUB_TOKEN") or getpass.getpass("GitHub access token: ")
    if not token:
        raise MigrationError("A GitHub personal access token is required.")
    return token


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(
            SafeRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        SECRETS.append(token)

    def request(self, endpoint, *, fields=None, method=None):
        method = method or ("POST" if fields is not None else "GET")
        if endpoint.startswith("/") or "://" in endpoint or ".." in endpoint.split("/"):
            raise MigrationError("Invalid GitHub API endpoint.")
        data = json.dumps(fields).encode("utf-8") if fields is not None else None
        request = urllib.request.Request(
            "https://api.github.com/" + endpoint, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "local-gitlab-to-github-migrator"})
        for attempt in range(4):
            try:
                with self.opener.open(request, timeout=120) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if method == "GET" and (exc.code == 429 or exc.code >= 500) and attempt < 3:
                    retry = exc.headers.get("Retry-After", "")
                    time.sleep(min(int(retry) if retry.isdigit() else 2 ** attempt, 30))
                    continue
                try:
                    detail = json.loads(exc.read()).get("message", "")
                except (ValueError, AttributeError):
                    detail = ""
                raise MigrationError(
                    f"GitHub {method} {endpoint}: HTTP {exc.code}. {clean(detail)} "
                    "Check token permissions, repository-name conflicts and API limits.") from exc
            except urllib.error.URLError as exc:
                raise MigrationError(f"Cannot reach GitHub: {clean(exc.reason)}") from exc

    def git_environment(self, owner):
        return credential_environment("https://github.com", owner, self.token)


def credential_environment(base, username, token, ca_file=None):
    env = os.environ.copy()
    # Credentials exist only in these child processes and match this service URL.
    encoded = base64.b64encode(f"{username}:{token}".encode()).decode()
    SECRETS.extend([token, encoded])
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env[f"GIT_CONFIG_KEY_{count}"] = f"http.{base}/.extraHeader"
    env[f"GIT_CONFIG_VALUE_{count}"] = "Authorization: Basic " + encoded
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    if ca_file:
        env["GIT_SSL_CAINFO"] = ca_file
    return env


def source_environment(api):
    return credential_environment(api.base, "oauth2", api.token, api.ca_file)


def clone_source(project, protocol, api):
    source = project["ssh_url_to_repo" if protocol == "ssh" else "http_url_to_repo"]
    if not isinstance(source, str) or not source or source.startswith("-"):
        raise MigrationError("Missing or invalid source clone URL in plan.")
    if protocol == "https":
        parts = urllib.parse.urlsplit(source)
        if origin(source) != origin(api.base) or parts.username or parts.password:
            raise MigrationError(
                "The HTTP(S) clone URL differs from the configured GitLab origin "
                "(scheme, host or port), or contains credentials. Use --offline-source "
                "to migrate saved exports/mirrors, or correct http_url_to_repo in the "
                "plan to your reachable GitLab clone URL without credentials.")
    return source


def ensure_mirror(project, mirror, protocol, api):
    source = clone_source(project, protocol, api)
    env = source_environment(api)
    if mirror.exists():
        existing = run(["git", "-C", mirror, "remote", "get-url", "origin"])
        if existing != source:
            raise MigrationError("Local mirror has a different source. Use a new --work-dir "
                                 "when changing clone protocol or source URL.")
        log("  Updating local mirror from GitLab")
        run(["git", "-C", mirror, "fetch", "--prune", "origin"], env=env)
    else:
        log("  Cloning all advertised Git references locally")
        run(["git", "clone", "--mirror", source, mirror], env=env)
    run(["git", "-C", mirror, "fsck", "--full"], env=env)
    return env


def local_source_environment():
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    return env


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_local_mirror(mirror, env):
    if run(["git", "-C", mirror, "rev-parse", "--is-bare-repository"], env=env) != "true":
        raise MigrationError("Offline source must be a bare Git mirror.")
    if list((mirror / "objects" / "pack").glob("*.promisor")):
        raise MigrationError("Offline source is a partial clone; obtain all Git objects first.")
    run(["git", "-C", mirror, "fsck", "--full"], env=env)


def mirror_from_export(project, mirror, archive_path, plan, env):
    # Copy only the bundle and hash-named LFS objects. Never extract archive paths.
    staging_root = mirror.parent / "import-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"project-{project['id']}-",
                                     dir=staging_root) as temporary:
        staging = Path(temporary)
        imported = staging / "mirror.git"
        run(["git", "init", "--bare", imported], env=env)
        bundle = staging / "project.bundle"
        found_bundle = False
        try:
            with tarfile.open(archive_path, "r:*") as archive:
                for member in archive:
                    path = PurePosixPath(member.name)
                    is_bundle = path.name == "project.bundle"
                    is_lfs = ("lfs-objects" in path.parts and
                              re.fullmatch(r"[0-9a-f]{64}", path.name) is not None)
                    if not (is_bundle or is_lfs):
                        continue
                    if (not member.isfile() or path.is_absolute() or ".." in path.parts or
                            "\\" in member.name or ":" in member.name):
                        raise MigrationError("Unsafe repository/LFS member in the project export.")
                    if is_bundle:
                        if found_bundle:
                            raise MigrationError("The export contains multiple project.bundle files.")
                        found_bundle = True
                        target = bundle
                    else:
                        oid = path.name
                        target = imported / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        raise MigrationError("Duplicate repository/LFS member in the project export.")
                    with archive.extractfile(member) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    if is_lfs and sha256_file(target) != path.name:
                        raise MigrationError("An exported LFS object's content does not match its hash.")
        except (tarfile.TarError, EOFError) as exc:
            raise MigrationError(f"Cannot read project export {archive_path.name}: {exc}") from exc
        if not found_bundle:
            if project.get("empty_repo") is not True:
                raise MigrationError(
                    "The saved export has no project.bundle. Cannot assume it is an empty "
                    "repository. Use a complete export or an existing full Git mirror.")
        else:
            run(["git", "-C", imported, "bundle", "verify", bundle], env=env)
            text = run(["git", "-C", imported, "bundle", "unbundle", bundle], env=env)
            refs = {}
            for line in text.splitlines():
                match = re.fullmatch(r"([0-9a-f]{40}|[0-9a-f]{64}) (refs/\S+|HEAD)", line)
                if not match:
                    raise MigrationError("Unexpected reference in the saved Git bundle.")
                oid, ref = match.groups()
                if ref != "HEAD":
                    refs[ref] = oid
            commands = "start\n" + "".join(f"update {ref} {oid}\n" for ref, oid in refs.items())
            commands += "prepare\ncommit\n"
            run(["git", "-C", imported, "update-ref", "--stdin"],
                env=env, input_text=commands)
            branch = project.get("default_branch")
            if branch and f"refs/heads/{branch}" in refs:
                run(["git", "-C", imported, "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
                    env=env)
        save_json(imported / "migration-source.json", {
            "gitlab": plan["gitlab"], "project_id": int(project["id"]),
            "export_file": archive_path.name})
        verify_local_mirror(imported, env)
        # Check both resolved paths before moving a directory on Windows.
        parent = mirror.parent.resolve()
        if (not imported.resolve().is_relative_to(parent) or
                mirror.resolve().parent != parent):
            raise MigrationError("Offline mirror paths leave the intended mirrors directory.")
        os.replace(imported, mirror)


def ensure_offline_mirror(project, mirror, plan, state):
    env = local_source_environment()
    if mirror.exists():
        marker = mirror / "migration-source.json"
        if marker.exists():
            source = load_json(marker)
            if (source.get("gitlab") != plan["gitlab"] or
                    source.get("project_id") != int(project["id"])):
                raise MigrationError("The existing mirror belongs to a different source project.")
        else:
            source = run(["git", "-C", mirror, "remote", "get-url", "origin"], env=env)
            if source not in (project.get("http_url_to_repo"), project.get("ssh_url_to_repo")):
                raise MigrationError("The existing mirror's origin does not match this project.")
        log("  Using the existing local mirror; no GitLab fetch")
        verify_local_mirror(mirror, env)
        return env
    entry = state.get("exports", {}).get(str(project["id"]), {})
    filename = entry.get("file")
    if (entry.get("status") != "complete" or not isinstance(filename, str) or
            Path(filename).name != filename or "/" in filename or "\\" in filename):
        raise MigrationError(
            "No completed local export or Git mirror for this project. Offline mode "
            "does not connect to GitLab. Use the same --work-dir used for archive.")
    folder = mirror.parent.parent / "exports"
    archive_path = (folder / filename).resolve()
    if not archive_path.is_relative_to(folder.resolve()) or not archive_path.is_file():
        raise MigrationError(f"Saved project export is missing: {filename}")
    log(f"  Building a Git mirror from saved export {filename}; no GitLab connection")
    mirror_from_export(project, mirror, archive_path, plan, env)
    return env


def verify_local_lfs(mirror, env):
    storage = mirror / "lfs"
    listing = run(["git", "-C", mirror, "-c", f"lfs.storage={storage}",
                   "lfs", "ls-files", "--all", "--long"], env=env)
    objects = set()
    for line in listing.splitlines():
        match = re.match(r"^([0-9a-f]{64}) [*-] ", line)
        if not match:
            raise MigrationError("Cannot interpret local LFS object listing.")
        objects.add(match.group(1))
    for oid in sorted(objects):
        path = storage / "objects" / oid[:2] / oid[2:4] / oid
        if not path.is_file():
            raise MigrationError(
                f"Local LFS object {oid} is missing. Offline mode cannot download it. "
                "Obtain a complete export or fetch LFS objects before retrying.")
        if sha256_file(path) != oid:
            raise MigrationError(f"Local LFS object {oid} failed its SHA-256 check.")
    log(f"  Verified {len(objects)} local LFS objects without downloading")


def offline_github_environment(env, mirror, target_url):
    env = dict(env)
    settings = {"lfs.url": target_url + "/info/lfs",
                "lfs.pushurl": target_url + "/info/lfs",
                "lfs.storage": str(mirror / "lfs")}
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    for key, value in settings.items():
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def local_refs(mirror):
    text = run(["git", "-C", mirror, "for-each-ref",
                "--format=%(refname) %(objectname)", *SCOPES])
    return dict(line.split(" ", 1) for line in text.splitlines() if line)


def remote_refs(url, env=None):
    text = run(["git", "ls-remote", "--refs", url,
                "refs/heads/*", "refs/tags/*", "refs/notes/*"], env=env)
    return {ref: sha for sha, ref in (line.split() for line in text.splitlines() if line)}


def publish_refs(mirror, target, env=None):
    expected = local_refs(mirror)
    if expected:
        # Explicit refspecs: no force, no deletion, no GitLab internal MR refs on GitHub.
        refspecs = [f"{scope}*:{scope}*" for scope in SCOPES
                    if any(ref.startswith(scope) for ref in expected)]
        run(["git", "-C", mirror, "push", "--atomic", target, *refspecs], env=env)
    actual = remote_refs(target, env=env)
    if expected != actual:
        raise MigrationError("Destination branch/tag/note hashes differ from the source. "
                             "No force push will be attempted.")
    return expected


def write_report(work, plan, state):
    with (work / "report.csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(["gitlab_path", "github_target", "status", "verified_refs", "error"])
        for p in plan["projects"]:
            item = state["repositories"].get(str(p["id"]), {})
            writer.writerow([p["path_with_namespace"],
                             f"{plan['github_owner']}/{p['github_name']}",
                             item.get("status", "pending") if p["include"] else "excluded",
                             item.get("verified_refs", ""), item.get("error", "")])


def migrate_one(project, plan, state, state_path, args, api, github):
    key = str(project["id"])
    target = f"{plan['github_owner']}/{project['github_name']}"
    target_url = f"https://github.com/{target}.git"
    github_env = github.git_environment(plan["github_owner"])
    record = state["repositories"].get(key)
    if record and record["target"].casefold() != target.casefold():
        raise MigrationError("Destination changed after a migration attempt. "
                             "Use a separate --work-dir for the new mapping.")
    if record and record.get("github_id"):
        repo = github.request(f"repos/{target}")
        if repo["id"] != record["github_id"] or not repo["private"]:
            raise MigrationError("Recorded GitHub repository changed identity or visibility.")
        if record.get("status") == "complete":
            log("  Already completed; skipping")
            return
    mirror = Path(args.work_dir).resolve() / "mirrors" / f"project-{key}.git"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    offline = getattr(args, "offline_source", False)
    if offline:
        source_env = ensure_offline_mirror(project, mirror, plan, state)
        github_env = offline_github_environment(github_env, mirror, target_url)
    else:
        source_env = ensure_mirror(project, mirror, args.clone_protocol, api)
    if not args.skip_lfs and local_refs(mirror):
        if offline:
            verify_local_lfs(mirror, source_env)
        else:
            log("  Downloading all source LFS objects")
            run(["git", "-C", mirror, "lfs", "fetch", "--all", "origin"], env=source_env)
    if not record or not record.get("github_id"):
        log("  Creating a private GitHub repository")
        repo = github.request("user/repos", fields={
            "name": project["github_name"], "private": True, "auto_init": False,
            "description": (project.get("description") or "")[:350]})
        record = {"target": target, "github_id": repo["id"], "status": "created"}
        state["repositories"][key] = record
        save_json(state_path, state)
    if not args.skip_lfs and local_refs(mirror):
        log("  Uploading all LFS objects to GitHub")
        lfs_url = target_url + "/info/lfs"
        run(["git", "-C", mirror, "-c", f"lfs.url={lfs_url}",
             "-c", f"lfs.pushurl={lfs_url}", "lfs", "push", "--all", target_url], env=github_env)
    log("  Pushing branches, tags and notes; verifying hashes")
    refs = publish_refs(mirror, target_url, env=github_env)
    branch = project.get("default_branch")
    if branch and f"refs/heads/{branch}" in refs:
        github.request(f"repos/{target}", fields={"default_branch": branch}, method="PATCH")
    record.update({"status": "complete", "verified_refs": len(refs), "error": "",
                   "completed_at": datetime.now(timezone.utc).isoformat(),
                   "lfs": "skipped_by_user" if args.skip_lfs else "transferred"})
    save_json(state_path, state)


def migrate(args):
    for executable in ("git",):
        if not shutil.which(executable):
            raise MigrationError(f"Install {executable} and reopen your terminal.")
    if not args.skip_lfs:
        run(["git", "lfs", "version"])
    plan = load_json(args.plan)
    projects = validate_plan(plan)
    github = GitHub(token_for_github())
    account = github.request("user")
    if account["login"].casefold() != plan["github_owner"].casefold():
        raise MigrationError(
            f"Your GitHub token belongs to {account['login']!r}, but github_owner in "
            f"{args.plan} is {plan['github_owner']!r}. Set github_owner to the intended "
            "destination GitHub username or use a token for the planned owner "
            "(check GITHUB_TOKEN if set). Your GitLab username may be different. "
            "GitHub CLI is not required.")
    work = Path(args.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    state_path = work / "state.json"
    state = load_state(state_path, plan)
    offline = getattr(args, "offline_source", False)
    api = None if offline else GitLab(plan["gitlab"], token_for_gitlab(), args.ca_file)
    if offline:
        log("Offline source mode: using local Git mirrors/project exports; no GitLab access.")
    failures = 0
    for index, project in enumerate(projects, 1):
        key = str(project["id"])
        target = f"{plan['github_owner']}/{project['github_name']}"
        log(f"[{index}/{len(projects)}] {project['path_with_namespace']} -> {target}")
        try:
            migrate_one(project, plan, state, state_path, args, api, github)
        except (MigrationError, OSError) as exc:
            failures += 1
            record = state["repositories"].setdefault(key, {"target": target})
            record.update({"status": "failed", "error": clean(exc)})
            save_json(state_path, state)
            log(f"  FAILED: {exc}")
        write_report(work, plan, state)
    log(f"Finished: {len(projects) - failures} completed/already complete, {failures} failed.")
    log(f"Report: {work / 'report.csv'}. Re-run the same command to retry failed projects.")
    return 1 if failures else 0


def archive(args):
    plan = load_json(args.plan)
    projects = validate_plan(plan)
    work = Path(args.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    folder = work / "exports"
    folder.mkdir(exist_ok=True)
    state_path = work / "state.json"
    state = load_state(state_path, plan)
    api = GitLab(plan["gitlab"], token_for_gitlab(), args.ca_file,
                 export_download_interval=getattr(args, "export_download_interval", 0))
    failures = 0
    for index, p in enumerate(projects, 1):
        key = str(p["id"])
        previous = state["exports"].get(key, {})
        log(f"[{index}/{len(projects)}] Exporting {p['path_with_namespace']}")
        try:
            if not args.refresh and previous.get("file"):
                existing = folder / previous["file"]
                if existing.is_file() and existing.stat().st_size:
                    log("  Existing downloaded export; skipping (use --refresh for a new snapshot)")
                    continue
            state["exports"][key] = {"status": "in_progress",
                                     "path_with_namespace": p["path_with_namespace"]}
            save_json(state_path, state)
            api.request(f"projects/{key}/export", method="POST")
            started, last_status = time.monotonic(), None
            while True:
                status = api.request(f"projects/{key}/export").get("export_status")
                if status != last_status:
                    log(f"  Export status: {status}")
                    last_status = status
                if status == "finished":
                    break
                if time.monotonic() - started > args.export_timeout:
                    raise MigrationError("Export timed out; check GitLab Sidekiq/export settings.")
                time.sleep(args.poll_interval)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            name = f"project-{key}-{stamp}.tar.gz"
            api.download_export(key, folder / name)
            state["exports"][key] = {"file": name, "status": "complete",
                                     "path_with_namespace": p["path_with_namespace"]}
            save_json(state_path, state)
            log(f"  Saved {folder / name}")
        except (MigrationError, OSError) as exc:
            failures += 1
            state["exports"][key] = {"status": "failed", "error": clean(exc)}
            save_json(state_path, state)
            log(f"  FAILED: {exc}")
    log(f"Export pass finished: {failures} failed. These archives are not GitHub issue imports.")
    return 1 if failures else 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("plan", help="Discover projects; save mapping without GitHub writes")
    discover.add_argument("--gitlab", required=True, help="LAN GitLab base URL")
    discover.add_argument("--owner", required=True, help="Your personal GitHub username")
    discover.add_argument("--plan", default="migration-plan.json")
    discover.add_argument("--group", action="append", help="Exact group path; repeat to include groups")
    discover.add_argument("--membership-only", action="store_true",
                          help="Only projects where the API user is a member")
    discover.add_argument("--ca-file", help="Trusted internal CA PEM file, if required")
    discover.set_defaults(handler=make_plan)
    for name, handler in (("migrate", migrate), ("archive", archive)):
        command = sub.add_parser(name)
        command.add_argument("--plan", default="migration-plan.json")
        command.add_argument("--work-dir", default="migration-data")
        command.add_argument("--ca-file", help="Trusted internal CA PEM file, if required")
        command.set_defaults(handler=handler)
        if name == "migrate":
            command.add_argument("--offline-source", action="store_true",
                                 help="Use saved mirrors/exports and local LFS; never connect to GitLab")
            command.add_argument("--clone-protocol", choices=("https", "ssh"), default="https",
                                 help="https uses the GitLab token; ssh uses your existing key")
            command.add_argument("--skip-lfs", action="store_true",
                                 help="Only use when you know no selected project needs LFS")
        else:
            command.add_argument("--refresh", action="store_true",
                                 help="Create a new export snapshot; retain previous files")
            command.add_argument("--export-download-interval", type=float, default=0,
                                 help="Optional gap between successful export requests; default 0 (no gap)")
            command.add_argument("--export-timeout", type=int, default=7200)
            command.add_argument("--poll-interval", type=int, default=5)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except (MigrationError, OSError, ValueError, KeyError) as exc:
        log(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        log("Interrupted. Keep the work directory and rerun to resume.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
