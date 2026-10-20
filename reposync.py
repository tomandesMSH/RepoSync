#!/usr/bin/env python3
"""reposync - clones / updates all repositories of selected GitHub users.

Configuration lives in config.json (see README.md). No external dependencies.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API = "https://api.github.com"

OK, CHANGED, WARN, FAIL, SKIP = "ok", "changed", "warn", "fail", "skip"

COLORS = {OK: "\033[32m", CHANGED: "\033[36m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[90m"}
RESET = "\033[0m"
USE_COLOR = sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))


def tag(status: str) -> str:
    label = {OK: "OK     ", CHANGED: "UPDATED", WARN: "WARN   ", FAIL: "FAIL   ", SKIP: "SKIP   "}[status]
    return f"{COLORS[status]}{label}{RESET}" if USE_COLOR else label


# --------------------------------------------------------------------------- config


@dataclass
class UserCfg:
    name: str
    dir: str
    protocol: str
    include_forks: bool
    include_archived: bool
    include_private: bool
    strategy: str
    exclude: list[str] = field(default_factory=list)
    only: list[str] = field(default_factory=list)


@dataclass
class Config:
    root: Path
    users: list[UserCfg]
    token: str | None


DEFAULTS = {
    "protocol": "ssh",
    "include_forks": False,
    "include_archived": False,
    "include_private": True,
    "strategy": "ff-only",
    "exclude": [],
    "only": [],
}

# top-level keys that are NOT defaults for every user
TOP_LEVEL_KEYS = {"where", "github_username"}

# "name [exclude: a, b; only: c; private: false]" - name plus optional filters in square brackets
INLINE_RE = re.compile(r"^\s*(?P<name>[^\[\]]+?)\s*(?:\[(?P<filters>.*)\])?\s*$")

LIST_FILTER_KEYS = {"exclude", "only"}
# bool filter -> matching UserCfg field name
BOOL_FILTER_KEYS = {"private": "include_private", "forks": "include_forks", "archived": "include_archived"}


def parse_bool(raw: str, context: str) -> bool:
    value = raw.strip().lower()
    if value in ("true", "yes", "1"):
        return True
    if value in ("false", "no", "0"):
        return False
    sys.exit(f"{context}: '{raw.strip()}' is not a valid bool value (use true/false)")


def parse_inline_user(entry: str) -> dict:
    """Parses 'name' or 'name [exclude: a, b; only: c; private: false]'."""
    match = INLINE_RE.match(entry)
    if not match or not match.group("name"):
        sys.exit(f"cannot parse entry in 'github_username': {entry!r}")
    parsed: dict = {"name": match.group("name")}
    filters = match.group("filters")
    if not filters:
        return parsed
    for clause in filters.split(";"):
        clause = clause.strip()
        if not clause:
            continue
        if ":" not in clause:
            sys.exit(f"{entry!r}: filter '{clause}' is missing ':' (e.g. 'exclude: test-*')")
        key, value = clause.split(":", 1)
        key = key.strip()
        if key in LIST_FILTER_KEYS:
            parsed[key] = [v.strip() for v in value.split(",") if v.strip()]
        elif key in BOOL_FILTER_KEYS:
            parsed[BOOL_FILTER_KEYS[key]] = parse_bool(value, f"{entry!r}, filter '{key}'")
        else:
            allowed = ", ".join([*LIST_FILTER_KEYS, *BOOL_FILTER_KEYS])
            sys.exit(f"{entry!r}: unknown filter '{key}' (allowed: {allowed})")
    return parsed


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"config not found: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"config {path} is not valid JSON: {e}")

    # anything at the top level (besides where/github_username) is a default for every user
    defaults = {**DEFAULTS, **{k: v for k, v in raw.items() if k not in TOP_LEVEL_KEYS}}
    root = Path(raw.get("where", "./repos")).expanduser()
    if not root.is_absolute():
        root = (path.resolve().parent / root).resolve()

    users: list[UserCfg] = []
    for entry in raw.get("github_username", []):
        if isinstance(entry, str):
            entry = parse_inline_user(entry)
        name = entry.get("name")
        if not name:
            sys.exit("every entry in 'github_username' must have a name")
        merged = {**defaults, **entry}
        if merged["strategy"] not in ("ff-only", "rebase", "fetch-only"):
            sys.exit(f"{name}: unknown strategy {merged['strategy']!r}")
        if merged["protocol"] not in ("ssh", "https"):
            sys.exit(f"{name}: unknown protocol {merged['protocol']!r}")
        users.append(
            UserCfg(
                name=name,
                dir=merged.get("dir") or name,
                protocol=merged["protocol"],
                include_forks=merged["include_forks"],
                include_archived=merged["include_archived"],
                include_private=merged["include_private"],
                strategy=merged["strategy"],
                exclude=list(merged["exclude"]),
                only=list(merged["only"]),
            )
        )
    if not users:
        sys.exit("no users in config")

    token = os.environ.get("GITHUB_TOKEN") or None
    return Config(root=root, users=users, token=token)


# --------------------------------------------------------------------------- github


def api_get(url: str, token: str | None) -> tuple[list | dict, dict]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "reposync"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp), dict(resp.headers)


def next_link(headers: dict) -> str | None:
    for part in headers.get("Link", "").split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip(" <>")
    return None


def authenticated_login(token: str | None) -> str | None:
    if not token:
        return None
    try:
        me, _ = api_get(f"{API}/user", token)
        return me.get("login")
    except (urllib.error.URLError, OSError):
        return None


def list_repos(user: UserCfg, token: str | None, me: str | None) -> list[dict]:
    """Returns the user's repositories; includes private ones for the authenticated user."""
    own = bool(me) and me.lower() == user.name.lower()
    if own and user.include_private:
        url = f"{API}/user/repos?per_page=100&affiliation=owner&sort=full_name"
    else:
        url = f"{API}/users/{user.name}/repos?per_page=100&type=owner&sort=full_name"

    repos: list[dict] = []
    while url:
        try:
            page, headers = api_get(url, token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(f"user '{user.name}' does not exist (or is not visible without a token)")
            if e.code in (401, 403):
                if e.headers.get("X-RateLimit-Remaining") == "0":
                    raise RuntimeError("GitHub rate limit exhausted - set GITHUB_TOKEN")
                raise RuntimeError(f"GitHub rejected the request ({e.code}) - check your token")
            raise RuntimeError(f"GitHub API error {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"network unavailable: {e.reason}")
        repos.extend(page)
        url = next_link(headers)
    return repos


def wanted(repo: dict, user: UserCfg) -> tuple[bool, str]:
    name = repo["name"]
    if user.only and not any(fnmatch.fnmatch(name, p) for p in user.only):
        return False, "not in 'only'"
    for pattern in user.exclude:
        if fnmatch.fnmatch(name, pattern):
            return False, f"excluded by pattern '{pattern}'"
    if repo.get("fork") and not user.include_forks:
        return False, "fork"
    if repo.get("archived") and not user.include_archived:
        return False, "archived"
    if repo.get("private") and not user.include_private:
        return False, "private"
    return True, ""


def clone_url(repo: dict, user: UserCfg) -> str:
    return repo["ssh_url"] if user.protocol == "ssh" else repo["clone_url"]


# --------------------------------------------------------------------------- git


class GitError(RuntimeError):
    pass


def git(args: list[str], cwd: Path | None = None, check: bool = True, timeout: float | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)} did not respond within {timeout}s - check network/keys")
    if check and proc.returncode != 0:
        raise GitError(proc.stderr.strip() or proc.stdout.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def first_error_line(text: str) -> str:
    """First descriptive line of a git error (git prints progress like 'Cloning into ...' first)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.lower().startswith("cloning into")]
    return lines[0] if lines else text.strip()


def check_remote(url: str, timeout: float = 20) -> None:
    """Quickly (without downloading any data) verifies the repo is reachable and auth works.

    Used as a preflight check before 'git clone', so we never blindly clone into a
    nonexistent/unreachable repository, and a failure gets a clear diagnosis before
    anything is written to disk.
    """
    git(["ls-remote", "--exit-code", url, "HEAD"], timeout=timeout)


@dataclass
class Result:
    repo: str
    status: str
    message: str


def sync_repo(repo: dict, user: UserCfg, dest: Path, dry_run: bool) -> Result:
    name = repo["name"]
    url = clone_url(repo, user)

    if not (dest / ".git").is_dir():
        if dest.exists() and any(dest.iterdir()):
            return Result(name, FAIL, f"{dest} exists and is not a git repository")

        try:
            check_remote(url)
        except GitError as e:
            return Result(name, FAIL, f"remote repository unreachable, not cloning: {first_error_line(str(e))}")

        if dry_run:
            return Result(name, CHANGED, f"would clone from {url}")
        try:
            git(["clone", url, str(dest)])
        except GitError as e:
            return Result(name, FAIL, f"clone failed: {first_error_line(str(e))}")
        return Result(name, CHANGED, "cloned")

    return update_repo(name, url, user, dest, dry_run)


def update_repo(name: str, url: str, user: UserCfg, dest: Path, dry_run: bool) -> Result:
    notes: list[str] = []

    origin = git(["remote", "get-url", "origin"], dest, check=False)
    if origin and origin.rstrip("/") != url.rstrip("/"):
        notes.append(f"origin is '{origin}', config expects '{url}'")

    dirty = git(["status", "--porcelain", "--untracked-files=no"], dest, check=False)
    stash = git(["stash", "list"], dest, check=False)

    try:
        git(["fetch", "--prune", "--tags", "origin"], dest)
    except GitError as e:
        return Result(name, FAIL, f"fetch failed: {first_error_line(str(e))}")

    branch = git(["symbolic-ref", "--quiet", "--short", "HEAD"], dest, check=False)
    if not branch:
        return Result(name, WARN, join_notes("detached HEAD - not updating", notes))

    upstream = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], dest, check=False)
    if not upstream:
        return Result(name, WARN, join_notes(f"branch '{branch}' has no upstream - not updating", notes))

    counts = git(["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], dest, check=False)
    try:
        behind, ahead = (int(x) for x in counts.split())
    except ValueError:
        return Result(name, FAIL, join_notes("could not determine status vs. upstream", notes))

    if dirty:
        notes.append(f"{len(dirty.splitlines())} uncommitted change(s) in the working tree")
    if stash:
        notes.append(f"{len(stash.splitlines())} entr(ies) in the stash")

    if behind and ahead:
        return Result(name, WARN, join_notes(
            f"DIVERGED: {branch} is {ahead} commit(s) ahead and {behind} behind {upstream}"
            " - merge conflict risk, resolve manually", notes))
    if ahead and not behind:
        return Result(name, WARN, join_notes(
            f"{branch} is {ahead} commit(s) ahead of {upstream} - don't forget to push", notes))
    if not behind:
        return Result(name, OK, join_notes(f"up to date ({branch})", notes))

    # behind > 0, ahead == 0
    if user.strategy == "fetch-only":
        return Result(name, WARN, join_notes(
            f"{behind} new commit(s) available (strategy fetch-only)", notes))
    if dirty:
        return Result(name, WARN, join_notes(
            f"{behind} new commit(s), but the working tree has uncommitted changes - not updating", notes))
    if dry_run:
        return Result(name, CHANGED, join_notes(f"would pull {behind} commit(s) into {branch}", notes))

    args = ["merge", "--ff-only", upstream] if user.strategy == "ff-only" else ["rebase", upstream]
    try:
        git(args, dest)
    except GitError as e:
        git(["rebase", "--abort"], dest, check=False)
        return Result(name, FAIL, join_notes(f"{user.strategy} failed: {first_error_line(str(e))}", notes))
    return Result(name, CHANGED, join_notes(f"+{behind} commit(s) into {branch}", notes))


def join_notes(message: str, notes: list[str]) -> str:
    return message + "".join(f"\n{' ' * 13}! {n}" for n in notes)


# --------------------------------------------------------------------------- run


def run_user(user: UserCfg, cfg: Config, me: str | None, args) -> list[Result]:
    print(f"\n=== {user.name} ===")
    try:
        repos = list_repos(user, cfg.token, me)
    except RuntimeError as e:
        print(f"  {tag(FAIL)} {e}")
        return [Result(f"{user.name}/*", FAIL, str(e))]

    selected: list[tuple[dict, str]] = []
    skipped: list[tuple[dict, str]] = []
    for repo in repos:
        keep, reason = wanted(repo, user)
        (selected if keep else skipped).append((repo, reason))

    print(f"  {len(repos)} repo(s) total, {len(selected)} to sync, {len(skipped)} skipped")
    if args.verbose:
        for repo, reason in skipped:
            print(f"  {tag(SKIP)} {repo['name']}: {reason}")

    base = cfg.root / user.dir
    if not args.dry_run:
        base.mkdir(parents=True, exist_ok=True)

    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(sync_repo, repo, user, base / repo["name"], args.dry_run): repo
            for repo, _ in selected
        }
        for future in concurrent.futures.as_completed(futures):
            repo = futures[future]
            try:
                results.append(future.result())
            except Exception as e:  # a single repo must never bring down the whole run
                results.append(Result(repo["name"], FAIL, f"unexpected error: {e}"))

    for result in sorted(results, key=lambda r: r.repo.lower()):
        if result.status == OK and not args.verbose:
            continue
        print(f"  {tag(result.status)} {result.repo}: {result.message}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clones / updates all repositories of the users listed in config.json")
    parser.add_argument("-c", "--config", default="config.json", type=Path, help="path to the config file")
    parser.add_argument("-u", "--user", action="append", help="only this user (repeatable)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="only show what would happen")
    parser.add_argument("-j", "--jobs", type=int, default=4, help="number of concurrent repos (default 4)")
    parser.add_argument("-v", "--verbose", action="store_true", help="also print up-to-date and skipped repos")
    args = parser.parse_args()

    cfg = load_config(args.config)
    users = cfg.users
    if args.user:
        names = {u.lower() for u in args.user}
        users = [u for u in users if u.name.lower() in names]
        if not users:
            sys.exit(f"none of the users {args.user} are in the config")

    me = authenticated_login(cfg.token)
    print(f"root directory: {cfg.root}")
    print("token: " + (f"yes, logged in as {me}" if me else "no (public repos only, lower rate limit)"))
    if args.dry_run:
        print("DRY RUN - nothing is written")

    all_results: list[Result] = []
    for user in users:
        all_results.extend(run_user(user, cfg, me, args))

    counts = {s: sum(1 for r in all_results if r.status == s) for s in (OK, CHANGED, WARN, FAIL)}
    print(f"\ndone: {counts[CHANGED]} updated, {counts[OK]} up to date, "
          f"{counts[WARN]} warning(s), {counts[FAIL]} error(s)")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
