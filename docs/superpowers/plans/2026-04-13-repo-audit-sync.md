# Repo Audit & Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `mship audit` and `mship sync` — git-state drift detection and safe fast-forward reconciliation — plus opt-out gates on `spawn` and `finish`.

**Architecture:** `core/repo_state.py` produces an immutable `AuditReport` from raw git subprocess calls, grouping subdir repos by their shared git root. `core/repo_sync.py` consumes the report and runs at most `fetch --prune` + `pull --ff-only` per repo. `core/audit_gate.py` shares one call path between `spawn` and `finish`. CLI is two thin Typer commands that format the report for TTY/JSON.

**Tech Stack:** Python 3.12+, Typer, Pydantic, `ShellRunner` wrapping subprocess, `git` CLI.

**Spec:** `docs/superpowers/specs/2026-04-13-repo-audit-sync-design.md`

---

## File Structure

**Create:**
- `src/mship/core/repo_state.py` — `Issue`, `RepoAudit`, `AuditReport`, `audit_repos`.
- `src/mship/core/repo_sync.py` — `SyncResult`, `SyncReport`, `sync_repos`.
- `src/mship/core/audit_gate.py` — `run_audit_gate` used by spawn/finish.
- `src/mship/cli/audit.py` — `mship audit` command.
- `src/mship/cli/sync.py` — `mship sync` command.
- `tests/core/test_repo_state.py`, `tests/core/test_repo_sync.py`, `tests/core/test_audit_gate.py`.
- `tests/cli/test_audit.py`, `tests/cli/test_sync.py`.
- `tests/conftest.py` additions: `audit_workspace` fixture with a bare origin + clone per repo, exercising real git state.

**Modify:**
- `src/mship/core/config.py` — add per-repo fields; add `AuditPolicy`; add `WorkspaceConfig.audit`; add conflict validator.
- `src/mship/cli/__init__.py` — register the two new commands.
- `src/mship/cli/worktree.py` — call `run_audit_gate` in `spawn` and `finish`; add `--force-audit`.
- `tests/core/test_config.py`, `tests/test_finish_integration.py`, `tests/test_integration.py` — extend for the new fields + gate behavior.

---

## Task 1: Config additions — per-repo fields + `AuditPolicy` + validator

**Files:**
- Modify: `src/mship/core/config.py`
- Test: `tests/core/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_config.py`:
```python
def _write_repo(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")


def test_repo_config_accepts_drift_fields(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "cli")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {
            "cli": {
                "path": "./cli", "type": "service",
                "expected_branch": "main",
                "allow_dirty": True,
                "allow_extra_worktrees": True,
            },
        },
    }))
    cfg = ConfigLoader.load(cfg_path)
    r = cfg.repos["cli"]
    assert r.expected_branch == "main"
    assert r.allow_dirty is True
    assert r.allow_extra_worktrees is True


def test_repo_config_drift_defaults(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "cli")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {"cli": {"path": "./cli", "type": "service"}},
    }))
    cfg = ConfigLoader.load(cfg_path)
    r = cfg.repos["cli"]
    assert r.expected_branch is None
    assert r.allow_dirty is False
    assert r.allow_extra_worktrees is False


def test_audit_policy_defaults_to_blocking(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "cli")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {"cli": {"path": "./cli", "type": "service"}},
    }))
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.audit.block_spawn is True
    assert cfg.audit.block_finish is True


def test_audit_policy_opt_out(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "cli")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "audit": {"block_spawn": False, "block_finish": False},
        "repos": {"cli": {"path": "./cli", "type": "service"}},
    }))
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.audit.block_spawn is False
    assert cfg.audit.block_finish is False


def test_expected_branch_conflict_rejected(tmp_path):
    import yaml
    import pytest
    from mship.core.config import ConfigLoader

    _write_repo(tmp_path, "mono")
    _write_repo(tmp_path, "mono/pkg-a")
    _write_repo(tmp_path, "mono/pkg-b")
    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {
            "mono": {"path": "./mono", "type": "service"},
            "pkg_a": {"path": "pkg-a", "type": "library", "git_root": "mono",
                       "expected_branch": "main"},
            "pkg_b": {"path": "pkg-b", "type": "library", "git_root": "mono",
                       "expected_branch": "develop"},
        },
    }))
    with pytest.raises(ValueError, match="expected_branch"):
        ConfigLoader.load(cfg_path)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_config.py -v -k "drift or audit_policy or expected_branch_conflict"`
Expected: FAIL (fields not defined; no validator).

- [ ] **Step 3: Implement**

In `src/mship/core/config.py`:

Add the three fields to `RepoConfig` (alongside `base_branch`):
```python
    expected_branch: str | None = None
    allow_dirty: bool = False
    allow_extra_worktrees: bool = False
```

Add a new class before `WorkspaceConfig`:
```python
class AuditPolicy(BaseModel):
    block_spawn: bool = True
    block_finish: bool = True
```

Add a field on `WorkspaceConfig`:
```python
    audit: AuditPolicy = AuditPolicy()
```

Add a validator to `WorkspaceConfig` (next to the existing `validate_depends_on_refs` / `validate_no_cycles`):
```python
    @model_validator(mode="after")
    def validate_expected_branch_consistency(self) -> "WorkspaceConfig":
        groups: dict[str, list[tuple[str, str]]] = {}
        for name, repo in self.repos.items():
            if repo.git_root is None or repo.expected_branch is None:
                continue
            groups.setdefault(repo.git_root, []).append((name, repo.expected_branch))
        for root, members in groups.items():
            branches = {b for _, b in members}
            if len(branches) > 1:
                raise ValueError(
                    f"Repos sharing git_root={root!r} declare conflicting expected_branch values: "
                    + ", ".join(f"{n}={b!r}" for n, b in members)
                )
        return self
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: PASS (all existing + 5 new).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/config.py tests/core/test_config.py
git commit -m "feat: add per-repo drift fields and AuditPolicy to config"
```

---

## Task 2: Data model — `Issue`, `RepoAudit`, `AuditReport`

**Files:**
- Create: `src/mship/core/repo_state.py` (just the data classes in this task)
- Test: `tests/core/test_repo_state.py`

- [ ] **Step 1: Write the failing test**

`tests/core/test_repo_state.py`:
```python
from pathlib import Path

from mship.core.repo_state import Issue, RepoAudit, AuditReport


def test_issue_is_immutable():
    i = Issue(code="dirty_worktree", severity="error", message="x")
    try:
        i.code = "other"
        raised = False
    except Exception:
        raised = True
    assert raised


def test_repo_audit_has_errors_true_for_error_issue():
    a = RepoAudit(
        name="cli",
        path=Path("/abs"),
        current_branch="main",
        issues=(Issue(code="dirty_worktree", severity="error", message="x"),),
    )
    assert a.has_errors is True


def test_repo_audit_has_errors_false_for_info_only():
    a = RepoAudit(
        name="cli",
        path=Path("/abs"),
        current_branch="main",
        issues=(Issue(code="ahead_remote", severity="info", message="x"),),
    )
    assert a.has_errors is False


def test_audit_report_has_errors_aggregates():
    clean = RepoAudit(name="a", path=Path("/"), current_branch="main", issues=())
    bad = RepoAudit(
        name="b", path=Path("/"), current_branch="main",
        issues=(Issue(code="dirty_worktree", severity="error", message="x"),),
    )
    assert AuditReport(repos=(clean,)).has_errors is False
    assert AuditReport(repos=(clean, bad)).has_errors is True


def test_audit_report_to_json_shape():
    r = AuditReport(repos=(
        RepoAudit(
            name="cli", path=Path("/abs/cli"), current_branch="main",
            issues=(Issue(code="dirty_worktree", severity="error", message="3 files"),),
        ),
    ))
    payload = r.to_json(workspace="ws")
    assert payload["workspace"] == "ws"
    assert payload["has_errors"] is True
    assert payload["repos"] == [{
        "name": "cli",
        "path": "/abs/cli",
        "current_branch": "main",
        "issues": [{"code": "dirty_worktree", "severity": "error", "message": "3 files"}],
    }]
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_repo_state.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the data model**

`src/mship/core/repo_state.py`:
```python
"""Repo drift detection — data model and audit entry point."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["error", "info"]


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str


@dataclass(frozen=True)
class RepoAudit:
    name: str
    path: Path
    current_branch: str | None
    issues: tuple[Issue, ...]

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


@dataclass(frozen=True)
class AuditReport:
    repos: tuple[RepoAudit, ...]

    @property
    def has_errors(self) -> bool:
        return any(r.has_errors for r in self.repos)

    def to_json(self, workspace: str) -> dict:
        return {
            "workspace": workspace,
            "has_errors": self.has_errors,
            "repos": [
                {
                    "name": r.name,
                    "path": str(r.path),
                    "current_branch": r.current_branch,
                    "issues": [
                        {"code": i.code, "severity": i.severity, "message": i.message}
                        for i in r.issues
                    ],
                }
                for r in self.repos
            ],
        }
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_repo_state.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/repo_state.py tests/core/test_repo_state.py
git commit -m "feat: add AuditReport data model for repo drift"
```

---

## Task 3: `audit_repos` — git probes + monorepo grouping

**Files:**
- Modify: `src/mship/core/repo_state.py` (add `audit_repos` and private probes)
- Modify: `tests/conftest.py` (add `audit_workspace` fixture)
- Test: `tests/core/test_repo_state.py` (append issue-detection tests + grouping test)

This is the biggest task. It adds real git-probe logic plus a fixture that builds a bare origin + clone per repo in tmp_path so issue detection is exercised against real git behavior.

- [ ] **Step 1: Add the shared fixture**

Append to `tests/conftest.py`:
```python
@pytest.fixture
def audit_workspace(tmp_path: Path) -> Path:
    """Workspace with a bare 'origin' and working clone for each of two repos.

    Layout:
        tmp_path/
            origin/{cli,api}.git   # bare
            cli/, api/              # working clones + Taskfile.yml
            mothership.yaml
    """
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def _sh(*args, cwd):
        subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=env)

    (tmp_path / "origin").mkdir()
    for name in ("cli", "api"):
        bare = tmp_path / "origin" / f"{name}.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)

        clone = tmp_path / name
        subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
        _sh("git", "config", "user.email", "t@t", cwd=clone)
        _sh("git", "config", "user.name", "t", cwd=clone)
        (clone / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
        (clone / "README.md").write_text(f"{name}\n")
        _sh("git", "add", ".", cwd=clone)
        _sh("git", "commit", "-qm", "init", cwd=clone)
        _sh("git", "push", "-q", "origin", "main", cwd=clone)

    (tmp_path / "mothership.yaml").write_text(
        "workspace: audit-test\n"
        "repos:\n"
        "  cli:\n    path: ./cli\n    type: service\n"
        "  api:\n    path: ./api\n    type: service\n"
    )
    return tmp_path
```

- [ ] **Step 2: Write the failing detection tests**

Append to `tests/core/test_repo_state.py`:
```python
import os
import subprocess
from pathlib import Path

import pytest

from mship.core.config import ConfigLoader
from mship.core.repo_state import audit_repos
from mship.util.shell import ShellRunner


def _load(ws: Path):
    return ConfigLoader.load(ws / "mothership.yaml"), ShellRunner()


def _sh(*args, cwd):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=env)


def _issue_codes(report, name):
    (repo,) = [r for r in report.repos if r.name == name]
    return {i.code for i in repo.issues}


def test_audit_clean_repos_have_no_issues(audit_workspace):
    cfg, shell = _load(audit_workspace)
    rep = audit_repos(cfg, shell)
    assert rep.has_errors is False
    for r in rep.repos:
        assert r.issues == ()


def test_audit_path_missing(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli").rename(audit_workspace / "cli.moved")
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "path_missing" in _issue_codes(rep, "cli")


def test_audit_not_a_git_repo(audit_workspace):
    cfg, shell = _load(audit_workspace)
    import shutil
    shutil.rmtree(audit_workspace / "cli" / ".git")
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "not_a_git_repo" in _issue_codes(rep, "cli")


def test_audit_detached_head(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    _sh("git", "checkout", "--detach", "HEAD", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "detached_head" in _issue_codes(rep, "cli")


def test_audit_unexpected_branch(audit_workspace):
    import yaml
    cfg_path = audit_workspace / "mothership.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["repos"]["cli"]["expected_branch"] = "marshal-refactor"
    cfg_path.write_text(yaml.safe_dump(data))
    cfg, shell = _load(audit_workspace)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "unexpected_branch" in _issue_codes(rep, "cli")


def test_audit_dirty_worktree(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "new.txt").write_text("hi\n")
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "dirty_worktree" in _issue_codes(rep, "cli")


def test_audit_allow_dirty_suppresses(audit_workspace):
    import yaml
    cfg_path = audit_workspace / "mothership.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["repos"]["cli"]["allow_dirty"] = True
    cfg_path.write_text(yaml.safe_dump(data))
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "new.txt").write_text("hi\n")
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "dirty_worktree" not in _issue_codes(rep, "cli")


def test_audit_ahead_remote_is_info(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    (clone / "x.txt").write_text("x")
    _sh("git", "add", "x.txt", cwd=clone)
    _sh("git", "commit", "-qm", "ahead", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    (repo,) = [r for r in rep.repos if r.name == "cli"]
    codes = {(i.code, i.severity) for i in repo.issues}
    assert ("ahead_remote", "info") in codes
    assert repo.has_errors is False


def test_audit_behind_remote(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    # Push an update via the api clone? Simpler: commit in cli, push, reset local.
    (clone / "y.txt").write_text("y")
    _sh("git", "add", "y.txt", cwd=clone)
    _sh("git", "commit", "-qm", "later", cwd=clone)
    _sh("git", "push", "-q", "origin", "main", cwd=clone)
    _sh("git", "reset", "--hard", "HEAD^", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "behind_remote" in _issue_codes(rep, "cli")


def test_audit_diverged(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    (clone / "a.txt").write_text("a")
    _sh("git", "add", "a.txt", cwd=clone)
    _sh("git", "commit", "-qm", "a", cwd=clone)
    _sh("git", "push", "-q", "origin", "main", cwd=clone)
    _sh("git", "reset", "--hard", "HEAD^", cwd=clone)
    (clone / "b.txt").write_text("b")
    _sh("git", "add", "b.txt", cwd=clone)
    _sh("git", "commit", "-qm", "b", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "diverged" in _issue_codes(rep, "cli")


def test_audit_no_upstream(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    _sh("git", "checkout", "-qb", "scratch", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "no_upstream" in _issue_codes(rep, "cli")


def test_audit_extra_worktrees(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    wt = audit_workspace / "cli-wt"
    _sh("git", "worktree", "add", str(wt), "-b", "scratch", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "extra_worktrees" in _issue_codes(rep, "cli")


def test_audit_fetch_failed(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    _sh("git", "remote", "set-url", "origin", "/no/such/path.git", cwd=clone)
    rep = audit_repos(cfg, shell, names=["cli"])
    assert "fetch_failed" in _issue_codes(rep, "cli")


def test_audit_names_filter_unknown_repo(audit_workspace):
    cfg, shell = _load(audit_workspace)
    with pytest.raises(ValueError, match="unknown"):
        audit_repos(cfg, shell, names=["cli", "nope"])


def test_audit_monorepo_one_fetch_per_root(tmp_path):
    """Two repos sharing a git_root should trigger one fetch and share git-wide issues."""
    import yaml
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    mono = tmp_path / "mono"
    subprocess.run(["git", "clone", str(bare), str(mono)], check=True, capture_output=True)
    for k in ("user.email", "t@t"), ("user.name", "t"):
        subprocess.run(["git", "config", *k], cwd=mono, check=True, capture_output=True)
    (mono / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (mono / "pkg-a").mkdir()
    (mono / "pkg-b").mkdir()
    (mono / "pkg-a" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (mono / "pkg-b" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "add", "."], cwd=mono, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=mono, check=True, capture_output=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=mono, check=True, capture_output=True)

    (tmp_path / "mothership.yaml").write_text(yaml.safe_dump({
        "workspace": "m",
        "repos": {
            "mono": {"path": "./mono", "type": "service"},
            "pkg_a": {"path": "pkg-a", "type": "library", "git_root": "mono"},
            "pkg_b": {"path": "pkg-b", "type": "library", "git_root": "mono"},
        },
    }))

    # Dirty pkg-a only — branch/fetch state is clean
    (mono / "pkg-a" / "dirty.txt").write_text("x")

    cfg, shell = _load(tmp_path)
    # Wrap shell to count fetch calls
    calls: list[str] = []
    real_run = shell.run
    def counting(cmd, cwd, env=None):
        calls.append(cmd)
        return real_run(cmd, cwd, env=env)
    shell.run = counting  # type: ignore[assignment]

    rep = audit_repos(cfg, shell)
    fetch_calls = [c for c in calls if c.startswith("git fetch")]
    assert len(fetch_calls) == 1, f"expected one fetch, got {fetch_calls}"

    pkg_a_codes = _issue_codes(rep, "pkg_a")
    pkg_b_codes = _issue_codes(rep, "pkg_b")
    assert "dirty_worktree" in pkg_a_codes
    assert "dirty_worktree" not in pkg_b_codes
```

- [ ] **Step 3: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_repo_state.py -v`
Expected: FAIL — `audit_repos` not defined.

- [ ] **Step 4: Implement `audit_repos` and the probes**

Append to `src/mship/core/repo_state.py`:
```python
from typing import Iterable

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.util.shell import ShellRunner


def _effective_path(cfg: WorkspaceConfig, name: str) -> Path:
    repo = cfg.repos[name]
    if repo.git_root is not None:
        parent = cfg.repos[repo.git_root]
        return (parent.path / repo.path).resolve()
    return repo.path


def _git_root_path(cfg: WorkspaceConfig, name: str) -> Path:
    repo = cfg.repos[name]
    if repo.git_root is not None:
        return cfg.repos[repo.git_root].path
    return repo.path


def _git_root_key(cfg: WorkspaceConfig, name: str) -> str:
    """The name of the repo that owns the git root this repo belongs to."""
    repo = cfg.repos[name]
    return repo.git_root if repo.git_root is not None else name


def _sh_out(shell: ShellRunner, cmd: str, cwd: Path) -> tuple[int, str, str]:
    r = shell.run(cmd, cwd=cwd)
    return r.returncode, r.stdout, r.stderr


def _probe_git_wide(shell: ShellRunner, root_path: Path, expected_branch: str | None,
                     allow_extra_worktrees: bool) -> tuple[str | None, list[Issue]]:
    """Run checks that operate on the git root. Returns (current_branch, issues)."""
    issues: list[Issue] = []

    # Branch / detached
    rc, out, _ = _sh_out(shell, "git symbolic-ref --short HEAD", root_path)
    if rc != 0:
        issues.append(Issue("detached_head", "error", "HEAD is detached"))
        current_branch = None
    else:
        current_branch = out.strip()
        if expected_branch is not None and current_branch != expected_branch:
            issues.append(Issue(
                "unexpected_branch", "error",
                f"on {current_branch!r}, expected {expected_branch!r}",
            ))

    # Fetch (needed for ahead/behind)
    rc, _, err = _sh_out(shell, "git fetch --prune origin", root_path)
    fetch_ok = rc == 0
    if not fetch_ok:
        issues.append(Issue("fetch_failed", "error", err.strip().splitlines()[-1] if err.strip() else "fetch failed"))

    # Upstream tracking
    if current_branch is not None and fetch_ok:
        rc, _, _ = _sh_out(shell, "git rev-parse --abbrev-ref --symbolic-full-name @{u}", root_path)
        if rc != 0:
            issues.append(Issue("no_upstream", "error", "current branch has no tracking remote"))
        else:
            rc_ah, out_ah, _ = _sh_out(shell, "git rev-list --count @{u}..HEAD", root_path)
            rc_be, out_be, _ = _sh_out(shell, "git rev-list --count HEAD..@{u}", root_path)
            if rc_ah == 0 and rc_be == 0:
                ahead = int(out_ah.strip() or "0")
                behind = int(out_be.strip() or "0")
                if ahead and behind:
                    issues.append(Issue("diverged", "error",
                                        f"{ahead} ahead, {behind} behind origin"))
                elif behind:
                    issues.append(Issue("behind_remote", "error",
                                        f"behind origin by {behind} commits"))
                elif ahead:
                    issues.append(Issue("ahead_remote", "info",
                                        f"ahead of origin by {ahead} commits"))

    # Extra worktrees
    if not allow_extra_worktrees:
        rc, out, _ = _sh_out(shell, "git worktree list --porcelain", root_path)
        if rc == 0:
            count = sum(1 for line in out.splitlines() if line.startswith("worktree "))
            if count > 1:
                issues.append(Issue("extra_worktrees", "error",
                                    f"{count} worktrees exist; expected 1"))

    return current_branch, issues


def _probe_dirty(shell: ShellRunner, root_path: Path, subdir: Path | None,
                  allow_dirty: bool) -> Issue | None:
    if allow_dirty:
        return None
    cmd = "git status --porcelain"
    if subdir is not None:
        import shlex
        cmd += f" -- {shlex.quote(str(subdir))}"
    rc, out, _ = _sh_out(shell, cmd, root_path)
    if rc != 0:
        return None
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if lines:
        return Issue("dirty_worktree", "error",
                     f"{len(lines)} uncommitted change" + ("s" if len(lines) != 1 else ""))
    return None


def audit_repos(
    config: WorkspaceConfig,
    shell: ShellRunner,
    names: Iterable[str] | None = None,
) -> AuditReport:
    """Run drift audit across repos, grouping by git root for git-wide checks."""
    target_names = list(names) if names is not None else list(config.repos.keys())
    unknown = [n for n in target_names if n not in config.repos]
    if unknown:
        raise ValueError(f"unknown repo(s): {', '.join(sorted(unknown))}")

    # Group by effective git root
    groups: dict[str, list[str]] = {}
    for name in target_names:
        groups.setdefault(_git_root_key(config, name), []).append(name)

    per_repo_issues: dict[str, list[Issue]] = {n: [] for n in target_names}
    per_repo_branch: dict[str, str | None] = {n: None for n in target_names}

    for root_key, members in groups.items():
        root_path = _git_root_path(config, root_key)

        if not root_path.exists():
            for m in members:
                per_repo_issues[m].append(Issue(
                    "path_missing", "error", f"path not found: {root_path}"))
            continue
        if not (root_path / ".git").exists():
            for m in members:
                per_repo_issues[m].append(Issue(
                    "not_a_git_repo", "error", f"no .git at {root_path}"))
            continue

        # Pick expected_branch / allow_extra_worktrees from the root's own RepoConfig,
        # falling back to any member's declaration (validator guarantees consistency).
        root_cfg = config.repos.get(root_key)
        expected = (root_cfg.expected_branch if root_cfg is not None else None)
        if expected is None:
            for m in members:
                if config.repos[m].expected_branch is not None:
                    expected = config.repos[m].expected_branch
                    break
        allow_wt = any(config.repos[m].allow_extra_worktrees for m in members) or (
            root_cfg.allow_extra_worktrees if root_cfg is not None else False
        )

        current_branch, wide_issues = _probe_git_wide(shell, root_path, expected, allow_wt)
        for m in members:
            per_repo_branch[m] = current_branch
            per_repo_issues[m].extend(wide_issues)

        # Per-repo dirty check, scoped to subdir when applicable.
        for m in members:
            m_cfg = config.repos[m]
            subdir: Path | None = None
            if m_cfg.git_root is not None:
                subdir = m_cfg.path  # relative path within the parent
            di = _probe_dirty(shell, root_path, subdir, m_cfg.allow_dirty)
            if di is not None:
                per_repo_issues[m].append(di)

    repos = tuple(
        RepoAudit(
            name=n,
            path=_effective_path(config, n),
            current_branch=per_repo_branch[n],
            issues=tuple(per_repo_issues[n]),
        )
        for n in target_names
    )
    return AuditReport(repos=repos)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/core/test_repo_state.py -v`
Expected: PASS (all tests from Task 2 + the new detection and monorepo tests).

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/repo_state.py tests/core/test_repo_state.py tests/conftest.py
git commit -m "feat: audit_repos with 11-code taxonomy and monorepo grouping"
```

---

## Task 4: `mship audit` CLI

**Files:**
- Modify: `src/mship/cli/audit.py` (create)
- Modify: `src/mship/cli/__init__.py` (register)
- Test: `tests/cli/test_audit.py`

- [ ] **Step 1: Write the failing test**

`tests/cli/test_audit.py`:
```python
import json
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def _override(audit_workspace):
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()


def test_audit_clean_exits_zero(audit_workspace):
    _override(audit_workspace)
    try:
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 0, result.output
        assert "clean" in result.output
    finally:
        _reset()


def test_audit_dirty_exits_one(audit_workspace):
    _override(audit_workspace)
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = runner.invoke(app, ["audit"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        _reset()


def test_audit_json_shape(audit_workspace):
    _override(audit_workspace)
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = runner.invoke(app, ["audit", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["has_errors"] is True
        assert payload["workspace"] == "audit-test"
        cli_entry = next(r for r in payload["repos"] if r["name"] == "cli")
        codes = {i["code"] for i in cli_entry["issues"]}
        assert "dirty_worktree" in codes
    finally:
        _reset()


def test_audit_repos_filter_unknown(audit_workspace):
    _override(audit_workspace)
    try:
        result = runner.invoke(app, ["audit", "--repos", "cli,nope"])
        assert result.exit_code == 1
        assert "nope" in result.output
    finally:
        _reset()
```

Create empty `tests/cli/test_audit.py` parent `__init__.py` if needed (should already exist from earlier work).

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/cli/test_audit.py -v`
Expected: FAIL — `audit` command does not exist.

- [ ] **Step 3: Implement**

`src/mship/cli/audit.py`:
```python
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def audit(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names"),
        json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    ):
        """Report git-state drift across workspace repos."""
        from mship.core.repo_state import audit_repos

        container = get_container()
        output = Output()
        config = container.config()
        shell = container.shell()

        names: list[str] | None = None
        if repos:
            names = [n.strip() for n in repos.split(",") if n.strip()]

        try:
            report = audit_repos(config, shell, names=names)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if json_output:
            import json as _json
            print(_json.dumps(report.to_json(workspace=config.workspace), indent=2))
            raise typer.Exit(code=1 if report.has_errors else 0)

        output.print(f"[bold]workspace:[/bold] {config.workspace}")
        output.print("")
        err_count = 0
        info_count = 0
        for r in report.repos:
            branch_suffix = f" ({r.current_branch})" if r.current_branch else ""
            output.print(f"[bold]{r.name}[/bold]{branch_suffix}:")
            if not r.issues:
                output.print("  [green]✓[/green] clean")
            else:
                for i in r.issues:
                    if i.severity == "error":
                        err_count += 1
                        output.print(f"  [red]✗[/red] {i.code}: {i.message}")
                    else:
                        info_count += 1
                        output.print(f"  [blue]ⓘ[/blue] {i.code}: {i.message}")
            output.print("")
        output.print(f"{err_count} error(s), {info_count} info across {len(report.repos)} repos")
        raise typer.Exit(code=1 if report.has_errors else 0)
```

In `src/mship/cli/__init__.py`, alongside the other imports:
```python
from mship.cli import audit as _audit_mod
```

And alongside the register calls:
```python
_audit_mod.register(app, get_container)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/cli/test_audit.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/audit.py src/mship/cli/__init__.py tests/cli/test_audit.py
git commit -m "feat: mship audit command with TTY and JSON output"
```

---

## Task 5: `mship sync` + `sync_repos`

**Files:**
- Create: `src/mship/core/repo_sync.py`
- Create: `src/mship/cli/sync.py`
- Modify: `src/mship/cli/__init__.py` (register)
- Tests: `tests/core/test_repo_sync.py`, `tests/cli/test_sync.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_repo_sync.py`:
```python
import os
import subprocess
from pathlib import Path

from mship.core.config import ConfigLoader
from mship.core.repo_state import audit_repos
from mship.core.repo_sync import sync_repos, SyncResult
from mship.util.shell import ShellRunner


def _sh(*args, cwd):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=env)


def _load(ws):
    return ConfigLoader.load(ws / "mothership.yaml"), ShellRunner()


def _result_for(rep, name):
    return next(r for r in rep.results if r.name == name)


def test_sync_clean_repo_up_to_date(audit_workspace):
    cfg, shell = _load(audit_workspace)
    report = audit_repos(cfg, shell)
    out = sync_repos(report, cfg, shell)
    r = _result_for(out, "cli")
    assert r.status == "up_to_date"


def test_sync_behind_repo_fast_forwards(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    (clone / "y.txt").write_text("y")
    _sh("git", "add", "y.txt", cwd=clone)
    _sh("git", "commit", "-qm", "x", cwd=clone)
    _sh("git", "push", "-q", "origin", "main", cwd=clone)
    _sh("git", "reset", "--hard", "HEAD^", cwd=clone)

    report = audit_repos(cfg, shell)
    out = sync_repos(report, cfg, shell)
    r = _result_for(out, "cli")
    assert r.status == "fast_forwarded"
    assert "1" in r.message  # 1 commit


def test_sync_dirty_skipped(audit_workspace):
    cfg, shell = _load(audit_workspace)
    (audit_workspace / "cli" / "x.txt").write_text("x")
    report = audit_repos(cfg, shell)
    out = sync_repos(report, cfg, shell)
    r = _result_for(out, "cli")
    assert r.status == "skipped"
    assert "dirty_worktree" in r.message


def test_sync_diverged_skipped(audit_workspace):
    cfg, shell = _load(audit_workspace)
    clone = audit_workspace / "cli"
    (clone / "a.txt").write_text("a"); _sh("git", "add", "a.txt", cwd=clone); _sh("git", "commit", "-qm", "a", cwd=clone)
    _sh("git", "push", "-q", "origin", "main", cwd=clone)
    _sh("git", "reset", "--hard", "HEAD^", cwd=clone)
    (clone / "b.txt").write_text("b"); _sh("git", "add", "b.txt", cwd=clone); _sh("git", "commit", "-qm", "b", cwd=clone)

    report = audit_repos(cfg, shell)
    out = sync_repos(report, cfg, shell)
    r = _result_for(out, "cli")
    assert r.status == "skipped"
    assert "diverged" in r.message
```

`tests/cli/test_sync.py`:
```python
from typer.testing import CliRunner

from mship.cli import app, container

runner = CliRunner()


def test_sync_clean_exits_zero(audit_workspace):
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")
    try:
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output or "up_to_date" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_sync_dirty_nonzero(audit_workspace):
    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_repo_sync.py tests/cli/test_sync.py -v`
Expected: FAIL — `sync_repos` and `sync` command don't exist.

- [ ] **Step 3: Implement**

`src/mship/core/repo_sync.py`:
```python
"""Safe fast-forward reconciliation for repos that audit as behind-only."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mship.core.config import WorkspaceConfig
from mship.core.repo_state import AuditReport, RepoAudit
from mship.util.shell import ShellRunner


Status = Literal["up_to_date", "fast_forwarded", "skipped"]


@dataclass(frozen=True)
class SyncResult:
    name: str
    path: Path
    status: Status
    message: str


@dataclass(frozen=True)
class SyncReport:
    results: tuple[SyncResult, ...]

    @property
    def has_errors(self) -> bool:
        return any(r.status == "skipped" for r in self.results)


_BLOCKING_CODES = {
    "path_missing", "not_a_git_repo", "fetch_failed", "detached_head",
    "unexpected_branch", "dirty_worktree", "no_upstream", "diverged",
}


def _git_root_path(cfg: WorkspaceConfig, name: str) -> Path:
    repo = cfg.repos[name]
    if repo.git_root is not None:
        return cfg.repos[repo.git_root].path
    return repo.path


def _result_for(repo: RepoAudit, cfg: WorkspaceConfig, shell: ShellRunner) -> SyncResult:
    blocking = [i for i in repo.issues if i.code in _BLOCKING_CODES]
    if blocking:
        first = blocking[0]
        return SyncResult(repo.name, repo.path, "skipped",
                          f"{first.code} — {first.message}")
    behind = [i for i in repo.issues if i.code == "behind_remote"]
    if behind:
        root = _git_root_path(cfg, repo.name)
        r = shell.run("git pull --ff-only", cwd=root)
        if r.returncode != 0:
            return SyncResult(repo.name, repo.path, "skipped",
                              f"pull failed: {r.stderr.strip() or 'unknown error'}")
        # Extract the commit count reported in audit issue message (e.g. "behind origin by 3 commits")
        msg = behind[0].message
        return SyncResult(repo.name, repo.path, "fast_forwarded", msg)
    return SyncResult(repo.name, repo.path, "up_to_date", "no action")


def sync_repos(report: AuditReport, config: WorkspaceConfig, shell: ShellRunner) -> SyncReport:
    # Avoid double fast-forwarding subdir repos that share a git root.
    seen_roots: set[str] = set()
    results: list[SyncResult] = []
    for repo_audit in report.repos:
        root_key = config.repos[repo_audit.name].git_root or repo_audit.name
        if root_key in seen_roots:
            # Mirror whatever result the root already got, minus the pull.
            prev = next(r for r in results if
                        (config.repos[r.name].git_root or r.name) == root_key)
            results.append(SyncResult(
                repo_audit.name, repo_audit.path, prev.status,
                f"(shared git root with {prev.name}) {prev.message}",
            ))
            continue
        seen_roots.add(root_key)
        results.append(_result_for(repo_audit, config, shell))
    return SyncReport(results=tuple(results))
```

`src/mship/cli/sync.py`:
```python
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def sync(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names"),
    ):
        """Fast-forward repos that audit cleanly and are behind origin."""
        from mship.core.repo_state import audit_repos
        from mship.core.repo_sync import sync_repos

        container = get_container()
        output = Output()
        config = container.config()
        shell = container.shell()

        names: list[str] | None = None
        if repos:
            names = [n.strip() for n in repos.split(",") if n.strip()]

        try:
            report = audit_repos(config, shell, names=names)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        out = sync_repos(report, config, shell)
        for r in out.results:
            if r.status == "up_to_date":
                output.print(f"  {r.name}: up to date")
            elif r.status == "fast_forwarded":
                output.print(f"  [green]{r.name}[/green]: fast-forwarded ({r.message})")
            else:
                output.print(f"  [yellow]{r.name}[/yellow]: skipped ({r.message})")

        raise typer.Exit(code=1 if out.has_errors else 0)
```

Register in `src/mship/cli/__init__.py` alongside `audit`:
```python
from mship.cli import sync as _sync_mod
...
_sync_mod.register(app, get_container)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/core/test_repo_sync.py tests/cli/test_sync.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/repo_sync.py src/mship/cli/sync.py src/mship/cli/__init__.py \
        tests/core/test_repo_sync.py tests/cli/test_sync.py
git commit -m "feat: mship sync with fast-forward-only reconcile"
```

---

## Task 6: `audit_gate` helper + wire into `spawn`

**Files:**
- Create: `src/mship/core/audit_gate.py`
- Modify: `src/mship/cli/worktree.py` (spawn command)
- Test: `tests/core/test_audit_gate.py`, extend `tests/test_integration.py`

- [ ] **Step 1: Write the failing unit tests**

`tests/core/test_audit_gate.py`:
```python
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mship.core.audit_gate import run_audit_gate, AuditGateBlocked
from mship.core.repo_state import AuditReport, RepoAudit, Issue


def _blocking_report():
    return AuditReport(repos=(
        RepoAudit(name="cli", path=Path("/"), current_branch="main",
                  issues=(Issue("dirty_worktree", "error", "x"),)),
    ))


def _clean_report():
    return AuditReport(repos=(
        RepoAudit(name="cli", path=Path("/"), current_branch="main", issues=()),
    ))


def test_gate_passes_when_no_errors():
    run_audit_gate(_clean_report(), block=True, force=False, command_name="spawn",
                   on_bypass=lambda codes: None)


def test_gate_blocks_when_errors_and_block_true():
    with pytest.raises(AuditGateBlocked) as exc:
        run_audit_gate(_blocking_report(), block=True, force=False, command_name="spawn",
                       on_bypass=lambda codes: None)
    assert "dirty_worktree" in str(exc.value)


def test_gate_force_calls_on_bypass_and_proceeds():
    seen: list[list[str]] = []
    run_audit_gate(_blocking_report(), block=True, force=True, command_name="spawn",
                   on_bypass=lambda codes: seen.append(list(codes)))
    assert seen == [["dirty_worktree"]]


def test_gate_warns_but_does_not_block_when_block_false():
    run_audit_gate(_blocking_report(), block=False, force=False, command_name="spawn",
                   on_bypass=lambda codes: None)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_audit_gate.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `audit_gate`**

`src/mship/core/audit_gate.py`:
```python
"""Shared audit-gate logic used by mship spawn and mship finish."""
from __future__ import annotations

from typing import Callable

from mship.core.repo_state import AuditReport


class AuditGateBlocked(RuntimeError):
    """Raised when the audit gate blocks the command."""


def run_audit_gate(
    report: AuditReport,
    *,
    block: bool,
    force: bool,
    command_name: str,
    on_bypass: Callable[[list[str]], None],
) -> None:
    """Apply the audit gate.

    - No errors: return silently.
    - Errors + force=True: call on_bypass(codes) and return.
    - Errors + block=True: raise AuditGateBlocked with the issue summary.
    - Errors + block=False: print nothing here; caller is expected to warn.
    """
    if not report.has_errors:
        return

    error_codes: list[str] = []
    for repo in report.repos:
        for issue in repo.issues:
            if issue.severity == "error":
                error_codes.append(f"{repo.name}:{issue.code}")

    if force:
        on_bypass(error_codes)
        return

    if block:
        raise AuditGateBlocked(
            f"{command_name} blocked by audit — "
            + ", ".join(error_codes)
        )
    # block=False, not forced: caller handles warning
```

- [ ] **Step 4: Run unit tests, verify they pass**

Run: `uv run pytest tests/core/test_audit_gate.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire the gate into `spawn`**

In `src/mship/cli/worktree.py`, find the `spawn` command (search for `def spawn(`). Add `--force-audit` option and insert the gate call before the worktree creation.

Add the new flag to the signature:
```python
    def spawn(
        description: str = typer.Argument(...),
        repos: Optional[str] = typer.Option(None, "--repos", help="..."),
        force_audit: bool = typer.Option(False, "--force-audit", help="Bypass audit gate for this spawn"),
    ):
```

Immediately after `description` is validated and repo names are resolved (i.e. after you know `affected_repos: list[str]`), insert:
```python
        from mship.core.audit_gate import run_audit_gate, AuditGateBlocked
        from mship.core.repo_state import audit_repos

        audit_names = affected_repos if affected_repos else list(config.repos.keys())
        report = audit_repos(config, shell, names=audit_names)

        log_mgr = container.log_manager()

        def _log_bypass(codes: list[str]) -> None:
            # Defer log write until the task exists — capture here, apply after spawn.
            pending_bypass.append(codes)

        pending_bypass: list[list[str]] = []
        try:
            run_audit_gate(
                report,
                block=config.audit.block_spawn,
                force=force_audit,
                command_name="spawn",
                on_bypass=_log_bypass,
            )
        except AuditGateBlocked as e:
            output.error(str(e))
            raise typer.Exit(code=1)
        if not report.has_errors:
            pass
        elif not config.audit.block_spawn and not force_audit:
            output.print(f"[yellow]warning:[/yellow] spawn proceeding despite audit errors ({', '.join(c for c in (f'{r.name}:{i.code}' for r in report.repos for i in r.issues if i.severity == 'error'))})")
```

After the task is created and `task_slug` is known, append a log entry if a bypass happened:
```python
        for codes in pending_bypass:
            log_mgr.append(task_slug, f"BYPASSED AUDIT: spawn — {', '.join(codes)}")
```

(Adjust variable names to match the actual spawn body; do not invent new variables beyond `pending_bypass`.)

- [ ] **Step 6: Extend integration test**

Append to `tests/test_integration.py` (or a new `tests/test_audit_gate_integration.py` if existing file is crowded):

```python
def test_spawn_blocks_when_affected_repo_is_dirty(audit_workspace):
    from mship.cli import app, container
    from typer.testing import CliRunner
    _runner = CliRunner()

    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = _runner.invoke(app, ["spawn", "dirty test", "--repos", "cli"])
        assert result.exit_code == 1
        assert "dirty_worktree" in result.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()


def test_spawn_force_audit_bypasses_and_logs(audit_workspace):
    from mship.cli import app, container
    from typer.testing import CliRunner
    from mship.core.log import LogManager
    _runner = CliRunner()

    container.config_path.override(audit_workspace / "mothership.yaml")
    container.state_dir.override(audit_workspace / ".mothership")
    try:
        (audit_workspace / "cli" / "x.txt").write_text("x")
        result = _runner.invoke(app, ["spawn", "force test", "--repos", "cli", "--force-audit"])
        assert result.exit_code == 0, result.output

        log_mgr = LogManager(audit_workspace / ".mothership" / "logs")
        entries = log_mgr.read("force-test")
        assert any("BYPASSED AUDIT" in e.message for e in entries)
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/core/test_audit_gate.py tests/test_integration.py -v -k "audit or force"`
Expected: PASS for the new tests; no regression elsewhere. Then `uv run pytest -q` to confirm the full suite.

- [ ] **Step 8: Commit**

```bash
git add src/mship/core/audit_gate.py src/mship/cli/worktree.py \
        tests/core/test_audit_gate.py tests/test_integration.py
git commit -m "feat: wire audit_gate into mship spawn with --force-audit"
```

---

## Task 7: Wire `audit_gate` into `finish`

**Files:**
- Modify: `src/mship/cli/worktree.py` (finish command)
- Test: `tests/test_finish_integration.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_finish_integration.py`:
```python
def test_finish_blocks_when_affected_repo_is_dirty(finish_workspace):
    """Dirty affected repo blocks finish under default block_finish=true."""
    workspace, mock_shell = finish_workspace

    # First, spawn normally (shell mock returns zero so audit sees nothing)
    # Then, make the shell mock report dirty status for shared on the second pass.
    import yaml

    result = runner.invoke(app, ["spawn", "finish gate", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0, result.output

    def mock_run(cmd, cwd, env=None):
        if "git status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout=" M foo.py\n", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref" in cmd:
            return ShellResult(returncode=0, stdout="main\n", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "worktree list" in cmd:
            return ShellResult(returncode=0, stdout="worktree /tmp/shared\n", stderr="")
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 1
    assert "dirty_worktree" in result.output


def test_finish_unrelated_dirty_repo_does_not_block(finish_workspace):
    """Drift in a repo not in task.affected_repos must not block finish."""
    workspace, mock_shell = finish_workspace

    result = runner.invoke(app, ["spawn", "unrelated test", "--repos", "shared", "--force-audit"])
    assert result.exit_code == 0

    dirty_repos = {"auth-service"}  # NOT in affected_repos

    def mock_run(cmd, cwd, env=None):
        if "git status --porcelain" in cmd and any(str(cwd).endswith(d) for d in dirty_repos):
            return ShellResult(returncode=0, stdout=" M foo.py\n", stderr="")
        if "git status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "git fetch" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "symbolic-ref" in cmd:
            return ShellResult(returncode=0, stdout="main\n", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name @{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/main\n", stderr="")
        if "rev-list --count" in cmd:
            return ShellResult(returncode=0, stdout="0\n", stderr="")
        if "worktree list" in cmd:
            return ShellResult(returncode=0, stdout="worktree /tmp/r\n", stderr="")
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/main\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_finish_integration.py -v -k "dirty or unrelated"`
Expected: FAIL — gate not wired into finish yet.

- [ ] **Step 3: Wire the gate into `finish`**

In `src/mship/cli/worktree.py`, in the `finish` command:

1. Add `force_audit` to the signature alongside existing options:
```python
    def finish(
        handoff: bool = typer.Option(False, "--handoff", help="..."),
        base: Optional[str] = typer.Option(None, "--base", help="..."),
        base_map: Optional[str] = typer.Option(None, "--base-map", help="..."),
        force_audit: bool = typer.Option(False, "--force-audit", help="Bypass audit gate"),
    ):
```

2. After `task = state.tasks[state.current_task]` and before the `handoff` branch, insert:
```python
        from mship.core.audit_gate import run_audit_gate, AuditGateBlocked
        from mship.core.repo_state import audit_repos

        shell = container.shell()
        report = audit_repos(config, shell, names=task.affected_repos)

        bypass_recorded = False
        def _log_bypass(codes: list[str]) -> None:
            nonlocal bypass_recorded
            container.log_manager().append(
                task.slug, f"BYPASSED AUDIT: finish — {', '.join(codes)}"
            )
            bypass_recorded = True

        try:
            run_audit_gate(
                report,
                block=config.audit.block_finish,
                force=force_audit,
                command_name="finish",
                on_bypass=_log_bypass,
            )
        except AuditGateBlocked as e:
            output.error(str(e))
            raise typer.Exit(code=1)
        if report.has_errors and not config.audit.block_finish and not force_audit:
            output.print("[yellow]warning:[/yellow] finish proceeding despite audit errors")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_finish_integration.py -v`
Expected: all pass. Then `uv run pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/worktree.py tests/test_finish_integration.py
git commit -m "feat: audit gate on mship finish with --force-audit"
```

---

## Task 8: README documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a section under CLI Reference**

Find the CLI Reference area of `README.md`. Insert a new top-level subsection (placement: near `mship doctor`, before `### Live views`):

```markdown
### Drift audit & sync

`mship audit` reports git-state drift across all repos; `mship sync` fast-forwards the clean-behind ones. Both integrate as opt-out gates on `mship spawn` and `mship finish`.

**Per-repo config:**

```yaml
repos:
  schemas:
    path: ../schemas
    expected_branch: marshal-refactor   # optional
    allow_dirty: false                  # default
    allow_extra_worktrees: false        # default
```

**Workspace policy (defaults shown):**

```yaml
audit:
  block_spawn: true
  block_finish: true
```

**Commands:**

- `mship audit [--repos r1,r2] [--json]` — exit 1 on any error-severity drift.
- `mship sync [--repos r1,r2]` — fast-forwards behind-only clean repos; skips the rest with a reason.
- `mship spawn --force-audit` / `mship finish --force-audit` — bypass with a line logged to the task log.

**Issue codes:** `path_missing`, `not_a_git_repo`, `fetch_failed`, `detached_head`, `unexpected_branch`, `dirty_worktree`, `no_upstream`, `behind_remote`, `diverged`, `extra_worktrees` (errors); `ahead_remote` (info-only).
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document mship audit and sync"
```

---

## Self-Review

**Spec coverage:**
- Architecture (`core/repo_state`, `core/repo_sync`, `core/audit_gate`, CLI modules): Tasks 1–7 cover every file listed in the spec's File Layout. ✓
- Data model (Issue/RepoAudit/AuditReport, `to_json`): Task 2. ✓
- Per-repo config fields + AuditPolicy + conflict validator: Task 1. ✓
- 11-code taxonomy with correct severity: Task 3 (`_probe_git_wide` + `_probe_dirty`). ✓
- Monorepo grouping (one fetch per root; dirty per subdir): Task 3 (grouping loop + `_probe_dirty` subdir scoping). ✓
- `mship audit` TTY + JSON + `--repos` + error exit: Task 4. ✓
- `mship sync` semantics (skip dirty/branch/diverged/upstream, ff-only when behind-clean, benign up-to-date): Task 5. ✓
- Gate helper with bypass callback: Task 6 (`audit_gate.py`). ✓
- Spawn scoping (`--repos` else all), `--force-audit`, log entry: Task 6. ✓
- Finish scoping (`task.affected_repos`), `--force-audit`, log entry: Task 7. ✓
- README: Task 8. ✓

**Placeholder scan:** None — every step has real code or real README text.

**Type consistency:**
- `Issue`, `RepoAudit`, `AuditReport`, `SyncResult`, `SyncReport`, `AuditGateBlocked` — defined in Tasks 2/5/6, consumed consistently in later tasks.
- `audit_repos(config, shell, names=None)` signature identical in producer (Task 3) and all consumers (Tasks 4, 5, 6, 7).
- `run_audit_gate(report, *, block, force, command_name, on_bypass)` keyword-only matches all three call sites (unit test, spawn, finish).
- Issue codes as bare strings match between `_probe_*` emitters and `_BLOCKING_CODES` in sync; identical spellings.

**Known deferrals (explicit in spec):**
- Parallel `git fetch` across roots.
- `--checkout` / `--force` reconcile in sync.
- Per-issue severity overrides.
