# mship spawn: fetch + fast-forward base before cutting a worktree — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** `mship-spawndispatch` (approved) — `specs/2026-06-22-mship-spawndispatch.md`.

**Goal:** When `WorktreeManager.spawn()` creates a task worktree for an active repo, cut the new branch from the freshly-fetched `origin/<base>` instead of the canonical checkout's (possibly stale) local base — so a task dispatched right after a merge isn't born behind. Degrade gracefully offline / without a remote.

**Architecture:** One change at the shared chokepoint `WorktreeManager.spawn()` covers both `mship spawn` and `mship spec dispatch` (both call `spawn()`). Reuse the existing `offline` flag (already a `spawn()` param and a `mship spawn --offline` CLI flag) as the opt-out — **no new `--no-fetch-base` flag needed.** Reuse `GitRunner.fetch_remote_ref` (best-effort fetch) and `has_uncommitted_changes`; add small git helpers.

**Tech Stack:** Python, pytest with real temp git repos + a bare `origin` (matching `tests/core/test_worktree.py`'s `GitRunner()` + self-contained-`mothership.yaml` style). Run tests via the repo's test target.

**Note on the dev binary:** `mship doctor` warns the installed `mship` may lag this source. Run the suite from the worktree via `uv run task test` (or `uv run pytest tests/core/test_worktree.py -q`), not the stale installed binary. `mship test` also works (it shells out to the repo's `task test`, which runs the worktree source).

---

<!-- mship:task id=1 -->
### Task 1: GitRunner helpers — start-point, has_remote, opportunistic fast-forward

**Files:**
- Modify: `src/mship/util/git.py`
- Test: `tests/util/test_git.py` (create if absent; otherwise append)

- [ ] **Step 1: Write the failing tests**

Create/append `tests/util/test_git.py`:

```python
import os
import subprocess
from pathlib import Path

from mship.util.git import GitRunner

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=ENV)


def _rev(cwd, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_origin_ahead(tmp_path: Path):
    """Return (repo, origin_tip) where `repo`'s local main is one commit behind origin/main."""
    repo = tmp_path / "svc"
    repo.mkdir()
    _run(["git", "init", "-b", "main", str(repo)], tmp_path)
    (repo / "a.txt").write_text("1")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "c1"], repo)
    origin = tmp_path / "svc-origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)], tmp_path)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    _run(["git", "push", "origin", "main"], repo)
    clone = tmp_path / "svc-clone"
    _run(["git", "clone", str(origin), str(clone)], tmp_path)
    (clone / "b.txt").write_text("2")
    _run(["git", "add", "-A"], clone)
    _run(["git", "commit", "-m", "c2"], clone)
    _run(["git", "push", "origin", "main"], clone)
    # local `repo` still at c1; fetch so origin/main is known locally
    _run(["git", "fetch", "origin", "main"], repo)
    return repo, _rev(clone)


def test_worktree_add_with_start_point_branches_from_ref(tmp_path):
    repo, origin_tip = _repo_with_origin_ahead(tmp_path)
    git = GitRunner()
    wt = tmp_path / "wt"
    git.worktree_add(repo, wt, "feat/x", start_point="origin/main")
    assert _rev(wt) == origin_tip  # cut from origin tip, not local HEAD


def test_worktree_add_without_start_point_uses_local_head(tmp_path):
    repo, origin_tip = _repo_with_origin_ahead(tmp_path)
    git = GitRunner()
    wt = tmp_path / "wt"
    git.worktree_add(repo, wt, "feat/x")
    assert _rev(wt) == _rev(repo)   # local HEAD (behind)
    assert _rev(wt) != origin_tip


def test_has_remote(tmp_path):
    repo, _ = _repo_with_origin_ahead(tmp_path)
    git = GitRunner()
    assert git.has_remote(repo) is True
    bare = tmp_path / "no-remote"
    bare.mkdir()
    _run(["git", "init", "-b", "main", str(bare)], tmp_path)
    assert git.has_remote(bare) is False


def test_fast_forward_if_clean_advances_clean_behind_base(tmp_path):
    repo, origin_tip = _repo_with_origin_ahead(tmp_path)
    git = GitRunner()
    assert git.fast_forward_if_clean(repo, "main") is True
    assert _rev(repo, "main") == origin_tip


def test_fast_forward_if_clean_skips_dirty_tree(tmp_path):
    repo, origin_tip = _repo_with_origin_ahead(tmp_path)
    (repo / "dirty.txt").write_text("uncommitted")
    git = GitRunner()
    assert git.fast_forward_if_clean(repo, "main") is False
    assert _rev(repo, "main") != origin_tip   # untouched
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/util/test_git.py -q`
Expected: FAIL — `worktree_add() got an unexpected keyword 'start_point'`, `has_remote`/`fast_forward_if_clean` missing.

- [ ] **Step 3: Implement the helpers**

In `src/mship/util/git.py`, change `worktree_add` to accept an optional start-point and add two helpers:

```python
    def worktree_add(
        self, repo_path: Path, worktree_path: Path, branch: str, start_point: str | None = None
    ) -> None:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "worktree", "add", str(worktree_path), "-b", branch]
        if start_point is not None:
            cmd.append(start_point)
        subprocess.run(cmd, cwd=repo_path, check=True, capture_output=True, text=True)

    def has_remote(self, repo_path: Path, remote: str = "origin") -> bool:
        """True if `remote` is configured for the repo."""
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            cwd=repo_path, capture_output=True, text=True,
        )
        return result.returncode == 0

    def fast_forward_if_clean(self, repo_path: Path, base: str, remote: str = "origin") -> bool:
        """Best-effort fast-forward of the canonical checkout's `base` to `<remote>/<base>`.

        Only acts when the checkout is ON `base`, has no uncommitted changes, and a
        fast-forward is possible. Returns whether it advanced. Never resets or forces;
        a diverged/ahead/dirty/off-base checkout is left untouched.
        """
        cur = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if cur.returncode != 0 or cur.stdout.strip() != base:
            return False
        if self.has_uncommitted_changes(repo_path):
            return False
        result = subprocess.run(
            ["git", "merge", "--ff-only", f"{remote}/{base}"],
            cwd=repo_path, capture_output=True, text=True,
        )
        return result.returncode == 0
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/util/test_git.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/util/git.py tests/util/test_git.py
git commit -m "feat(git): worktree_add start_point + has_remote + fast_forward_if_clean"
mship journal "added GitRunner.worktree_add start_point, has_remote, fast_forward_if_clean; tests green" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: spawn() freshens the base before cutting active-repo worktrees

**Files:**
- Modify: `src/mship/core/worktree.py`
- Test: `tests/core/test_worktree.py` (append)

- [ ] **Step 1: Write the failing tests** — append to `tests/core/test_worktree.py` (it already imports `ConfigLoader`, `DependencyGraph`, `StateManager`, `GitRunner`, `ShellRunner`, `ShellResult`, `LogManager`, `WorktreeManager`, `MagicMock`):

```python
def _spawn_env():
    import os
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}


def _g(args, cwd):
    import subprocess
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=_spawn_env())


def _sha(cwd, ref="HEAD"):
    import subprocess
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def _svc_with_origin_ahead(tmp_path):
    """Build workspace/svc whose local main is 1 commit behind origin/main. Returns origin_tip."""
    repo = tmp_path / "svc"
    repo.mkdir()
    _g(["git", "init", "-b", "main", str(repo)], tmp_path)
    (repo / "a.txt").write_text("1"); _g(["git", "add", "-A"], repo); _g(["git", "commit", "-m", "c1"], repo)
    origin = tmp_path / "svc-origin.git"
    _g(["git", "init", "--bare", "-b", "main", str(origin)], tmp_path)
    _g(["git", "remote", "add", "origin", str(origin)], repo); _g(["git", "push", "origin", "main"], repo)
    clone = tmp_path / "svc-clone"
    _g(["git", "clone", str(origin), str(clone)], tmp_path)
    (clone / "b.txt").write_text("2"); _g(["git", "add", "-A"], clone); _g(["git", "commit", "-m", "c2"], clone)
    _g(["git", "push", "origin", "main"], clone)
    return _sha(clone)


def _mgr(tmp_path):
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text("workspace: t\nrepos:\n  svc:\n    path: ./svc\n    type: service\n    base_branch: main\n")
    config = ConfigLoader.load(cfg)
    graph = DependencyGraph(config)
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    state_mgr = StateManager(state_dir)
    git = GitRunner()
    shell = MagicMock(spec=ShellRunner)
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    log = MagicMock(spec=LogManager)
    return WorktreeManager(config, graph, state_mgr, git, shell, log)


def test_spawn_cuts_worktree_from_origin_when_local_behind(tmp_path):
    origin_tip = _svc_with_origin_ahead(tmp_path)
    repo = tmp_path / "svc"
    assert _sha(repo) != origin_tip   # local behind
    mgr = _mgr(tmp_path)
    mgr.spawn("fresh base", repos=["svc"], workspace_root=tmp_path)
    wt = tmp_path / ".worktrees" / "fresh-base" / "svc"
    assert _sha(wt) == origin_tip                 # worktree cut from origin tip (ac1/ac2)
    assert _sha(repo, "main") == origin_tip       # local base fast-forwarded (ac3)


def test_spawn_offline_uses_local_base(tmp_path):
    origin_tip = _svc_with_origin_ahead(tmp_path)
    repo = tmp_path / "svc"
    local_tip = _sha(repo)
    mgr = _mgr(tmp_path)
    mgr.spawn("offline test", repos=["svc"], workspace_root=tmp_path, offline=True)
    wt = tmp_path / ".worktrees" / "offline-test" / "svc"
    assert _sha(wt) == local_tip                  # local (stale) base, no fetch (ac5)
    assert _sha(wt) != origin_tip
    assert _sha(repo, "main") == local_tip        # local base untouched when offline


def test_spawn_without_remote_falls_back_to_local_base(tmp_path):
    # svc with NO origin remote → silent local base, spawn still succeeds (ac4)
    repo = tmp_path / "svc"; repo.mkdir()
    _g(["git", "init", "-b", "main", str(repo)], tmp_path)
    (repo / "a.txt").write_text("1"); _g(["git", "add", "-A"], repo); _g(["git", "commit", "-m", "c1"], repo)
    mgr = _mgr(tmp_path)
    result = mgr.spawn("no remote", repos=["svc"], workspace_root=tmp_path)
    wt = tmp_path / ".worktrees" / "no-remote" / "svc"
    assert _sha(wt) == _sha(repo)                 # local base, no error
    assert result.task.slug == "no-remote"


def test_spawn_dirty_canonical_base_not_fast_forwarded_but_worktree_fresh(tmp_path):
    origin_tip = _svc_with_origin_ahead(tmp_path)
    repo = tmp_path / "svc"
    (repo / "dirty.txt").write_text("uncommitted")   # canonical checkout dirty
    mgr = _mgr(tmp_path)
    mgr.spawn("dirty base", repos=["svc"], workspace_root=tmp_path)
    wt = tmp_path / ".worktrees" / "dirty-base" / "svc"
    assert _sha(wt) == origin_tip                 # worktree still cut from origin (ac1)
    assert _sha(repo, "main") != origin_tip       # dirty canonical base left untouched (ac3)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/core/test_worktree.py -q -k "origin or offline or remote or dirty_canonical"`
Expected: FAIL — worktrees cut from local HEAD (behind), so `_sha(wt) == origin_tip` assertions fail.

- [ ] **Step 3: Implement the freshen pass in `spawn()`**

In `src/mship/core/worktree.py`, hoist the workspace default once before the repo loop. Find where `all_repos` is computed (≈ line 438) and add right after it:

```python
        # Resolve the workspace default branch once (used for base resolution + the Task).
        default_base = workspace_default_branch_from_config(self._config)
```

Then, in the active-repo branch of the loop (the `else:` that currently calls `self._git.worktree_add(...)`, ≈ lines 518–523), replace it with:

```python
            else:
                start_point = None
                base = repo_config.base_branch or default_base or "main"
                if not offline and self._git.has_remote(repo_path):
                    if self._git.fetch_remote_ref(repo_path=repo_path, ref=base):
                        start_point = f"origin/{base}"            # cut from fetched tip
                        self._git.fast_forward_if_clean(repo_path=repo_path, base=base)
                    else:
                        setup_warnings.append(
                            f"{repo_name}: could not fetch origin/{base}; "
                            f"cutting worktree from local {base}"
                        )
                self._git.worktree_add(
                    repo_path=repo_path,
                    worktree_path=wt_path,
                    branch=branch,
                    start_point=start_point,
                )
```

Finally, reuse `default_base` for the Task (find `base_branch=workspace_default_branch_from_config(self._config),` ≈ line 557 and change it to):

```python
            base_branch=default_base,
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/core/test_worktree.py -q`
Expected: PASS (the 4 new tests + all pre-existing worktree tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat(spawn): cut active-repo worktrees from fetched origin/<base>; ff local base when clean"
mship journal "spawn() now fetches + cuts active worktrees from origin/<base> (+opportunistic local ff); offline/no-remote fall back to local; tests green" --action committed
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: Full verification + phase transition

**Files:** none (verification only).

- [ ] **Step 1: Run the full suite from source**

Run: `uv run task test` (or `uv run pytest -q`). Expected: green, no regressions. (Use `uv run` so the worktree source is exercised, per the dev-binary note.)

- [ ] **Step 2: Confirm acceptance criteria**

Re-read `specs/2026-06-22-mship-spawndispatch.md`. Confirm: ac1 (worktree_add start_point + cut from origin/<base>) → Tasks 1+2; ac2 (both spawn & dispatch via the one chokepoint — `spec dispatch`/`_serve_spawn` call `spawn()` unchanged) → Task 2; ac3 (clean-behind local ff; dirty/diverged untouched) → Tasks 1+2; ac4 (fetch-fail / no-remote → local base, no block) → Task 2; ac5 (opt-out) → satisfied by the **existing `--offline`** flag (note in the journal that no new flag was added — `--offline` already provides the opt-out with the right semantics). Note any gap.

- [ ] **Step 3: Journal + transition**

```bash
mship journal "spawn-fresh-base implemented; opt-out via existing --offline (no new flag); full suite green via uv run" --action completed --test-state pass
mship phase review
```

> Then `mship finish --body-file <path>` with a real Summary + Test plan when ready to open the PR.
<!-- /mship:task -->

---

## Decisions & non-goals

- **Opt-out reuses the existing `--offline`** (CLI + `spawn(offline=...)`) instead of adding `--no-fetch-base` — same semantics (don't hit the network, use local refs), smaller surface. The spec's ac5 intent ("an opt-out") is satisfied.
- **No-remote repos stay silent** (only warn on a real fetch failure with a configured remote) to avoid noise for local-only workflows.
- Does NOT change base *resolution*, does NOT touch existing task branches, and does NOT make `spec dispatch` run the CLI audit gate (the chokepoint fix removes the need to). `mship sync`/`reconcile`/audit are complementary and unchanged.
