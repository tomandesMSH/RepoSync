# RepoSync

Clones and pulls all repositories of selected GitHub users.
Python 3.9+ and Git required.

Exit code is 1 if anything failed (warnings don't affect the exit code).

## config.json

Simplest case: just a list of users, everything else default:

```json
{
  "where": "C:/Users/account/documents/repos",
  "github_username": ["tomandesmsh", "christitustech"]
}
```

Filters (`exclude`/`only`/`private`/...) are written right after the name in square brackets — no nested objects:

```json
{
  "where": "C:/Users/account/documents/repos",
  "github_username": [
    "tomandesmsh [exclude: QUbuntu-Android, 3D-Model]",
    "christitustech [exclude: linutil; only: winutil]",
    "romanKaxo "
  ]
}
```

Syntax: `name [exclude: pattern1, pattern2; only: pattern3; private: false]` 
filters are optional, separated by `;`:

| filter       | value                       | meaning                                                                                |
| ------------ | --------------------------- | -------------------------------------------------------------------------------------- |
| `exclude`  | patterns separated by `,` | glob patterns of names to skip (`test-*`, `scratch`)                               |
| `only`     | patterns separated by `,` | when present, **only** matching names are synced                                 |
| `private`  | `true`/`false`          | also sync this user's private repos (requires a `GITHUB_TOKEN` that belongs to them) |
| `forks`    | `true`/`false`          | also sync forks                                                                        |
| `archived` | `true`/`false`          | also sync archived repos                                                               |

A name without brackets means no filter (everything default).

Anything at the top level of the config besides `where` and `github_username` is a default applied to every user (a single user can override it with an inline filter above):

```json
{
  "where": "./repos",
  "protocol": "https",
  "include_private": true,
  "github_username": ["tomandesmsh [exclude: scratch]"]
}
```

| key                  | default     | meaning                                                                                                                          |
| -------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `where`            | `./repos` | where to clone into; a relative path is resolved against the config file's location. Each user gets a subfolder `where/<name>` |
| `protocol`         | `ssh`     | `ssh` or `https`                                                                                                             |
| `include_forks`    | `false`   | also clone forks (globally; or inline `[forks: true]`)                                                                         |
| `include_archived` | `false`   | also clone archived repos (globally; or inline `[archived: true]`)                                                             |
| `include_private`  | `true`    | also clone private repos (globally; or inline `[private: false]`) — only works for the token's owner                          |
| `exclude`          | `[]`      | glob patterns of names to skip (globally; or inline `[exclude: ...]`)                                                          |
| `only`             | `[]`      | when non-empty, **only** matching names are synced (globally; or inline `[only: ...]`)                                   |
| `strategy`         | `ff-only` | `ff-only` = fast-forward merge only, `rebase` = rebase onto upstream, `fetch-only` = just fetch, never change anything     |

For exceptions that don't fit in `[...]` (e.g. a custom subfolder name `dir`), use a full JSON object instead of a string: `{"name": "tomandesmsh", "dir": "mine", "exclude": ["scratch"]}`.

The GitHub token (optional, but without it you only get public repos and a 60 requests/hour limit) is read from the `GITHUB_TOKEN` environment variable:

```powershell
$env:GITHUB_TOKEN = "ghp_..."     # PowerShell
export GITHUB_TOKEN=ghp_...       # bash
```

## What it does with existing repositories

Always runs `git fetch --prune --tags` first, then acts based on the current branch's status:

| state                                   | what happens                                                       |
| --------------------------------------- | ------------------------------------------------------------------ |
| up to date                              | `OK`                                                             |
| behind, clean tree                      | fast-forward / rebase → `UPDATED`                               |
| behind, **uncommitted changes**   | `WARN`, not updated                                              |
| ahead                                   | `WARN` – don't forget to push                                   |
| **diverged** (ahead and behind)   | `WARN: DIVERGED` – merge conflict risk, left for you to resolve |
| detached HEAD / branch without upstream | `WARN`, not updated                                              |

The program **never** performs a merge that could end in conflict, never touches uncommitted changes, and never overwrites anything — it only warns in risky cases. As notes (`!`) it also reports stash entries and cases where `origin` points somewhere other than what the config expects.

## New repositories

Before cloning, it runs `git ls-remote` as a quick preflight check (no data downloaded) to confirm the remote is reachable and auth works — so it never blindly clones into a nonexistent/unreachable repo and leaves a half-created folder behind. A failure here is reported with a clear reason (e.g. "Repository not found" or "Host key verification failed") before anything touches disk.
