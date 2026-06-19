# Cloud auth (GH_TOKEN passthrough) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `cloud-auth` (MOS-187) — approved. Read it at `specs/2026-06-17-cloud-auth.md` in the workspace metarepo.

**Goal:** Make `mship bootstrap` and `mship finish` work in a cloud session with no git creds / no `gh`, by auto-configuring auth from an env-injected token (precedence `--token` > `GH_TOKEN` > `GITHUB_TOKEN`), with a clear actionable error when none is present.

**Architecture:** A new single-purpose `core/gh_auth.py` turns a token into (a) a non-persistent, github.com-scoped `git -c credential.helper` (token passed via subprocess env, never argv/disk) for clone+push, and (b) a gh-independent httpx PR-creation path. `bootstrap` splices the cred args onto `git clone`; `finish` (`core/pr.py` + the finish CLI) splices them onto `git push` and routes PR creation to `gh pr create` when gh is usable, else to the httpx path. Reuses the existing `_parse_github_slug` for owner/repo (satisfies the spec's `parse_owner_repo` criterion).

**Tech Stack:** Python 3.14, Typer, httpx (already a dep, `httpx>=0.27`), pytest, `uv`. Git/gh via `ShellRunner` (`run(command, cwd, env=None)` merges `env` over `os.environ`).

**Where to work:** all paths are relative to the `mothership` worktree:
`/home/bailey/development/repos/mship-workspace/.worktrees/cloud-auth/mothership`. `cd` there; task slug `cloud-auth`, branch `feat/cloud-auth`. Targeted tests: `uv run pytest …`; full suite: `mship test`.

---

## Key facts from the codebase (verified)

- `bootstrap._clone_one` clones via `shell.run(f"git clone {shlex.quote(url)} {shlex.quote(str(path))}", cwd=workspace_root)` (`src/mship/core/bootstrap.py:51`).
- `PRManager` (`src/mship/core/pr.py`) is a DI `providers.Factory(PRManager, shell=shell)` — obtained as `container.pr_manager()`; do NOT add a `token` constructor arg (the factory only passes `shell`). Thread the token through method params instead.
- `PRManager.push_branch(repo_path, branch)` runs `git push -u origin <branch>`.
- `PRManager.create_pr(repo_path, branch, title, body, base=None)` runs `gh pr create …` (with a gh-api REST fallback that still needs `gh`, only for GraphQL rate-limits).
- `PRManager.check_gh_available()` runs `gh auth status`; returncode `127` = gh not installed, non-zero = not authed.
- `_parse_github_slug(remote_url) -> tuple[str,str] | None` already handles `https`, `https…​.git`, and `git@github.com:o/r.git`, returns `None` for non-github. **Reuse it.**
- Finish CLI (`src/mship/cli/worktree.py`): `check_gh_available()` guard at ~`:1082`; `push_branch` calls at ~`:1050`, `:1275`, `:1295`; `create_pr` call at ~`:1354`. The `finish` command def is at ~`:802`. The `bootstrap` CLI is `src/mship/cli/bootstrap.py`.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/mship/core/gh_auth.py` (new) | `resolve_token`, `git_cred_args`, `create_pr_via_httpx`, `get_default_branch_via_httpx` | 1,2 |
| `src/mship/core/bootstrap.py` | resolve token, splice cred args onto `git clone`, actionable no-token error | 3 |
| `src/mship/cli/bootstrap.py` | `--token` option | 3 |
| `src/mship/core/pr.py` | `gh_usable()`, token on `push_branch`/`create_pr`, httpx branch | 4 |
| `src/mship/cli/worktree.py` | resolve token in `finish`, token-aware gh guard, thread token to push/create, `--token` | 4 |
| `tests/core/test_gh_auth.py` (new) | unit tests for all of gh_auth | 1,2 |
| `tests/core/test_bootstrap.py` | bootstrap token-wiring tests | 3 |
| `tests/core/test_pr.py` | PRManager token/gh-vs-httpx tests | 4 |

---

## Task 1: `gh_auth.py` — token resolution + git credential args

**Files:**
- Create: `src/mship/core/gh_auth.py`
- Test: `tests/core/test_gh_auth.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_gh_auth.py`:

```python
import pytest

from mship.core.gh_auth import resolve_token, git_cred_args


def test_resolve_token_precedence(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh_env")
    monkeypatch.setenv("GITHUB_TOKEN", "github_env")
    assert resolve_token("explicit") == "explicit"          # flag wins
    assert resolve_token(None) == "gh_env"                   # GH_TOKEN next
    monkeypatch.delenv("GH_TOKEN")
    assert resolve_token(None) == "github_env"               # GITHUB_TOKEN last
    monkeypatch.delenv("GITHUB_TOKEN")
    assert resolve_token(None) is None                       # none


def test_resolve_token_blank_is_none(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert resolve_token("   ") is None                      # blank flag ignored


def test_git_cred_args_token_only_in_env_not_args():
    args, env = git_cred_args("secret-tok")
    # token is carried only in the env dict, never in the args
    assert "secret-tok" not in " ".join(args)
    assert env["MSHIP_GH_TOKEN"] == "secret-tok"
    # the helper config is scoped to github.com and is a -c override
    assert args[0] == "-c"
    assert args[1].startswith('credential.https://github.com.helper=')
    # the helper reads the token from the env var, not a literal
    assert "$MSHIP_GH_TOKEN" in args[1]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_gh_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mship.core.gh_auth'`.

- [ ] **Step 3: Implement**

Create `src/mship/core/gh_auth.py`:

```python
"""Configure git/gh auth from an environment token for cloud sessions (MOS-187).

The token is passed to git only via a subprocess env var read by a github.com-
scoped credential helper — never written to disk, never placed in argv.
"""
from __future__ import annotations

import os

# Credential helper: on a `get` request, emit username/password reading the
# token from the env var git inherits. Single-quote-safe (uses double quotes
# internally) so callers can shlex.quote the whole `-c` value.
_CRED_HELPER = (
    '!f() { test "$1" = get && '
    'printf "username=x-access-token\\npassword=%s\\n" "$MSHIP_GH_TOKEN"; }; f'
)
_TOKEN_ENV_VAR = "MSHIP_GH_TOKEN"


def resolve_token(explicit: str | None) -> str | None:
    """Token precedence: explicit (--token) > GH_TOKEN > GITHUB_TOKEN. Blank → None."""
    for candidate in (explicit, os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def git_cred_args(token: str) -> tuple[list[str], dict[str, str]]:
    """Return (`-c` args for a github.com-scoped credential helper, env carrying
    the token). Splice the args into a `git` invocation and pass the env to it.
    The token appears only in the env, never in the args."""
    keyval = f"credential.https://github.com.helper={_CRED_HELPER}"
    return ["-c", keyval], {_TOKEN_ENV_VAR: token}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_gh_auth.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/gh_auth.py tests/core/test_gh_auth.py
git commit -m "feat(gh_auth): token resolution + github-scoped git credential args (MOS-187)"
mship journal "gh_auth: resolve_token precedence + git_cred_args (token in env, not argv); tests passing" --action committed
```

---

## Task 2: `gh_auth.py` — httpx PR creation (gh-independent)

**Files:**
- Modify: `src/mship/core/gh_auth.py`
- Test: `tests/core/test_gh_auth.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_gh_auth.py`:

```python
import json
import httpx

from mship.core.gh_auth import create_pr_via_httpx, get_default_branch_via_httpx


def test_create_pr_via_httpx_posts_and_returns_html_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    url = create_pr_via_httpx(
        "tok", "o", "r", head="feat/x", base="main",
        title="T", body="B", client=client,
    )
    assert url == "https://github.com/o/r/pull/7"
    assert captured["url"] == "https://api.github.com/repos/o/r/pulls"
    assert captured["auth"] == "Bearer tok"
    assert captured["body"] == {"title": "T", "head": "feat/x", "base": "main", "body": "B"}


def test_create_pr_via_httpx_raises_on_error_status():
    def handler(request):
        return httpx.Response(422, json={"message": "Validation Failed"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    import pytest
    with pytest.raises(RuntimeError):
        create_pr_via_httpx("tok", "o", "r", head="h", base="main",
                            title="T", body="B", client=client)


def test_get_default_branch_via_httpx():
    def handler(request):
        assert str(request.url) == "https://api.github.com/repos/o/r"
        return httpx.Response(200, json={"default_branch": "trunk"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_default_branch_via_httpx("tok", "o", "r", client=client) == "trunk"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_gh_auth.py -k httpx -v`
Expected: FAIL — `ImportError` (functions don't exist yet).

- [ ] **Step 3: Implement** — append to `src/mship/core/gh_auth.py`:

```python
import httpx

_API = "https://api.github.com"
_API_HEADERS = {"Accept": "application/vnd.github+json"}


def _client(token: str, client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(timeout=30.0), True


def create_pr_via_httpx(
    token: str, owner: str, repo: str, *, head: str, base: str,
    title: str, body: str, client: httpx.Client | None = None,
) -> str:
    """Open a PR via the GitHub REST API (no gh dependency). Returns html_url."""
    c, owns = _client(token, client)
    try:
        resp = c.post(
            f"{_API}/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
            headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
        )
    finally:
        if owns:
            c.close()
    if resp.status_code >= 300:
        raise RuntimeError(
            f"GitHub PR creation failed ({resp.status_code}): {resp.text[:300]}"
        )
    url = resp.json().get("html_url")
    if not isinstance(url, str) or not url:
        raise RuntimeError("GitHub PR created but response had no html_url")
    return url


def get_default_branch_via_httpx(
    token: str, owner: str, repo: str, *, client: httpx.Client | None = None,
) -> str:
    """Fetch the repo's default branch (used when no base is given)."""
    c, owns = _client(token, client)
    try:
        resp = c.get(
            f"{_API}/repos/{owner}/{repo}",
            headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
        )
    finally:
        if owns:
            c.close()
    if resp.status_code >= 300:
        raise RuntimeError(
            f"Could not fetch default branch ({resp.status_code}): {resp.text[:200]}"
        )
    return resp.json().get("default_branch") or "main"
```

Move the `import httpx` to the top of the file with the other imports (don't leave it mid-file).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_gh_auth.py -v`
Expected: PASS (all 6).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/gh_auth.py tests/core/test_gh_auth.py
git commit -m "feat(gh_auth): httpx PR creation + default-branch lookup (MOS-187)"
mship journal "gh_auth: create_pr_via_httpx + get_default_branch_via_httpx with MockTransport tests; passing" --action committed
```

---

## Task 3: Wire token into `mship bootstrap`

**Files:**
- Modify: `src/mship/core/bootstrap.py`, `src/mship/cli/bootstrap.py`
- Test: `tests/core/test_bootstrap.py`

- [ ] **Step 1: Write the failing tests** — add to `tests/core/test_bootstrap.py`:

```python
def test_bootstrap_clone_includes_cred_args_when_token(tmp_path, monkeypatch):
    # Capture the git clone command instead of really cloning.
    from mship.core import bootstrap as bmod
    calls = []

    class FakeShell:
        def run(self, command, cwd, env=None):
            calls.append((command, env))
            from mship.util.shell import ShellResult
            # pretend the clone produced a dir + Taskfile so later steps no-op
            return ShellResult(returncode=0, stdout="", stderr="")
        def run_task(self, *a, **k):
            from mship.util.shell import ShellResult
            return ShellResult(returncode=0, stdout="", stderr="")

    ws = tmp_path / "ws"; ws.mkdir(); (ws / ".mothership").mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
        "    url: https://github.com/o/lib\n"
    )
    bmod.bootstrap(ws / "mothership.yaml", FakeShell(),
                   state_dir=ws / ".mothership", token="tok123")
    clone = next((c, e) for c, e in calls if "git" in c and "clone" in c)
    cmd, env = clone
    assert "credential.https://github.com.helper" in cmd
    assert "tok123" not in cmd                 # token never in argv
    assert env and env.get("MSHIP_GH_TOKEN") == "tok123"


def test_bootstrap_clone_no_cred_args_without_token(tmp_path):
    from mship.core import bootstrap as bmod
    calls = []

    class FakeShell:
        def run(self, command, cwd, env=None):
            calls.append((command, env))
            from mship.util.shell import ShellResult
            return ShellResult(returncode=0, stdout="", stderr="")
        def run_task(self, *a, **k):
            from mship.util.shell import ShellResult
            return ShellResult(returncode=0, stdout="", stderr="")

    ws = tmp_path / "ws2"; ws.mkdir(); (ws / ".mothership").mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
        "    url: https://github.com/o/lib\n"
    )
    bmod.bootstrap(ws / "mothership.yaml", FakeShell(),
                   state_dir=ws / ".mothership")  # no token
    clone = next(c for c, e in calls if "git" in c and "clone" in c)
    assert "credential.https://github.com.helper" not in clone
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_bootstrap.py -k "cred_args" -v`
Expected: FAIL — `bootstrap()` has no `token` kwarg (TypeError).

- [ ] **Step 3: Implement** — in `src/mship/core/bootstrap.py`:

Add the import near the top:
```python
from mship.core.gh_auth import resolve_token, git_cred_args
```

Change the `bootstrap` signature to accept `token`:
```python
def bootstrap(
    config_path: Path,
    shell: ShellRunner,
    *,
    state_dir: Path,
    repos: list[str] | None = None,
    token: str | None = None,
) -> BootstrapReport:
```

Just after `config = ConfigLoader.load(config_path, require_paths=False)`, resolve once and thread to `_clone_one`:
```python
    resolved_token = resolve_token(token)
```
Update the `_clone_one(...)` call in the list comprehension to pass it:
```python
    results: list[MemberResult] = [
        _clone_one(n, config.repos[n], config.default_remote, workspace_root, shell,
                   resolved_token)
        for n in names
    ]
```

Update `_clone_one` to splice cred args onto the clone:
```python
def _clone_one(
    name: str, repo: RepoConfig, default_remote: str | None,
    workspace_root: Path, shell: ShellRunner, token: str | None = None,
) -> MemberResult:
    path = Path(repo.path)
    if os.path.lexists(path):
        return MemberResult(name, "present", f"already present at {path}")

    url = resolve_clone_url(name, repo, default_remote)
    if url is None:
        return MemberResult(
            name, "error",
            "no resolvable url — set `url` on the member or `default_remote` "
            "on the workspace",
        )

    if token:
        cred_args, cred_env = git_cred_args(token)
        prefix = " ".join(shlex.quote(a) for a in cred_args) + " "
    else:
        prefix, cred_env = "", None
    res = shell.run(
        f"git {prefix}clone {shlex.quote(url)} {shlex.quote(str(path))}",
        cwd=workspace_root, env=cred_env,
    )
    if res.returncode != 0:
        hint = ""
        if token is None and _looks_like_auth_failure(res.stderr):
            hint = (" — authentication failed and no GH_TOKEN/GITHUB_TOKEN found; "
                    "set a token with repo scope or pass --token")
        return MemberResult(
            name, "error",
            f"clone failed: {res.stderr.strip()[:200] or 'unknown'}{hint}",
        )
    # ... (checkout expected/base branch — unchanged from current code) ...
```

Add a small helper near the top of the module:
```python
def _looks_like_auth_failure(stderr: str) -> bool:
    s = stderr.lower()
    return any(k in s for k in (
        "authentication failed", "could not read username",
        "terminal prompts disabled", "permission denied", "403", "fatal: could not read",
    ))
```

(Keep the existing branch-checkout block after the clone success exactly as it is now.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_bootstrap.py -v`
Expected: PASS (all — the new two plus the existing bootstrap tests, which pass `token=None` by default).

- [ ] **Step 5: Add `--token` to the CLI** — in `src/mship/cli/bootstrap.py`, add the option and pass it through:

```python
    def bootstrap(
        repos: Optional[str] = typer.Option(
            None, "--repos", help="Comma-separated repo names (default: all)."
        ),
        token: Optional[str] = typer.Option(
            None, "--token", help="GitHub token for cloning private members "
            "(else GH_TOKEN / GITHUB_TOKEN).",
        ),
    ):
```
and update the core call:
```python
        report = run_bootstrap(config_path, shell, state_dir=state_dir,
                               repos=names, token=token)
```
(Keep the existing `try/except ValueError` wrapper around it.)

- [ ] **Step 6: Verify CLI wiring**

Run: `uv run mship bootstrap --help`
Expected: shows `--token`.

- [ ] **Step 7: Commit**

```bash
git add src/mship/core/bootstrap.py src/mship/cli/bootstrap.py tests/core/test_bootstrap.py
git commit -m "feat(bootstrap): token-authed git clone for private members (MOS-187)"
mship journal "bootstrap: splices github-scoped cred args onto git clone when a token resolves; --token added; tests passing" --action committed
```

---

## Task 4: Wire token into `mship finish` (push + gh-or-httpx PR)

**Files:**
- Modify: `src/mship/core/pr.py`, `src/mship/cli/worktree.py`
- Test: `tests/core/test_pr.py`

- [ ] **Step 1: Write the failing tests** — add to `tests/core/test_pr.py` (create the file if it doesn't exist; mirror its existing import style if it does):

```python
from pathlib import Path

from mship.core.pr import PRManager
from mship.util.shell import ShellResult


class _Shell:
    def __init__(self, gh_returncode=0):
        self.calls = []
        self._gh_rc = gh_returncode
    def run(self, command, cwd, env=None):
        self.calls.append((command, env))
        if command.startswith("gh auth status"):
            return ShellResult(returncode=self._gh_rc, stdout="", stderr="")
        if command.startswith("git remote get-url"):
            return ShellResult(returncode=0, stdout="https://github.com/o/r\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")


def test_push_branch_includes_cred_args_with_token():
    sh = _Shell()
    PRManager(sh).push_branch(Path("/x"), "feat/y", token="tok")
    push = next((c, e) for c, e in sh.calls if "git" in c and "push" in c)
    cmd, env = push
    assert "credential.https://github.com.helper" in cmd
    assert "tok" not in cmd
    assert env and env["MSHIP_GH_TOKEN"] == "tok"


def test_gh_usable_true_when_status_zero():
    assert PRManager(_Shell(gh_returncode=0)).gh_usable() is True


def test_gh_usable_false_when_not_installed():
    assert PRManager(_Shell(gh_returncode=127)).gh_usable() is False


def test_create_pr_uses_httpx_when_gh_absent(monkeypatch):
    sh = _Shell(gh_returncode=127)
    sent = {}
    def fake_httpx(token, owner, repo, *, head, base, title, body, client=None):
        sent.update(owner=owner, repo=repo, head=head, base=base, token=token)
        return "https://github.com/o/r/pull/9"
    monkeypatch.setattr("mship.core.pr.create_pr_via_httpx", fake_httpx)
    url = PRManager(sh).create_pr(Path("/x"), "feat/y", "T", "B", base="main", token="tok")
    assert url == "https://github.com/o/r/pull/9"
    assert sent == {"owner": "o", "repo": "r", "head": "feat/y", "base": "main", "token": "tok"}


def test_create_pr_uses_gh_when_available():
    sh = _Shell(gh_returncode=0)
    # gh pr create returns a URL on stdout
    real_run = sh.run
    def run(command, cwd, env=None):
        if command.startswith("gh pr create"):
            return ShellResult(returncode=0, stdout="https://github.com/o/r/pull/3\n", stderr="")
        return real_run(command, cwd, env)
    sh.run = run
    url = PRManager(sh).create_pr(Path("/x"), "feat/y", "T", "B", base="main", token="tok")
    assert url == "https://github.com/o/r/pull/3"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_pr.py -k "cred_args or gh_usable or httpx or uses_gh" -v`
Expected: FAIL — `push_branch`/`create_pr` have no `token` kwarg and `gh_usable` doesn't exist.

- [ ] **Step 3: Implement** — in `src/mship/core/pr.py`:

Add the import at the top:
```python
from mship.core.gh_auth import git_cred_args, create_pr_via_httpx, get_default_branch_via_httpx
```

Add `gh_usable` to `PRManager`:
```python
    def gh_usable(self) -> bool:
        """True if gh is installed and authenticated (returncode 0)."""
        return self._shell.run("gh auth status", cwd=Path(".")).returncode == 0
```

Update `push_branch` to splice cred args when a token is given:
```python
    def push_branch(self, repo_path: Path, branch: str, token: str | None = None) -> None:
        prefix, env = "", None
        if token:
            args, env = git_cred_args(token)
            prefix = " ".join(shlex.quote(a) for a in args) + " "
        result = self._shell.run(
            f"git {prefix}push -u origin {shlex.quote(branch)}",
            cwd=repo_path, env=env,
        )
        if result.returncode != 0:
            hint = ""
            if token is None and "could not read" in result.stderr.lower():
                hint = " — set GH_TOKEN/GITHUB_TOKEN or pass --token"
            raise RuntimeError(
                f"Failed to push branch '{branch}': {result.stderr.strip()}{hint}"
            )
```

Update `create_pr` to route to httpx when gh isn't usable and a token is present. Add `token` param and a branch at the top of the method:
```python
    def create_pr(
        self, repo_path: Path, branch: str, title: str, body: str,
        base: str | None = None, token: str | None = None,
    ) -> str:
        if not self.gh_usable():
            if not token:
                raise RuntimeError(
                    "gh CLI not available and no GH_TOKEN/GITHUB_TOKEN found. "
                    "Install gh, or set a token (repo scope) / pass --token."
                )
            remote = self._shell.run("git remote get-url origin", cwd=repo_path)
            slug = _parse_github_slug(remote.stdout) if remote.returncode == 0 else None
            if slug is None:
                raise RuntimeError(
                    "Could not determine owner/repo from origin remote for REST PR creation."
                )
            owner, repo = slug
            effective_base = base or get_default_branch_via_httpx(token, owner, repo)
            return create_pr_via_httpx(
                token, owner, repo, head=branch, base=effective_base,
                title=title, body=body,
            )
        # --- existing gh pr create path (unchanged) ---
        safe_title = shlex.quote(title)
        # ... rest of the current method body stays exactly as-is ...
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/core/test_pr.py -v`
Expected: PASS.

- [ ] **Step 5: Thread the token through the finish CLI** — in `src/mship/cli/worktree.py`:

Add the option to the `finish` command (near the other finish options, ~`:802`):
```python
        token: Optional[str] = typer.Option(
            None, "--token", help="GitHub token for push + PR creation in "
            "credential-less environments (else GH_TOKEN / GITHUB_TOKEN).",
        ),
```

Resolve it once early in `finish` (after `pr_mgr = container.pr_manager()` is obtained):
```python
        from mship.core.gh_auth import resolve_token
        gh_token = resolve_token(token)
```

Make the gh guard token-aware — replace the unconditional `pr_mgr.check_gh_available()` (~`:1082`) with:
```python
        if gh_token is None:
            pr_mgr.check_gh_available()
        # else: a token is present; the httpx PR path covers gh absence.
```

Pass `token=gh_token` at every `push_branch` call (~`:1050`, `:1275`, `:1295`):
```python
                    pr_mgr.push_branch(repo_path, task.branch, token=gh_token)
```
and at the `create_pr` call (~`:1354`):
```python
                    pr_url = pr_mgr.create_pr(
                        ...,                       # existing args unchanged
                        token=gh_token,
                    )
```
(Find each exact call and add the `token=gh_token` kwarg; don't change the other args.)

- [ ] **Step 6: Verify + full suite**

Run: `uv run mship finish --help` → shows `--token`.
Run: `mship test` (full suite). Expected: green (modulo the known MOS-188 serve test if run from a bare checkout).

- [ ] **Step 7: Commit**

```bash
git add src/mship/core/pr.py src/mship/cli/worktree.py tests/core/test_pr.py
git commit -m "feat(finish): token-authed push + gh-or-httpx PR creation (MOS-187)"
mship journal "finish: push uses token cred args; create_pr falls back to httpx when gh absent; --token added + threaded; tests passing" --action committed
```

---

## Final verification

- [ ] `mship test` — full suite green (only the pre-existing MOS-188 serve test may fail from a bare checkout).
- [ ] `uv run mship bootstrap --help` and `uv run mship finish --help` both show `--token`.
- [ ] Manual/cloud note: true end-to-end (private clone + tokenless→token, gh-absent PR) requires a cloud session — call it out in the PR as the one path not covered by local unit tests.

Then `mship phase review` → `mship finish --require-tests --body-file <body>` (PR body: gh_auth module, bootstrap/finish token wiring, gh-or-httpx PR path, security notes; `Refs MOS-187`).

---

## Self-Review (by plan author)

**Spec coverage:** ac1 resolve_token → Task 1. ac2 git_cred_args token-in-env → Task 1. ac3 owner/repo parse → reuse `_parse_github_slug` (Task 4 uses it; already tested in the repo — add a case if coverage is thin). ac4 create_pr_via_httpx POST+html_url → Task 2. ac5 finish gh-vs-REST selection → Task 4 (`test_create_pr_uses_httpx_when_gh_absent` / `_uses_gh_when_available`). ac6 bootstrap splices cred args iff token → Task 3. ac7 no-token auth-failure actionable error → Task 3 (`_looks_like_auth_failure` hint) + Task 4 (create_pr raise, push hint). ac8 `--token` on both commands → Task 3 Step 5, Task 4 Step 5. ac9 token never in argv/disk → Task 1 `test_git_cred_args_token_only_in_env_not_args` + Task 3/4 command-capture asserts. ✓ all covered.

**Placeholder scan:** none — every code step has complete code and an exact command + expected result. The one "unchanged from current code" reference (the clone's branch-checkout block, and the gh `create_pr` body) is intentional: those blocks already exist verbatim in the files and must be preserved, not rewritten.

**Type consistency:** `resolve_token(explicit) -> str | None`, `git_cred_args(token) -> (list[str], dict[str,str])`, `create_pr_via_httpx(token, owner, repo, *, head, base, title, body, client=None) -> str`, `get_default_branch_via_httpx(token, owner, repo, *, client=None) -> str`, `gh_usable() -> bool`, `push_branch(..., token=None)`, `create_pr(..., token=None)` — used identically across tasks.
