# Stale-Main-Index Diagnostics + Sync Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship instrumentation + mitigation for the recurring "stale main index after merge" bug. `mship sync` auto-recovers when dirty tracked files on main already match `origin/<branch>`; diagnostics snapshots capture forensics data for the future root-cause fix.

**Architecture:** One new module (`src/mship/core/diagnostics.py`) with `capture_snapshot()`. Two integration points: `mship sync`'s `_result_for` in `repo_sync.py` (hash-compare + reset recovery), plus silent post-op capture in `mship finish` and `mship close`. `mship doctor` gains a `diagnostics` row when snapshots exist. State mutation (reset) only happens AFTER every dirty file's blob hash is verified against `origin/<branch>` — wrong guesses never touch user work.

**Tech Stack:** Python 3.14, stdlib `pathlib` + `datetime`, `git hash-object` / `git rev-parse` via `ShellRunner`, pytest with `file://` bare repos for real-git integration.

**Reference spec:** `docs/superpowers/specs/2026-04-21-stale-main-index-diagnostics-and-recovery-design.md`

---

## File structure

**New files:**
- `src/mship/core/diagnostics.py` — `capture_snapshot(command, reason, state_dir, *, repos=None, extra=None) -> Path | None`.
- `tests/core/test_diagnostics.py` — unit tests for the library.
- `tests/core/test_sync_recovery.py` — unit tests for `_try_recover_stale_main` against real `file://` repos.

**Modified files:**
- `src/mship/core/repo_sync.py` — new `_try_recover_stale_main`; `_result_for` and `sync_repos` gain `state_dir` parameter.
- `src/mship/cli/sync.py` — pass `container.state_dir()` to `sync_repos`.
- `src/mship/core/doctor.py` — `DoctorChecker.__init__` accepts `state_dir`; `run()` appends a `diagnostics` row when snapshots exist.
- `src/mship/cli/doctor.py` — pass `container.state_dir()` to `DoctorChecker`.
- `src/mship/cli/worktree.py` — post-op silent capture in `finish` and `close` commands.
- `tests/core/test_doctor.py` — new tests for the diagnostics row.
- `tests/cli/test_worktree.py` — new integration tests for post-op capture in finish/close.

**Task ordering rationale:** Task 1 (diagnostics library) is fully independent — unit-testable without the rest of the stack. Task 2 (sync recovery) uses it. Task 3 (finish/close post-op) uses it. Task 4 (doctor row) is independent of 2/3 but depends on 1. Task 5 is smoke + PR. Tasks 1-4 can be implemented in any order after 1 ships; I order them by dependency + risk.

---

## Task 1: Diagnostics library

**Files:**
- Create: `src/mship/core/diagnostics.py`
- Create: `tests/core/test_diagnostics.py`

**Context:** Pure library. No mship-specific side effects. `capture_snapshot` writes a JSON blob to `<state_dir>/diagnostics/<ts>-<command>-<reason>.json` with git state for the caller-provided repos plus environment info. Write failures are caught and returned as None (best-effort).

- [ ] **Step 1.1: Write failing tests**

Create `tests/core/test_diagnostics.py`:

```python
import json
import os
import subprocess
from pathlib import Path

import pytest


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def test_capture_snapshot_writes_json_with_required_keys(tmp_path):
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    path = capture_snapshot("sync", "test-reason", state_dir)
    assert path is not None
    assert path.is_file()
    assert path.parent == state_dir / "diagnostics"
    data = json.loads(path.read_text())
    for key in ("captured_at", "command", "reason", "cwd", "mship_version",
                "python_version", "path_env"):
        assert key in data, f"missing key {key}"
    assert data["command"] == "sync"
    assert data["reason"] == "test-reason"


def test_capture_snapshot_filename_is_filesystem_safe(tmp_path):
    """ISO timestamps contain colons on some platforms; must be replaced."""
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    path = capture_snapshot("sync", "a-reason", state_dir)
    assert path is not None
    # No colons in the filename portion.
    assert ":" not in path.name
    # Filename starts with a UTC ISO-like timestamp ending in Z.
    assert path.name.endswith(".json")
    assert "sync" in path.name
    assert "a-reason" in path.name


def test_capture_snapshot_creates_directory(tmp_path):
    """diagnostics/ subdir is created on first call."""
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    assert not (state_dir / "diagnostics").exists()
    capture_snapshot("sync", "r", state_dir)
    assert (state_dir / "diagnostics").is_dir()


def test_capture_snapshot_populates_repos(tmp_path):
    from mship.core.diagnostics import capture_snapshot
    repo = tmp_path / "r"
    _init_git_repo(repo)
    state_dir = tmp_path / ".mothership"
    path = capture_snapshot("sync", "r", state_dir, repos={"r": repo})
    data = json.loads(path.read_text())
    assert "repos" in data
    assert "r" in data["repos"]
    repo_info = data["repos"]["r"]
    for key in ("git_status_porcelain", "head_sha", "head_branch"):
        assert key in repo_info
    assert repo_info["git_status_porcelain"] == ""  # clean repo
    assert len(repo_info["head_sha"]) == 40  # full SHA


def test_capture_snapshot_extra_kwarg_included(tmp_path):
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    path = capture_snapshot("sync", "r", state_dir, extra={"foo": "bar", "n": 42})
    data = json.loads(path.read_text())
    assert data["extra"] == {"foo": "bar", "n": 42}


def test_capture_snapshot_returns_none_on_write_failure(tmp_path, monkeypatch):
    """Write failure never raises; returns None."""
    from mship.core.diagnostics import capture_snapshot
    state_dir = tmp_path / ".mothership"
    # Make the target directory creation fail by making its parent read-only.
    def _raise(*args, **kwargs):
        raise OSError("simulated disk full")
    monkeypatch.setattr(Path, "mkdir", _raise)
    result = capture_snapshot("sync", "r", state_dir)
    assert result is None
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `pytest tests/core/test_diagnostics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mship.core.diagnostics'`.

- [ ] **Step 1.3: Create the module**

Write `src/mship/core/diagnostics.py`:

```python
"""Forensics snapshot library for mship's self-diagnosing commands.

Writes JSON blobs to <state_dir>/diagnostics/<ts>-<command>-<reason>.json
when commands observe anomalous state. Best-effort — write failures are
caught and returned as None so callers are never interrupted.

Filename format: <ISO-8601 UTC, colons replaced with `-`>-<command>-<reason>.json.

See `docs/superpowers/specs/2026-04-21-stale-main-index-diagnostics-and-recovery-design.md`.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _git_state(repo_path: Path) -> dict:
    """Per-repo git state captured for the snapshot. Best-effort."""
    info: dict = {
        "git_status_porcelain": None,
        "head_sha": None,
        "head_branch": None,
        "upstream_tracking": None,
        "reflog_tail": None,
        "stash_count": None,
    }
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["git_status_porcelain"] = r.stdout
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["head_sha"] = r.stdout.strip() or None
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["head_branch"] = r.stdout.strip() or None
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            info["upstream_tracking"] = r.stdout.strip()
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "reflog", "-n", "10"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["reflog_tail"] = r.stdout.splitlines()
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "stash", "list"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["stash_count"] = len([l for l in r.stdout.splitlines() if l])
    except OSError:
        pass
    return info


def _mship_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("mothership")
    except Exception:
        return None


def _safe_timestamp() -> str:
    """ISO-8601 UTC timestamp with filesystem-safe separators.

    Colons (invalid on Windows, awkward on macOS) are replaced with hyphens.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # ends with +00:00, which includes a colon — strip tz and append Z
    if ts.endswith("+00:00"):
        ts = ts[:-len("+00:00")] + "Z"
    return ts.replace(":", "-")


def capture_snapshot(
    command: str,
    reason: str,
    state_dir: Path,
    *,
    repos: dict[str, Path] | None = None,
    extra: dict | None = None,
) -> Path | None:
    """Write a JSON forensics snapshot. Returns the path, or None on failure.

    command: invoking mship command name (e.g. "sync", "finish").
    reason:  short tag identifying what triggered capture (e.g. "dirty-main-pre-recovery").
    state_dir: workspace's .mothership directory.
    repos: optional {name: path} to capture per-repo git state.
    extra: caller-supplied free-form data.
    """
    try:
        diag_dir = Path(state_dir) / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)

        payload: dict = {
            "captured_at": _safe_timestamp(),
            "command": command,
            "reason": reason,
            "cwd": str(Path.cwd()),
            "mship_version": _mship_version(),
            "python_version": sys.version,
            "path_env": os.environ.get("PATH", ""),
        }
        if repos:
            payload["repos"] = {name: _git_state(Path(p)) for name, p in repos.items()}
        if extra:
            payload["extra"] = extra

        filename = f"{_safe_timestamp()}-{command}-{reason}.json"
        target = diag_dir / filename
        target.write_text(json.dumps(payload, indent=2))
        return target
    except OSError as e:
        log.debug("capture_snapshot write failed: %s", e)
        return None
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `pytest tests/core/test_diagnostics.py -v`
Expected: 6 passed.

- [ ] **Step 1.5: Commit**

```bash
git add src/mship/core/diagnostics.py tests/core/test_diagnostics.py
git commit -m "feat(core): diagnostics snapshot library"
mship journal "capture_snapshot() writes JSON forensics blobs to .mothership/diagnostics/; best-effort, non-raising" --action committed
```

---

## Task 2: `mship sync` hash-compare recovery

**Files:**
- Modify: `src/mship/core/repo_sync.py` — new `_try_recover_stale_main`; `_result_for` and `sync_repos` gain `state_dir` parameter.
- Modify: `src/mship/cli/sync.py` — pass `container.state_dir()` to `sync_repos`.
- Create: `tests/core/test_sync_recovery.py`

**Context:** When `mship sync` sees ONLY `dirty_worktree` blocking a repo, attempt recovery by hashing each dirty tracked file and comparing against the same path on `origin/<branch>`. If every file matches, the dirty state IS the fast-forward delta and we reset those files. If any file differs, bail before touching anything.

- [ ] **Step 2.1: Write failing tests**

Create `tests/core/test_sync_recovery.py`:

```python
"""Integration tests for _try_recover_stale_main.

Uses real `file://` bare-repo origins so git commands exercise actual
index/working-tree logic.
"""
import json
import subprocess
from pathlib import Path

import pytest

from mship.core.repo_sync import _try_recover_stale_main
from mship.core.repo_state import RepoAudit, Issue
from mship.core.config import RepoConfig, WorkspaceConfig
from mship.util.shell import ShellRunner


def _run(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


def _setup_workspace(tmp_path: Path, behind: bool = True,
                     dirty_matches_upstream: bool = True,
                     extra_untracked: bool = False,
                     dirty_file_not_in_upstream: bool = False) -> Path:
    """Return the local repo path. Configures an origin ahead of local."""
    origin = tmp_path / "origin.git"
    local = tmp_path / "local"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    _run(local, "config", "user.email", "t@t")
    _run(local, "config", "user.name", "t")

    # Commit A on local, push to origin.
    (local / "a.txt").write_text("A\n")
    _run(local, "add", ".")
    _run(local, "commit", "-qm", "A")
    _run(local, "push", "-qu", "origin", "main" if _current_branch(local) == "main" else _current_branch(local))

    if behind:
        # Make a commit B on a fresh clone pushed to origin.
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
        _run(other, "config", "user.email", "t@t")
        _run(other, "config", "user.name", "t")
        if dirty_file_not_in_upstream:
            # Don't add the file upstream that's dirty locally.
            (other / "unrelated.txt").write_text("unrelated upstream\n")
            _run(other, "add", ".")
        else:
            (other / "a.txt").write_text("B\n")
            _run(other, "add", ".")
        _run(other, "commit", "-qm", "B")
        _run(other, "push", "-q")
        # Now origin is ahead of local.

    # Dirty local working tree.
    if dirty_matches_upstream and behind and not dirty_file_not_in_upstream:
        (local / "a.txt").write_text("B\n")  # matches upstream
    elif dirty_file_not_in_upstream:
        (local / "a.txt").write_text("user work on a file upstream doesn't touch\n")
    else:
        (local / "a.txt").write_text("user work\n")
    if extra_untracked:
        (local / "user-note.txt").write_text("untracked\n")
    return local


def _current_branch(local: Path) -> str:
    r = _run(local, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip()


def _make_audit(name: str, path: Path) -> RepoAudit:
    """A RepoAudit whose only issue is dirty_worktree."""
    return RepoAudit(
        name=name, path=path, branch=_current_branch(path),
        issues=[Issue("dirty_worktree", "error", "1 modified tracked file")],
    )


def _make_cfg(name: str, path: Path) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace="t",
        repos={name: RepoConfig(path=path, type="service")},
    )


def test_happy_path_dirty_matches_upstream(tmp_path):
    local = _setup_workspace(tmp_path, behind=True, dirty_matches_upstream=True)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is True
    assert "recovered" in msg.lower()
    # Working tree is now clean.
    status = _run(local, "status", "--porcelain").stdout.strip()
    assert status == ""
    # Diagnostic file exists.
    diags = list((state_dir / "diagnostics").glob("*.json"))
    assert len(diags) == 1


def test_user_work_preserved_when_hash_mismatches(tmp_path):
    local = _setup_workspace(tmp_path, behind=True, dirty_matches_upstream=False)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is False
    assert "does not match upstream" in msg.lower() or "real user work" in msg.lower()
    # Original dirty content preserved.
    assert (local / "a.txt").read_text() == "user work\n"
    # Two diagnostics: pre-recovery + real-user-work.
    diags = list((state_dir / "diagnostics").glob("*.json"))
    assert len(diags) == 2


def test_not_behind_origin_is_not_recoverable(tmp_path):
    local = _setup_workspace(tmp_path, behind=False, dirty_matches_upstream=False)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is False
    assert "not behind" in msg.lower()
    # Original content untouched.
    assert (local / "a.txt").read_text() == "user work\n"
    # One diagnostic (pre-recovery).
    diags = list((state_dir / "diagnostics").glob("*.json"))
    assert len(diags) == 1


def test_untracked_files_block_recovery(tmp_path):
    local = _setup_workspace(tmp_path, behind=True, dirty_matches_upstream=True,
                             extra_untracked=True)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is False
    assert "untracked" in msg.lower()
    # Tracked dirty file NOT reset (content preserved).
    assert (local / "a.txt").read_text() == "B\n"
    assert (local / "user-note.txt").exists()


def test_dirty_file_not_in_upstream_is_not_recoverable(tmp_path):
    local = _setup_workspace(tmp_path, behind=True,
                             dirty_matches_upstream=False,
                             dirty_file_not_in_upstream=True)
    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)

    assert recovered is False
    assert ("does not match upstream" in msg.lower()
            or "not match" in msg.lower())
    # User content preserved.
    assert "user work" in (local / "a.txt").read_text()


def test_multi_file_all_match_triggers_recovery(tmp_path):
    """Two dirty files, both match upstream → recovery succeeds."""
    origin = tmp_path / "origin.git"
    local = tmp_path / "local"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    _run(local, "config", "user.email", "t@t")
    _run(local, "config", "user.name", "t")

    (local / "a.txt").write_text("A1\n")
    (local / "b.txt").write_text("B1\n")
    _run(local, "add", ".")
    _run(local, "commit", "-qm", "initial")
    branch = _current_branch(local)
    _run(local, "push", "-qu", "origin", branch)

    # Upstream advance.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _run(other, "config", "user.email", "t@t")
    _run(other, "config", "user.name", "t")
    (other / "a.txt").write_text("A2\n")
    (other / "b.txt").write_text("B2\n")
    _run(other, "add", ".")
    _run(other, "commit", "-qm", "advance")
    _run(other, "push", "-q")

    # Dirty both files matching upstream.
    (local / "a.txt").write_text("A2\n")
    (local / "b.txt").write_text("B2\n")

    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)
    assert recovered is True
    # Working tree clean.
    assert _run(local, "status", "--porcelain").stdout.strip() == ""


def test_multi_file_one_mismatches_preserves_all(tmp_path):
    """Two dirty files; first matches, second doesn't.
    Recovery bails before touching anything — both files preserved."""
    origin = tmp_path / "origin.git"
    local = tmp_path / "local"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True)
    _run(local, "config", "user.email", "t@t")
    _run(local, "config", "user.name", "t")
    (local / "a.txt").write_text("A1\n")
    (local / "b.txt").write_text("B1\n")
    _run(local, "add", ".")
    _run(local, "commit", "-qm", "initial")
    branch = _current_branch(local)
    _run(local, "push", "-qu", "origin", branch)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _run(other, "config", "user.email", "t@t")
    _run(other, "config", "user.name", "t")
    (other / "a.txt").write_text("A2\n")
    (other / "b.txt").write_text("B2\n")
    _run(other, "add", ".")
    _run(other, "commit", "-qm", "advance")
    _run(other, "push", "-q")

    # Dirty: a.txt matches upstream, b.txt has user work.
    (local / "a.txt").write_text("A2\n")
    (local / "b.txt").write_text("user work\n")

    audit = _make_audit("r", local)
    cfg = _make_cfg("r", local)
    state_dir = tmp_path / ".mothership"

    recovered, msg = _try_recover_stale_main(audit, cfg, ShellRunner(), state_dir)
    assert recovered is False
    # Both files preserved — no reset happened.
    assert (local / "a.txt").read_text() == "A2\n"
    assert (local / "b.txt").read_text() == "user work\n"
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `pytest tests/core/test_sync_recovery.py -v`
Expected: FAIL — `ImportError: cannot import name '_try_recover_stale_main' from 'mship.core.repo_sync'`.

- [ ] **Step 2.3: Add the recovery helper**

Edit `src/mship/core/repo_sync.py`. Add these imports at the top if missing:

```python
from pathlib import Path
from mship.core.diagnostics import capture_snapshot
```

Add the helper — place it just above `def _result_for(...)`:

```python
def _repo_root_path(repo: RepoAudit, cfg: WorkspaceConfig) -> Path:
    """Resolve to the effective path (handles git_root)."""
    r = cfg.repos[repo.name]
    if r.git_root is not None:
        return Path(cfg.repos[r.git_root].path)
    return Path(r.path)


def _try_recover_stale_main(
    repo: RepoAudit,
    cfg: WorkspaceConfig,
    shell: ShellRunner,
    state_dir: Path,
) -> tuple[bool, str]:
    """Attempt to recover from dirty-main-matches-upstream state.

    Returns (recovered, message). If recovered, the working tree has been
    reset so the caller's fast-forward path can run. If not recovered, no
    state mutation occurred — user's working tree untouched.
    """
    root = _repo_root_path(repo, cfg)
    # 1. Snapshot before doing anything.
    capture_snapshot(
        "sync", "dirty-main-pre-recovery", state_dir,
        repos={repo.name: root},
    )

    branch = repo.branch or _detect_branch(root, shell)
    if not branch:
        return (False, "could not resolve branch for recovery check")

    # 2. Behind check.
    import shlex as _shlex
    r = shell.run(
        f"git rev-list --count {_shlex.quote(branch)}..origin/{_shlex.quote(branch)}",
        cwd=root,
    )
    if r.returncode != 0:
        return (False, f"behind-check failed: {r.stderr.strip()}")
    try:
        behind = int(r.stdout.strip() or "0")
    except ValueError:
        behind = 0
    if behind == 0:
        return (False, "not behind origin; not the recoverable pattern")

    # 3. Untracked check — recovery only handles the modified-tracked-files pattern.
    r = shell.run("git ls-files --others --exclude-standard", cwd=root)
    if r.returncode == 0 and r.stdout.strip():
        return (False, "untracked files present; recovery skipped to preserve data")

    # 4. Dirty tracked files enumeration.
    r = shell.run("git diff --name-only HEAD", cwd=root)
    if r.returncode != 0:
        return (False, f"diff --name-only failed: {r.stderr.strip()}")
    dirty_files = [p for p in r.stdout.splitlines() if p.strip()]
    if not dirty_files:
        return (False, "no dirty tracked files; nothing to recover")

    # 5. Per-file hash compare — PROVE redundancy BEFORE mutating state.
    mismatches: list[str] = []
    for path in dirty_files:
        wh = shell.run(f"git hash-object -- {_shlex.quote(path)}", cwd=root)
        if wh.returncode != 0:
            mismatches.append(f"{path} (hash-object failed)")
            break
        working_hash = wh.stdout.strip()

        uh = shell.run(
            f"git rev-parse origin/{_shlex.quote(branch)}:{_shlex.quote(path)}",
            cwd=root,
        )
        if uh.returncode != 0:
            mismatches.append(path)
            break
        upstream_hash = uh.stdout.strip()

        if working_hash != upstream_hash:
            mismatches.append(path)
            break

    if mismatches:
        capture_snapshot(
            "sync", "dirty-main-real-user-work", state_dir,
            repos={repo.name: root},
            extra={"mismatched_files": mismatches},
        )
        return (
            False,
            f"dirty file {mismatches[0]} does not match upstream; real user work",
        )

    # 6. All files verified redundant — safe to reset.
    for path in dirty_files:
        r = shell.run(f"git checkout -- {_shlex.quote(path)}", cwd=root)
        if r.returncode != 0:
            capture_snapshot(
                "sync", "dirty-main-reset-failed", state_dir,
                repos={repo.name: root},
                extra={"failed_path": path, "stderr": r.stderr},
            )
            return (
                False,
                f"checkout failed on {path} after hashes matched: {r.stderr.strip()}",
            )

    return (True, "recovered from stale main state")


def _detect_branch(root: Path, shell: ShellRunner) -> str | None:
    r = shell.run("git rev-parse --abbrev-ref HEAD", cwd=root)
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out if out and out != "HEAD" else None
```

Update `_result_for` to accept `state_dir` and call recovery when appropriate:

```python
def _result_for(
    repo: RepoAudit,
    cfg: WorkspaceConfig,
    shell: ShellRunner,
    state_dir: Path,
) -> SyncResult:
    blocking = [i for i in repo.issues if i.code in _BLOCKING_CODES]
    # Recovery attempt: only when dirty_worktree is the SOLE blocking code.
    if blocking and all(i.code == "dirty_worktree" for i in blocking):
        recovered, msg = _try_recover_stale_main(repo, cfg, shell, state_dir)
        if recovered:
            # Rebuild just the behind-remote signal — recovery just reset
            # files but didn't pull. Existing behind-remote arm handles the
            # fast-forward.
            root = _git_root_path(cfg, repo.name)
            r = shell.run("git pull --ff-only", cwd=root)
            if r.returncode != 0:
                return SyncResult(
                    repo.name, repo.path, "skipped",
                    f"pull failed after recovery: {r.stderr.strip() or 'unknown'}",
                )
            return SyncResult(
                repo.name, repo.path, "fast_forwarded",
                f"recovered from stale main state",
            )
        # Recovery declined → fall through to original skip.
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
        msg = behind[0].message
        return SyncResult(repo.name, repo.path, "fast_forwarded", msg)
    return SyncResult(repo.name, repo.path, "up_to_date", "no action")
```

Update `sync_repos` to accept `state_dir`:

```python
def sync_repos(
    report: AuditReport,
    config: WorkspaceConfig,
    shell: ShellRunner,
    state_dir: Path,
) -> SyncReport:
    ...
    for repo_audit in report.repos:
        ...
        results.append(_result_for(repo_audit, config, shell, state_dir))
    ...
```

Find the existing `sync_repos` body and update the per-repo call site accordingly. The rest of the function (shared-root dedup) stays identical.

- [ ] **Step 2.4: Update the CLI caller**

Edit `src/mship/cli/sync.py`. Find:

```python
out = sync_repos(report, config, shell)
```

Replace with:

```python
out = sync_repos(report, config, shell, container.state_dir())
```

- [ ] **Step 2.5: Run tests to verify they pass**

Run: `pytest tests/core/test_sync_recovery.py -v`
Expected: 7 passed.

- [ ] **Step 2.6: Run broader tests**

Run: `pytest tests/core/test_diagnostics.py tests/core/test_sync_recovery.py tests/core/ -v --ignore=tests/core/view/test_web_port.py 2>&1 | tail -10`

If any existing test in `tests/core/` fails because `sync_repos` / `_result_for` signature changed, update the call site to pass a `state_dir` (use `tmp_path / ".mothership"` in tests). These should be small fixes.

- [ ] **Step 2.7: Commit**

```bash
git add src/mship/core/repo_sync.py src/mship/cli/sync.py tests/core/test_sync_recovery.py
git commit -m "feat(sync): hash-compare recovery for stale-main-index bug"
mship journal "_try_recover_stale_main resets dirty tracked files only when every file's hash matches origin/<branch>; never stashes, never resets --hard" --action committed
```

---

## Task 3: Post-op sanity captures in `mship finish` and `mship close`

**Files:**
- Modify: `src/mship/cli/worktree.py` — add silent post-op captures in `finish` and `close`.
- Modify: `tests/cli/test_worktree.py` — add integration tests.

**Context:** When `mship finish` and `mship close` complete their work, the main checkouts of affected repos should be clean. Capture a snapshot (silent; no user-visible change) if they're dirty. No refusal, no warning — we're collecting evidence.

- [ ] **Step 3.1: Write failing tests**

Append to `tests/cli/test_worktree.py`:

```python
def test_finish_captures_diagnostic_when_main_is_dirty_post_op(configured_git_app: Path):
    """If main is dirty after finish completes, a diagnostic is captured."""
    from mship.cli import container as cli_container
    from mship.util.shell import ShellResult, ShellRunner
    from unittest.mock import MagicMock

    runner.invoke(app, ["spawn", "dirty diag", "--repos", "shared", "--skip-setup"])

    # Pre-dirty the main repo's shared/ working tree by writing an extra
    # file directly to simulate whatever causes the bug.
    shared_path = configured_git_app / "shared" / "dirty-marker.txt"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text("synthetic dirty content\n")
    # Stage so `git status --porcelain` reports it as a change.
    import subprocess as _sp
    _sp.run(["git", "add", "dirty-marker.txt"], cwd=configured_git_app / "shared", check=True)

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name" in cmd and "@{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/feat/dirty-diag", stderr="")
        if "gh pr list --head" in cmd:
            return ShellResult(returncode=0, stdout="\n", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        if "git status --porcelain" in cmd:
            # Simulate dirty state on the main repo path (our post-op check).
            if "shared" in str(cwd):
                return ShellResult(returncode=0, stdout="M  dirty-marker.txt\n", stderr="")
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "dirty-diag"])
    # Finish succeeds (diagnostic is silent).
    assert result.exit_code == 0, result.output

    # A finish-dirty-main-post-op diagnostic exists.
    diag_dir = configured_git_app / ".mothership" / "diagnostics"
    if diag_dir.is_dir():
        names = [p.name for p in diag_dir.glob("*.json")]
        assert any("finish-dirty-main-post-op" in n for n in names), names
    else:
        pytest.fail("diagnostics dir not created")

    cli_container.shell.reset_override()


def test_finish_does_not_capture_diagnostic_when_main_is_clean(configured_git_app: Path):
    """Clean happy path — no post-op diagnostic."""
    from mship.cli import container as cli_container
    from mship.util.shell import ShellResult, ShellRunner
    from unittest.mock import MagicMock

    runner.invoke(app, ["spawn", "clean diag", "--repos", "shared", "--skip-setup"])

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "rev-parse --abbrev-ref --symbolic-full-name" in cmd and "@{u}" in cmd:
            return ShellResult(returncode=0, stdout="origin/feat/clean-diag", stderr="")
        if "gh pr list --head" in cmd:
            return ShellResult(returncode=0, stdout="\n", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        if "git status --porcelain" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)

    result = runner.invoke(app, ["finish", "--task", "clean-diag"])
    assert result.exit_code == 0, result.output

    diag_dir = configured_git_app / ".mothership" / "diagnostics"
    if diag_dir.is_dir():
        names = [p.name for p in diag_dir.glob("*.json")]
        # No post-op diagnostics present for this run.
        assert not any("finish-dirty-main-post-op" in n for n in names), names

    cli_container.shell.reset_override()
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `pytest tests/cli/test_worktree.py::test_finish_captures_diagnostic_when_main_is_dirty_post_op -v`
Expected: FAIL — either no diagnostic captured, or `assert any(...)` returns False.

- [ ] **Step 3.3: Add post-op capture to `mship finish`**

Edit `src/mship/cli/worktree.py`. Locate the `finish` command handler. Find the successful-exit path (usually right before `typer.Exit(code=0)` or just before `return`).

Insert this defensive block just before the final successful exit (or at the end of the command body, wrapped in try/except so diagnostics never break finish):

```python
        # Post-op diagnostic: if any affected repo's main-checkout path is
        # dirty after finish completed, capture a snapshot. Silent — users
        # see the snapshot count via `mship doctor`. See spec 2026-04-21.
        try:
            from mship.core.diagnostics import capture_snapshot
            post_op_repos: dict[str, Path] = {}
            for _repo_name in t.affected_repos:
                _repo_cfg = config.repos.get(_repo_name)
                if _repo_cfg is None:
                    continue
                if _repo_cfg.git_root is not None:
                    _parent = config.repos.get(_repo_cfg.git_root)
                    if _parent is None:
                        continue
                    _main_path = Path(_parent.path).resolve()
                else:
                    _main_path = Path(_repo_cfg.path).resolve()
                if _main_path.is_dir():
                    post_op_repos[_repo_name] = _main_path
            any_dirty = False
            for _name, _path in post_op_repos.items():
                _res = shell.run("git status --porcelain", cwd=_path)
                if _res.returncode == 0 and _res.stdout.strip():
                    any_dirty = True
                    break
            if any_dirty and post_op_repos:
                capture_snapshot(
                    "finish", "dirty-main-post-op",
                    container.state_dir(),
                    repos=post_op_repos,
                )
        except Exception:
            # Diagnostics is strictly best-effort.
            pass
```

Place this AFTER all the normal finish logic (PR creation, state mutation, `finished_at` stamp, coordination block updates) and BEFORE the final return/exit.

- [ ] **Step 3.4: Add post-op capture to `mship close`**

Still in `src/mship/cli/worktree.py`, find the `close` command handler. Insert the same post-op block just before the successful exit. The same snippet works verbatim; re-use it.

- [ ] **Step 3.5: Run tests to verify they pass**

Run: `pytest tests/cli/test_worktree.py -v -k "captures_diagnostic or does_not_capture"`
Expected: 2 passed.

- [ ] **Step 3.6: Run the full test file to catch regressions**

Run: `pytest tests/cli/test_worktree.py -v 2>&1 | tail -15`
Expected: all green.

- [ ] **Step 3.7: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat(worktree): silent post-op diagnostic capture on finish + close"
mship journal "mship finish and mship close now capture a diagnostic if main is dirty post-op (data collection for the stale-main-index bug)" --action committed
```

---

## Task 4: `mship doctor` diagnostics row

**Files:**
- Modify: `src/mship/core/doctor.py` — `DoctorChecker.__init__` accepts `state_dir`; `run()` appends a `diagnostics` row when snapshots exist.
- Modify: `src/mship/cli/doctor.py` — pass `container.state_dir()`.
- Modify: `tests/core/test_doctor.py` — new tests.

**Context:** When `.mothership/diagnostics/` contains any `.json` file, `mship doctor` surfaces a `warn` row with the count and the suggestion to review or prune.

- [ ] **Step 4.1: Write failing tests**

Append to `tests/core/test_doctor.py`:

```python
def test_doctor_diagnostics_row_warn_when_snapshots_present(workspace: Path):
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.util.shell import ShellRunner
    from unittest.mock import MagicMock
    # Pre-create a diagnostic file.
    diag_dir = workspace / ".mothership" / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    (diag_dir / "2026-01-01T00-00-00Z-sync-pre-recovery.json").write_text("{}")

    config = ConfigLoader.load(workspace / "mothership.yaml")
    shell = MagicMock(spec=ShellRunner)
    from mship.util.shell import ShellResult
    shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")

    report = DoctorChecker(config, shell, state_dir=workspace / ".mothership").run()
    diag_checks = [c for c in report.checks if c.name == "diagnostics"]
    assert len(diag_checks) == 1
    assert diag_checks[0].status == "warn"
    assert "1" in diag_checks[0].message or "snapshot" in diag_checks[0].message.lower()


def test_doctor_no_diagnostics_row_when_absent(workspace: Path):
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.util.shell import ShellRunner
    from unittest.mock import MagicMock
    from mship.util.shell import ShellResult

    config = ConfigLoader.load(workspace / "mothership.yaml")
    shell = MagicMock(spec=ShellRunner)
    shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")

    report = DoctorChecker(config, shell, state_dir=workspace / ".mothership").run()
    diag_checks = [c for c in report.checks if c.name == "diagnostics"]
    assert len(diag_checks) == 0
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `pytest tests/core/test_doctor.py -v -k diagnostics`
Expected: FAIL — `DoctorChecker.__init__() got an unexpected keyword argument 'state_dir'`.

- [ ] **Step 4.3: Update `DoctorChecker` to accept `state_dir`**

Edit `src/mship/core/doctor.py`. Find:

```python
class DoctorChecker:
    """Run health checks on a mothership workspace."""

    def __init__(self, config: WorkspaceConfig, shell: ShellRunner) -> None:
        self._config = config
        self._shell = shell
```

Replace with:

```python
class DoctorChecker:
    """Run health checks on a mothership workspace."""

    def __init__(
        self,
        config: WorkspaceConfig,
        shell: ShellRunner,
        *,
        state_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._shell = shell
        self._state_dir = state_dir
```

(Default `state_dir=None` keeps existing test callers working; the diagnostics row only appears when state_dir is provided AND the directory has snapshots.)

In the `run()` method, AFTER the existing `go-task binary` check block (around line 230-ish, before `# Dev-mode trap:` or similar), insert:

```python
        # Pending diagnostics snapshots (spec 2026-04-21).
        if self._state_dir is not None:
            diag_dir = Path(self._state_dir) / "diagnostics"
            if diag_dir.is_dir():
                count = sum(1 for _ in diag_dir.glob("*.json"))
                if count > 0:
                    report.checks.append(CheckResult(
                        name="diagnostics",
                        status="warn",
                        message=(
                            f"{count} snapshot(s) in .mothership/diagnostics/ — "
                            f"review for unexpected-state captures; `rm -rf` to clear"
                        ),
                    ))
```

- [ ] **Step 4.4: Update the CLI wiring**

Edit `src/mship/cli/doctor.py`. Find:

```python
        checker = DoctorChecker(config, shell)
```

Replace with:

```python
        checker = DoctorChecker(config, shell, state_dir=container.state_dir())
```

- [ ] **Step 4.5: Run tests to verify they pass**

Run: `pytest tests/core/test_doctor.py -v`
Expected: all pass (2 new + existing). The existing tests that construct `DoctorChecker(config, shell)` without `state_dir` still work because the param is keyword-only with a default.

- [ ] **Step 4.6: Commit**

```bash
git add src/mship/core/doctor.py src/mship/cli/doctor.py tests/core/test_doctor.py
git commit -m "feat(doctor): diagnostics row when .mothership/diagnostics/ has snapshots"
mship journal "mship doctor now surfaces a warn row when forensic snapshots are pending review" --action committed
```

---

## Task 5: End-to-end smoke + finish PR

**Files:**
- None (verification + PR only).

**Context:** Unit + integration tests already cover the recovery, capture, and doctor-row paths. A quick end-to-end manual smoke verifies the integration when run against a real `mship sync` invocation.

- [ ] **Step 5.1: Reinstall tool**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/fix-stale-main-index-warn-on-wrong-cwd-gate-finish-on-tests
uv tool install --reinstall --from . mothership
```

- [ ] **Step 5.2: Smoke the recovery path**

Set up a scratch workspace where main is one commit behind origin AND the dirty tracked file matches upstream content:

```bash
rm -rf /tmp/stale-smoke
mkdir -p /tmp/stale-smoke && cd /tmp/stale-smoke

# Origin (bare) and local clone.
git init --bare -q origin.git
git clone -q origin.git local
cd local
git config user.email "t@t" && git config user.name "t"
echo "A" > a.txt && git add . && git commit -qm "A"
git push -qu origin main

# Advance origin from another clone.
cd ..
git clone -q origin.git other
cd other
git config user.email "t@t" && git config user.name "t"
echo "B" > a.txt && git add . && git commit -qm "B"
git push -q

# Back in local: dirty a.txt to match upstream (simulates the bug pattern).
cd ../local
echo "B" > a.txt   # working tree matches origin/main without having pulled

# Set up minimal mothership workspace rooted at local.
cat > mothership.yaml <<'EOF'
workspace: stale-smoke
repos:
  self:
    path: .
    type: service
EOF
mkdir -p .mothership

# Run mship sync; it should auto-recover.
mship sync
```

Expected output contains:
- `self: fast-forwarded (recovered from stale main state)` or similar recovery message.
- After the command, `git status --porcelain` shows clean.
- `.mothership/diagnostics/<ts>-sync-dirty-main-pre-recovery.json` exists.

Run `mship doctor` to confirm the row:

```bash
mship doctor 2>&1 | grep -i diagnostic
```

Expected: `warn   diagnostics   1 snapshot(s) …`.

- [ ] **Step 5.3: Smoke the refuse path**

Reset, then dirty a.txt with content that DOESN'T match upstream:

```bash
cd /tmp/stale-smoke/local
git reset --hard HEAD
echo "Z" > a.txt   # user work, not in upstream

mship sync; echo "EXIT: $?"
```

Expected:
- `self: skipped (dirty_worktree — ...)` — refusal, original message.
- `EXIT: 1`.
- `.mothership/diagnostics/` now has TWO files (pre-recovery + real-user-work).
- `a.txt` still contains "Z" (untouched).

- [ ] **Step 5.4: Cleanup**

```bash
rm -rf /tmp/stale-smoke
```

- [ ] **Step 5.5: Full pytest**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/fix-stale-main-index-warn-on-wrong-cwd-gate-finish-on-tests
pytest tests/ 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 5.6: Open PR**

Write `/tmp/stale-recovery-body.md`:

```markdown
## Summary

Two-part change to address the recurring "stale main index after merge" bug observed multiple times this session:

1. **Diagnostics library** — new `capture_snapshot()` writes JSON forensics blobs to `.mothership/diagnostics/<ts>-<command>-<reason>.json` whenever mship commands detect anomalous state. Ships the instrumentation the future root-cause investigation will need.

2. **`mship sync` auto-recovery** — when audit blocks solely on `dirty_worktree`, compare each dirty tracked file's blob hash against `origin/<branch>`. If every file matches, the dirty state IS the fast-forward delta: reset those files and let the normal fast-forward run. If any file doesn't match, bail before touching anything — user's working tree stays exactly as it was.

`mship finish` and `mship close` also capture silent post-op diagnostics when they observe main dirty at exit. `mship doctor` gains a `diagnostics` warn row when snapshots are pending review.

## Why hash-compare instead of stash

- Provably safe: no state mutation until every file's redundancy is verified. Wrong guess → no files touched.
- No stash/pop edge cases. No `git reset --hard` in any path.
- Untracked files block recovery outright (preserves anything unexpected).

## Scope

- Only `mship sync` actively recovers. `mship finish` / `mship close` only capture diagnostics, no behavior change.
- Recovery only triggers when `dirty_worktree` is the SOLE blocking audit code.
- No root-cause fix yet — this ships the instrumentation and the mitigation together.

## Related

- Issues #80, #81 filed for the other two papercuts (wrong-cwd warning, finish test-evidence gate); followups.
- Decision log + algorithm details in the spec.

## Test plan

- [x] `tests/core/test_diagnostics.py`: 6 unit tests for `capture_snapshot` (happy, filename-safe, dir-creation, repos populated, extra kwarg, write-failure-non-fatal).
- [x] `tests/core/test_sync_recovery.py`: 7 integration tests against real `file://` origins (happy, user-work-preserved, not-behind, untracked, file-missing-upstream, multi-match, multi-mismatch-preserves-all).
- [x] `tests/cli/test_worktree.py`: 2 new integration tests for finish post-op capture (dirty → captured; clean → not captured).
- [x] `tests/core/test_doctor.py`: 2 new tests for the diagnostics row.
- [x] Full suite: all pass.
- [x] Manual smoke: scratch workspace with dirty-main-matches-upstream state → `mship sync` recovers; with real user work → refusal preserved + two diagnostics captured.

Closes the top-ranked recurring papercut from session feedback.
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/fix-stale-main-index-warn-on-wrong-cwd-gate-finish-on-tests
mship finish --body-file /tmp/stale-recovery-body.md
```

Expected: PR URL returned.

---

## Done when

- [x] `capture_snapshot()` writes diagnostics with all required keys; write-failure non-fatal.
- [x] `_try_recover_stale_main()` proves redundancy via hash-compare before any state mutation; untracked files block; never uses stash or `reset --hard`.
- [x] `sync_repos` and `_result_for` plumb `state_dir`; CLI sync passes `container.state_dir()`.
- [x] `mship finish` / `mship close` silently capture post-op diagnostics when main is dirty.
- [x] `mship doctor` surfaces a `warn diagnostics` row when `.mothership/diagnostics/` has snapshots; zero rows when absent.
- [x] 17 new tests pass (6 diagnostics + 7 sync recovery + 2 finish post-op + 2 doctor).
- [x] Full pytest green.
- [x] Manual smoke confirms recovery on the bug pattern and preservation on real user work.
