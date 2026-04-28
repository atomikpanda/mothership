# Hub Worktree Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-repo `<repo>/.worktrees/feat/<slug>/` layout with per-task hub `<workspace>/.worktrees/<slug>/<repo>/`, and auto-materialize `depends_on` siblings as detached-HEAD passive worktrees pinned to `origin/<expected_branch || base_branch>`.

**Architecture:** Per-task hub directory under workspace root. Affected repos checked out on `feat/<slug>`. Passive deps materialized at `origin/<ref>` after fetch (no stale local refs). Pre-commit hook refuses commits in passive worktrees. State.yaml gains `Task.passive_repos: set[str]` so all consumers can ask "is this passive?"

**Tech Stack:** Python 3.13, pydantic, typer, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-04-28-hub-worktree-layout-design.md`

**PR sequence:** 8 phases, each ending in green tests + commit. Each phase is a natural PR boundary; in particular, Phase 1 (state field) and Phase 2 (hub layout) must merge in order, after which Phases 3–8 can be parallelized if desired.

---

## File Map

**Created:**
- (none — extend existing modules)

**Modified:**
- `src/mship/core/state.py` — add `Task.passive_repos`
- `src/mship/core/worktree.py` — hub layout, passive worktree creation, abort cleanup
- `src/mship/util/git.py` — `worktree_add_detached`, `fetch_remote_ref` helpers
- `src/mship/cli/worktree.py` — pass `--offline` to spawn
- `src/mship/cli/internal.py` — `_check-commit` refuses passive worktrees
- `src/mship/core/repo_state.py` — passive issue codes (`passive_drift`, `passive_dirty_worktree`, `passive_fetch_failed`)
- `src/mship/cli/audit.py` — surface passive issues
- `src/mship/core/repo_sync.py` — refresh passive worktrees
- `src/mship/cli/sync.py` — `--no-passive` flag
- `src/mship/core/prune.py` — scan `<workspace>/.worktrees/` for orphans alongside legacy `<repo>/.worktrees/`
- `src/mship/core/doctor.py` — workspace `.gitignore` check
- `src/mship/core/switch.py` — annotate passive in handoff
- `src/mship/cli/switch.py` — warn when target is passive
- `src/mship/cli/phase.py`, `src/mship/cli/exec.py` — refuse `phase`/`test` against passive active_repo

**Tests modified or added:**
- `tests/core/test_state.py`, `tests/core/test_worktree.py`, `tests/cli/test_internal.py`, `tests/test_hook_integration.py`, `tests/core/test_repo_state.py`, `tests/core/test_repo_sync.py`, `tests/cli/test_sync.py`, `tests/core/test_prune.py`, `tests/core/test_doctor.py`, `tests/core/test_switch.py`

**No code change required (verify only):**
- `mship bind refresh` — `WorktreeManager.refresh_bind_files` and `refresh_symlink_dirs` iterate over each `task.worktrees` entry (#71, #111). Passive worktrees live in the same `worktrees` dict, so they're picked up automatically. Phase 8 should add a smoke test confirming `bind refresh` covers passive worktrees, but no implementation change is required.

---

## Phase 1 — State model: `Task.passive_repos`

Foundation. Adds the field that every later phase consults.

### Task 1.1: Add `passive_repos` field to Task

**Files:**
- Modify: `src/mship/core/state.py:19-36`
- Test: `tests/core/test_state.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_state.py`:

```python
def test_task_passive_repos_defaults_empty(tmp_path):
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone
    sm = StateManager(tmp_path)
    state = WorkspaceState(tasks={
        "t": Task(
            slug="t", description="d", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["a"], branch="feat/t",
        )
    })
    sm.save(state)
    loaded = sm.load()
    assert loaded.tasks["t"].passive_repos == set()


def test_task_passive_repos_round_trips(tmp_path):
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone
    sm = StateManager(tmp_path)
    state = WorkspaceState(tasks={
        "t": Task(
            slug="t", description="d", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["a", "b"], branch="feat/t",
            passive_repos={"b"},
        )
    })
    sm.save(state)
    loaded = sm.load()
    assert loaded.tasks["t"].passive_repos == {"b"}
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/core/test_state.py::test_task_passive_repos_defaults_empty tests/core/test_state.py::test_task_passive_repos_round_trips -v
```

Expected: FAIL — `Task.__init__()` rejects unexpected `passive_repos`.

- [ ] **Step 3: Add the field**

Edit `src/mship/core/state.py`. Inside `class Task(BaseModel)`, after `base_branch: str | None = None`:

```python
    passive_repos: set[str] = set()
```

- [ ] **Step 4: Update `_save_nolock` so the set serializes deterministically**

In `state.py`, find the section that converts `worktrees` for serialization:

```python
        for task in data.get("tasks", {}).values():
            task["worktrees"] = {
                k: str(v) for k, v in task.get("worktrees", {}).items()
            }
```

Add immediately after:

```python
            if "passive_repos" in task:
                task["passive_repos"] = sorted(task["passive_repos"])
```

(Pydantic dumps `set` as `list` already; sorting makes diff-friendly.)

- [ ] **Step 5: Run tests, expect PASS**

```
uv run pytest tests/core/test_state.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/state.py tests/core/test_state.py
git commit -m "feat(state): add Task.passive_repos for hub layout passive worktrees"
mship journal "phase 1: passive_repos field on Task; round-trip tested" --action committed
```

---

## Phase 2 — Hub layout for affected repos

Move new spawns to `<workspace>/.worktrees/<slug>/<repo>/`. No passive yet — that's Phase 3. Legacy in-flight tasks unaffected.

### Task 2.1: Compute hub paths in WorktreeManager.spawn

**Files:**
- Modify: `src/mship/core/worktree.py` (the `spawn` method, ~line 398)
- Test: `tests/core/test_worktree.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_worktree.py`:

```python
def test_spawn_uses_hub_layout(worktree_deps):
    """New spawns place worktrees at <workspace>/.worktrees/<slug>/<repo>/, not <repo>/.worktrees/."""
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("hub layout", repos=["shared", "auth-service"], workspace_root=workspace)
    state = state_mgr.load()
    task = state.tasks["hub-layout"]
    expected_hub = workspace / ".worktrees" / "hub-layout"
    assert Path(task.worktrees["shared"]) == expected_hub / "shared"
    assert Path(task.worktrees["auth-service"]) == expected_hub / "auth-service"
    assert (expected_hub / "shared").exists()
    assert (expected_hub / "auth-service").exists()


def test_spawn_writes_single_marker_at_hub_root(worktree_deps):
    """One .mship-workspace marker per hub, not per worktree."""
    from mship.core.workspace_marker import MARKER_NAME
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("marker test", repos=["shared", "auth-service"], workspace_root=workspace)
    hub = workspace / ".worktrees" / "marker-test"
    assert (hub / MARKER_NAME).is_file()
    # No per-worktree markers
    assert not (hub / "shared" / MARKER_NAME).exists()
    assert not (hub / "auth-service" / MARKER_NAME).exists()


def test_spawn_workspace_gitignore_includes_worktrees(worktree_deps, tmp_path):
    """Workspace root .gitignore (if root is a git repo) gets `.worktrees` added."""
    import subprocess
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    # Make the workspace root itself a git repo
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("ignore test", repos=["shared"], workspace_root=workspace)
    gi = workspace / ".gitignore"
    assert gi.exists()
    assert ".worktrees" in gi.read_text().splitlines()
```

- [ ] **Step 2: Run tests, expect FAIL**

```
uv run pytest tests/core/test_worktree.py::test_spawn_uses_hub_layout tests/core/test_worktree.py::test_spawn_writes_single_marker_at_hub_root tests/core/test_worktree.py::test_spawn_workspace_gitignore_includes_worktrees -v
```

Expected: FAIL — current spawn places worktrees under `<repo>/.worktrees/`.

- [ ] **Step 3: Refactor `WorktreeManager.spawn` to use hub paths**

Replace the body of the `for repo_name in ordered:` loop in `src/mship/core/worktree.py` (~lines 429–502).

The new loop has THREE branches:
1. `git_root` repos: nested under their parent's hub worktree (path math same as today, just rooted differently).
2. Normal repos: hub worktree at `<hub>/<repo>/`.
3. (Phase 3 will add a fourth branch for passive — placeholder NotImplementedError for now.)

Replace from line 429 (`for repo_name in ordered:`) through line 502 (end of normal-repo branch) with:

```python
        if workspace_root is None:
            raise ValueError(
                "workspace_root required for hub layout spawn; "
                "callers must pass container.config_path().parent"
            )

        hub = workspace_root / ".worktrees" / slug
        hub.mkdir(parents=True, exist_ok=True)

        # Workspace-root .gitignore gets .worktrees if root is a git repo.
        if (workspace_root / ".git").exists():
            if not self._git.is_ignored(workspace_root, ".worktrees"):
                self._git.add_to_gitignore(workspace_root, ".worktrees")

        for repo_name in ordered:
            repo_config = self._config.repos[repo_name]

            if repo_config.git_root is not None:
                # Subdirectory child: nested inside parent's hub worktree.
                parent_wt = worktrees.get(repo_config.git_root)
                if parent_wt is None:
                    parent_wt = self._config.repos[repo_config.git_root].path
                effective = parent_wt / repo_config.path
                worktrees[repo_name] = effective

                symlink_warnings = self._create_symlinks(repo_name, repo_config, effective)
                setup_warnings.extend(symlink_warnings)
                bind_warnings = self._copy_bind_files(repo_name, repo_config, effective)
                setup_warnings.extend(bind_warnings)

                if not skip_setup and shutil.which("task") is not None:
                    actual_setup = repo_config.tasks.get("setup", "setup")
                    setup_result = self._shell.run_task(
                        task_name="setup",
                        actual_task_name=actual_setup,
                        cwd=effective,
                        env_runner=repo_config.env_runner or self._config.env_runner,
                    )
                    if setup_result.returncode != 0:
                        setup_warnings.append(
                            f"{repo_name}: setup failed (task '{actual_setup}') — "
                            f"{setup_result.stderr.strip()[:200]}"
                        )
                continue

            # Normal repo: hub worktree.
            repo_path = repo_config.path
            wt_path = hub / repo_name

            self._git.worktree_add(
                repo_path=repo_path,
                worktree_path=wt_path,
                branch=branch,
            )
            worktrees[repo_name] = wt_path

            symlink_warnings = self._create_symlinks(repo_name, repo_config, wt_path)
            setup_warnings.extend(symlink_warnings)
            bind_warnings = self._copy_bind_files(repo_name, repo_config, wt_path)
            setup_warnings.extend(bind_warnings)

            if not skip_setup and shutil.which("task") is not None:
                actual_setup = repo_config.tasks.get("setup", "setup")
                setup_result = self._shell.run_task(
                    task_name="setup",
                    actual_task_name=actual_setup,
                    cwd=wt_path,
                    env_runner=repo_config.env_runner or self._config.env_runner,
                )
                if setup_result.returncode != 0:
                    setup_warnings.append(
                        f"{repo_name}: setup failed (task '{actual_setup}') — "
                        f"{setup_result.stderr.strip()[:200]}"
                    )

        # Single .mship-workspace marker at the hub root.
        write_marker(hub, workspace_root)
```

- [ ] **Step 4: Remove the old per-repo `.worktrees` and `.mship-workspace` gitignore additions**

The old code (lines 464–467, before the refactor) added `.worktrees` and `MARKER_NAME` to the canonical repo's gitignore. These additions are no longer needed (worktrees and marker live in workspace, not in repos), and the per-repo additions are inert in the new layout. Leave any pre-existing entries as-is — don't backfill removals. The `add_to_gitignore` call on the workspace root replaces them.

(No code change in this step beyond verifying you removed those lines in step 3's replacement.)

- [ ] **Step 5: Run tests**

```
uv run pytest tests/core/test_worktree.py -v
```

Expected: the three new tests PASS. Some existing tests in `test_worktree.py` and integration tests will FAIL because they assume the old layout — fix in Task 2.2.

- [ ] **Step 6: Commit (don't mship journal yet — phase isn't green)**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat(worktree): hub layout for spawn (passive deferred to phase 3)"
```

### Task 2.2: Update existing tests that assume per-repo layout

**Files:**
- Modify: `tests/core/test_worktree.py`, `tests/test_hook_integration.py`, `tests/test_integration.py`, `tests/test_scaling_integration.py`, `tests/test_resilience_integration.py`, `tests/test_monorepo_integration.py`, `tests/cli/test_worktree.py`, `tests/cli/test_check_commit.py`, `tests/cli/test_internal_hooks.py`, `tests/cli/test_multi_task.py`, `tests/cli/test_internal.py`, `tests/cli/test_commit.py`

- [ ] **Step 1: Find all assertions on the old path layout**

```bash
grep -rn '\.worktrees/feat\|/\.worktrees/' tests/ --include='*.py' | grep -v 'workspace/.worktrees/'
```

For each match, decide:
- If the test expects `<repo>/.worktrees/feat/<slug>/`, rewrite to expect `<workspace>/.worktrees/<slug>/<repo>/`.
- If the test seeds a fake worktree (via `git worktree add` directly) for an unrelated test, leave it — those aren't testing layout.
- Existing per-repo `test_spawn_ensures_gitignore` (`tests/core/test_worktree.py:68-74`): now obsolete — delete it. Replace with `test_spawn_workspace_gitignore_includes_worktrees` (already added in Task 2.1).

- [ ] **Step 2: Run the full test suite**

```
uv run pytest -q
```

Expected: failures concentrated in tests that check worktree paths or that the marker is per-worktree. Fix each by:
- Updating expected path to hub layout
- Updating any `<wt>/.mship-workspace` checks to `<hub>/.mship-workspace`

(This step may take a single sitting of mechanical updates. Use the path pattern above as your guide.)

- [ ] **Step 3: Confirm green**

```
uv run pytest -q
```

Expected: 0 failures.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: update tests for hub worktree layout"
mship journal "phase 2 complete: hub layout shipped, all tests green" --action committed --test-state pass
```

### Task 2.3: Update `WorktreeManager.abort` for hub layout

**Files:**
- Modify: `src/mship/core/worktree.py:536-567`
- Test: `tests/core/test_worktree.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_worktree.py`:

```python
def test_abort_removes_hub_directory(worktree_deps):
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("abort test", repos=["shared", "auth-service"], workspace_root=workspace)
    hub = workspace / ".worktrees" / "abort-test"
    assert hub.exists()
    mgr.abort("abort-test")
    assert not hub.exists(), "abort should rm -rf the hub directory"
```

- [ ] **Step 2: Run test to verify it fails**

```
uv run pytest tests/core/test_worktree.py::test_abort_removes_hub_directory -v
```

Expected: FAIL — current `abort` removes individual git worktrees but leaves the hub dir.

- [ ] **Step 3: Update `abort`**

In `src/mship/core/worktree.py`, after the existing per-repo cleanup loop in `abort` (after line 562 `pass`), and before `def _abort(s):`, add:

```python
        # Remove the hub directory for this task. Best-effort: legacy
        # per-repo-layout tasks won't have a hub dir, which is fine.
        try:
            workspace_root = (
                Path(next(iter(self._config.repos.values())).path)
                .resolve()
                .parents[0]
            )
            # The above is a fallback; prefer config-derived path:
            if hasattr(self._config, "_workspace_root"):
                workspace_root = Path(self._config._workspace_root)
            hub = workspace_root / ".worktrees" / task_slug
            if hub.exists() and hub.is_dir():
                import shutil as _shutil
                _shutil.rmtree(hub, ignore_errors=True)
        except Exception:
            pass
```

Wait — `WorkspaceConfig` doesn't expose workspace_root. Better approach: derive it from `task.worktrees` (each entry is under the hub).

Replace the snippet above with:

```python
        # Remove the hub directory for this task. Inferred from the first
        # worktree's parent.parent, which is `<workspace>/.worktrees/<slug>/`.
        # Legacy per-repo-layout tasks have a different parent shape — leave
        # those alone.
        try:
            sample_wt = next(iter(task.worktrees.values()), None)
            if sample_wt is not None:
                hub = Path(sample_wt).parent
                # Sanity: only remove if it looks like a hub (parent ends in .worktrees)
                if hub.name == task_slug and hub.parent.name == ".worktrees":
                    if hub.exists():
                        import shutil as _shutil
                        _shutil.rmtree(hub, ignore_errors=True)
        except Exception:
            pass
```

- [ ] **Step 4: Run test, expect PASS**

```
uv run pytest tests/core/test_worktree.py::test_abort_removes_hub_directory -v
```

- [ ] **Step 5: Run full suite to confirm no regression**

```
uv run pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat(worktree): abort removes hub directory"
mship journal "phase 2.3: abort cleans hub dir" --action committed
```

---

## Phase 3 — Passive worktrees (`depends_on` materialization)

Auto-materialize sibling repos that affected repos `depends_on` but the user didn't include in `--repos`. Detached HEAD at `origin/<expected_branch || base_branch>`. Symlinks + binds, no `task setup`.

### Task 3.1: Add `worktree_add_detached` and `fetch_remote_ref` to GitRunner

**Files:**
- Modify: `src/mship/util/git.py`
- Test: `tests/util/test_git.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/util/test_git.py`:

```python
def test_worktree_add_detached(tmp_path):
    import subprocess
    from mship.util.git import GitRunner
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"],
                   cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         check=True, capture_output=True, text=True).stdout.strip()
    git = GitRunner()
    wt = tmp_path / "wt"
    git.worktree_add_detached(repo_path=repo, worktree_path=wt, ref=sha)
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    assert head == sha
    branch = subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-q", "HEAD"],
                            capture_output=True, text=True).returncode
    assert branch != 0, "expected detached HEAD (symbolic-ref returns nonzero)"


def test_fetch_remote_ref_succeeds(tmp_path):
    """Smoke: fetch_remote_ref returns True when origin has the branch."""
    import subprocess
    from mship.util.git import GitRunner
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"],
                   cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone,
                   check=True, capture_output=True)
    git = GitRunner()
    assert git.fetch_remote_ref(repo_path=clone, ref="main") is True


def test_fetch_remote_ref_returns_false_on_failure(tmp_path):
    """Returns False when origin doesn't have the ref (or no remote)."""
    import subprocess
    from mship.util.git import GitRunner
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    git = GitRunner()
    assert git.fetch_remote_ref(repo_path=repo, ref="nonexistent") is False
```

- [ ] **Step 2: Run tests, expect FAIL**

```
uv run pytest tests/util/test_git.py::test_worktree_add_detached tests/util/test_git.py::test_fetch_remote_ref_succeeds tests/util/test_git.py::test_fetch_remote_ref_returns_false_on_failure -v
```

Expected: FAIL — methods don't exist yet.

- [ ] **Step 3: Add the methods**

In `src/mship/util/git.py`, after `worktree_add` (~line 16):

```python
    def worktree_add_detached(self, repo_path: Path, worktree_path: Path, ref: str) -> None:
        """Create a detached-HEAD worktree at `worktree_path` pointing at `ref`.

        `ref` may be a SHA, a tag, or a remote ref like `origin/main`.
        """
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), ref],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def fetch_remote_ref(self, repo_path: Path, ref: str, remote: str = "origin") -> bool:
        """Fetch a single ref from `remote`. Returns True on success, False on any failure.

        Used by passive-worktree materialization: we want to know if origin has
        the ref, and we want it locally as `<remote>/<ref>` for `worktree add`.
        """
        try:
            result = subprocess.run(
                ["git", "fetch", remote, ref],
                cwd=repo_path, capture_output=True, text=True, check=False, timeout=60,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
```

- [ ] **Step 4: Run tests, expect PASS**

```
uv run pytest tests/util/test_git.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/mship/util/git.py tests/util/test_git.py
git commit -m "feat(git): add worktree_add_detached and fetch_remote_ref helpers"
mship journal "phase 3.1: git helpers for passive worktrees" --action committed
```

### Task 3.2: Compute passive repo set in spawn

**Files:**
- Modify: `src/mship/core/worktree.py` (the `spawn` method)
- Test: `tests/core/test_worktree.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_worktree.py`:

```python
def test_spawn_materializes_passive_dep(worktree_deps):
    """When --repos is auth-service only, shared (its dep) becomes passive."""
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    # auth-service depends_on shared; spawn auth-service alone
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    # workspace_with_git fixture has no remotes set up; pass offline=True
    # so we use the local main branch instead of origin/main.
    mgr.spawn("passive dep", repos=["auth-service"],
              workspace_root=workspace, offline=True)
    state = state_mgr.load()
    task = state.tasks["passive-dep"]
    # affected_repos contains only what user asked for
    assert task.affected_repos == ["auth-service"]
    # passive_repos contains the dep
    assert task.passive_repos == {"shared"}
    # both are in worktrees
    assert "auth-service" in task.worktrees
    assert "shared" in task.worktrees
    # passive worktree exists on disk
    assert Path(task.worktrees["shared"]).exists()


def test_spawn_passive_worktree_is_detached(worktree_deps):
    import subprocess
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("detached check", repos=["auth-service"],
              workspace_root=workspace, offline=True)
    state = state_mgr.load()
    passive_wt = Path(state.tasks["detached-check"].worktrees["shared"])
    rc = subprocess.run(["git", "-C", str(passive_wt), "symbolic-ref", "-q", "HEAD"],
                        capture_output=True).returncode
    assert rc != 0, "passive worktree should be on detached HEAD"


def test_spawn_passive_skips_task_setup(worktree_deps):
    """Passive worktrees materialize symlinks/binds but don't run `task setup`."""
    config, graph, state_mgr, git, shell, workspace, log = worktree_deps
    mgr = WorktreeManager(config, graph, state_mgr, git, shell, log)
    mgr.spawn("no setup", repos=["auth-service"],
              workspace_root=workspace, offline=True)
    # `shell.run_task` should be called for affected (auth-service) but not passive (shared).
    setup_calls = [c for c in shell.run_task.call_args_list
                   if c.kwargs.get("task_name") == "setup"]
    cwds = {Path(c.kwargs.get("cwd")).name for c in setup_calls}
    assert "auth-service" in cwds
    assert "shared" not in cwds
```

- [ ] **Step 2: Run tests, expect FAIL**

```
uv run pytest tests/core/test_worktree.py::test_spawn_materializes_passive_dep tests/core/test_worktree.py::test_spawn_passive_worktree_is_detached tests/core/test_worktree.py::test_spawn_passive_skips_task_setup -v
```

Expected: FAIL — `spawn` has no `offline` kwarg, no passive logic.

- [ ] **Step 3: Add `offline` parameter and passive expansion to `spawn`**

In `src/mship/core/worktree.py`, change the `spawn` signature:

```python
    def spawn(
        self,
        description: str,
        repos: list[str] | None = None,
        skip_setup: bool = False,
        slug: str | None = None,
        workspace_root: Path | None = None,
        offline: bool = False,
    ) -> SpawnResult:
```

Inside `spawn`, after `ordered = self._graph.topo_sort(repos)`, compute the passive set:

```python
        # Passive expansion: collect transitive depends_on of each repo in
        # `ordered` that isn't already in `ordered`. Topo-sort the union so
        # passive deps materialize before their consumers.
        affected = set(ordered)
        passive: set[str] = set()
        frontier = list(ordered)
        while frontier:
            r = frontier.pop()
            for dep in self._graph.direct_deps(r):
                if dep not in affected and dep not in passive:
                    passive.add(dep)
                    frontier.append(dep)
        all_repos = self._graph.topo_sort(list(affected | passive))
```

(Confirm `DependencyGraph` has a `direct_deps(name)` method or equivalent. If not, replace with the existing API — likely `self._config.repos[r].depends_on` iterated with `.repo` access.)

Then change the `for repo_name in ordered:` loop header to:

```python
        for repo_name in all_repos:
            repo_config = self._config.repos[repo_name]
            is_passive = repo_name in passive
```

And inside the normal-repo branch, replace the `git.worktree_add(...)` + setup section with conditional logic:

```python
            # Normal repo: hub worktree.
            repo_path = repo_config.path
            wt_path = hub / repo_name

            if is_passive:
                # Passive: detached HEAD at origin/<expected || base>.
                ref = repo_config.expected_branch or repo_config.base_branch
                if ref is None:
                    raise ValueError(
                        f"Passive materialization for '{repo_name}' requires "
                        f"`expected_branch` or `base_branch` declared in "
                        f"mothership.yaml."
                    )
                if not offline:
                    fetched = self._git.fetch_remote_ref(repo_path=repo_path, ref=ref)
                    if not fetched:
                        raise RuntimeError(
                            f"Failed to fetch origin/{ref} for passive repo "
                            f"'{repo_name}'. Re-run with `--offline` to use "
                            f"the local ref."
                        )
                    target_ref = f"origin/{ref}"
                else:
                    target_ref = ref
                self._git.worktree_add_detached(
                    repo_path=repo_path,
                    worktree_path=wt_path,
                    ref=target_ref,
                )
            else:
                self._git.worktree_add(
                    repo_path=repo_path,
                    worktree_path=wt_path,
                    branch=branch,
                )
            worktrees[repo_name] = wt_path

            symlink_warnings = self._create_symlinks(repo_name, repo_config, wt_path)
            setup_warnings.extend(symlink_warnings)
            bind_warnings = self._copy_bind_files(repo_name, repo_config, wt_path)
            setup_warnings.extend(bind_warnings)

            # Skip task setup for passive worktrees.
            if not is_passive and not skip_setup and shutil.which("task") is not None:
                actual_setup = repo_config.tasks.get("setup", "setup")
                setup_result = self._shell.run_task(
                    task_name="setup",
                    actual_task_name=actual_setup,
                    cwd=wt_path,
                    env_runner=repo_config.env_runner or self._config.env_runner,
                )
                if setup_result.returncode != 0:
                    setup_warnings.append(
                        f"{repo_name}: setup failed (task '{actual_setup}') — "
                        f"{setup_result.stderr.strip()[:200]}"
                    )
```

Then update the Task construction to record passive_repos:

```python
        task = Task(
            slug=slug,
            description=description,
            phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=ordered,
            worktrees=worktrees,
            branch=branch,
            base_branch=base_branch,
            passive_repos=passive,
        )
```

- [ ] **Step 4: Verify `DependencyGraph` API**

```
grep -n "def \|direct_deps\|depends_on" src/mship/core/graph.py | head -20
```

If `direct_deps` doesn't exist, add it (or inline the iteration over `self._config.repos[r].depends_on`).

If you need to add `direct_deps` to `DependencyGraph`:

```python
    def direct_deps(self, repo_name: str) -> list[str]:
        """Return the direct dependency names of `repo_name`."""
        return [d.repo for d in self._config.repos[repo_name].depends_on]
```

- [ ] **Step 5: Run tests, expect PASS**

```
uv run pytest tests/core/test_worktree.py::test_spawn_materializes_passive_dep tests/core/test_worktree.py::test_spawn_passive_worktree_is_detached tests/core/test_worktree.py::test_spawn_passive_skips_task_setup -v
```

- [ ] **Step 6: Run full suite**

```
uv run pytest -q
```

- [ ] **Step 7: Commit**

```bash
git add src/mship/core/worktree.py src/mship/core/graph.py tests/core/test_worktree.py
git commit -m "feat(worktree): materialize depends_on as detached passive worktrees"
mship journal "phase 3.2: passive depends_on materialization" --action committed
```

### Task 3.3: Wire `--offline` flag through CLI

**Files:**
- Modify: `src/mship/cli/worktree.py` (the `spawn` typer command, ~line 212)
- Test: `tests/cli/test_worktree.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cli/test_worktree.py`:

```python
def test_spawn_cli_passes_offline_flag(workspace_with_git, tmp_path, monkeypatch):
    """`mship spawn --offline` sets offline=True in the manager call."""
    from typer.testing import CliRunner
    from unittest.mock import patch
    from mship.cli import app, container
    from pathlib import Path

    container.config_path.override(workspace_with_git / "mothership.yaml")
    container.state_dir.override(workspace_with_git / ".mothership")
    monkeypatch.chdir(workspace_with_git)
    try:
        runner = CliRunner()
        with patch("mship.core.worktree.WorktreeManager.spawn") as mock_spawn:
            from mship.core.worktree import SpawnResult
            from mship.core.state import Task
            from datetime import datetime, timezone
            mock_spawn.return_value = SpawnResult(
                task=Task(
                    slug="x", description="x", phase="plan",
                    created_at=datetime.now(timezone.utc),
                    affected_repos=["shared"], branch="feat/x",
                    worktrees={"shared": Path("/tmp/x")},
                ),
            )
            result = runner.invoke(
                app, ["spawn", "x", "--repos", "shared",
                      "--skip-setup", "--force-audit", "--offline"],
            )
            assert result.exit_code == 0, result.output
            assert mock_spawn.call_args.kwargs.get("offline") is True
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
```

- [ ] **Step 2: Run test, expect FAIL**

```
uv run pytest tests/cli/test_worktree.py::test_spawn_cli_passes_offline_flag -v
```

Expected: FAIL — `--offline` not a registered option.

- [ ] **Step 3: Add `--offline` to the `spawn` CLI command**

In `src/mship/cli/worktree.py`, in the `def spawn(...)` typer command (around line 212), add a parameter:

```python
        offline: bool = typer.Option(
            False, "--offline",
            help="Skip `git fetch` for passive worktrees; use local refs. "
                 "Journal entry tagged OFFLINE.",
        ),
```

And in the `wt_mgr.spawn(...)` call (~line 364), add `offline=offline`:

```python
        result = wt_mgr.spawn(
            description, repos=repo_list, skip_setup=skip_setup, slug=slug,
            workspace_root=container.config_path().parent,
            offline=offline,
        )
```

After the `_log_bypass` for-loop (~line 373), add an OFFLINE journal entry:

```python
        if offline:
            container.log_manager().append(task.slug, "OFFLINE: passive fetches skipped")
```

- [ ] **Step 4: Run test, expect PASS**

```
uv run pytest tests/cli/test_worktree.py::test_spawn_cli_passes_offline_flag -v
```

- [ ] **Step 5: Run full suite**

```
uv run pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/worktree.py tests/cli/test_worktree.py
git commit -m "feat(spawn): --offline flag for passive worktrees"
mship journal "phase 3.3: --offline CLI flag wired" --action committed
```

---

## Phase 4 — Pre-commit hook refuses passive worktrees

### Task 4.1: Update `_check-commit` to refuse passive

**Files:**
- Modify: `src/mship/cli/internal.py:8-127`
- Test: `tests/cli/test_internal.py`, `tests/test_hook_integration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cli/test_internal.py`:

```python
def test_check_commit_refuses_passive_worktree(tmp_path, monkeypatch):
    """A commit attempted in a registered-but-passive worktree is rejected."""
    from datetime import datetime, timezone
    from mship.core.state import StateManager, Task, WorkspaceState
    from typer.testing import CliRunner
    from mship.cli import app, container

    # Workspace skeleton
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    passive_wt = tmp_path / ".worktrees" / "x" / "shared"
    passive_wt.mkdir(parents=True)
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": passive_wt},
            passive_repos={"shared"},
        )
    }))

    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    monkeypatch.chdir(passive_wt)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["_check-commit", str(passive_wt)])
        assert result.exit_code == 1
        assert "passive worktree" in (result.output or "").lower()
        assert "shared" in (result.output or "")
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
```

- [ ] **Step 2: Run test, expect FAIL**

```
uv run pytest tests/cli/test_internal.py::test_check_commit_refuses_passive_worktree -v
```

Expected: FAIL — current `_check-commit` allows the commit because the path matches a registered worktree.

- [ ] **Step 3: Update `_check-commit`**

In `src/mship/cli/internal.py`, locate the block where `matched_task` is set (around line 40-46). After `matched_task = state.tasks[slug]; break` and before the reconcile gate, add the passive check.

Find this block (~line 40-44):

```python
        matched_task = None
        for slug, wt in registered:
            if tl == wt:
                matched_task = state.tasks[slug]
                break
```

Change to also capture the matched repo name:

```python
        matched_task = None
        matched_repo: str | None = None
        try:
            registered = [
                (slug, repo, Path(wt).resolve())
                for slug, task in state.tasks.items()
                for repo, wt in task.worktrees.items()
            ]
        except (OSError, RuntimeError):
            raise typer.Exit(code=0)
        for slug, repo, wt in registered:
            if tl == wt:
                matched_task = state.tasks[slug]
                matched_repo = repo
                break
```

(Replace the earlier `registered = [...]` block to include the repo name.)

Then immediately after this match block (just before `if matched_task is not None:`), add:

```python
        if matched_task is not None and matched_repo in matched_task.passive_repos:
            import sys
            sys.stderr.write(
                f"⛔ mship: refusing commit — {tl} is a passive worktree of "
                f"`{matched_repo}` for task `{matched_task.slug}`.\n"
                f"   To edit {matched_repo}, close this task and respawn with "
                f"`--repos {matched_repo},...`\n"
                f"   (or `git commit --no-verify` to override).\n"
            )
            raise typer.Exit(code=1)
```

Update the existing rejection-list build (~line 92) to use the new 3-tuple `registered` shape: change `for slug, wt in registered:` to `for slug, _repo, wt in registered:`.

- [ ] **Step 4: Run tests, expect PASS**

```
uv run pytest tests/cli/test_internal.py -v
```

Expected: new test passes; existing tests in `test_internal.py` still pass.

- [ ] **Step 5: Add an integration test (real git commit through hook)**

Append to `tests/test_hook_integration.py`:

```python
def test_commit_in_passive_worktree_refused(workspace_for_hooks, monkeypatch):
    """End-to-end: spawn with --repos affected; commit attempt in passive worktree refused."""
    import os
    from pathlib import Path
    from mship.core.state import StateManager, Task, WorkspaceState
    from datetime import datetime, timezone

    tmp_path, repo = workspace_for_hooks
    runner.invoke(app, ["init", "--install-hooks"])

    # Manually create a passive worktree by seeding state (simulating a real
    # spawn that materialized 'cli' as passive — we reuse the existing
    # workspace_for_hooks fixture which has a single 'cli' repo).
    state_dir = tmp_path / ".mothership"
    sm = StateManager(state_dir)
    state = sm.load()
    # Create an empty subdirectory that pretends to be a passive worktree;
    # the hook only checks state, not git plumbing.
    passive_wt = tmp_path / ".worktrees" / "x" / "cli"
    passive_wt.mkdir(parents=True)
    state.tasks["x"] = Task(
        slug="x", description="x", phase="plan",
        created_at=datetime.now(timezone.utc),
        affected_repos=["cli"], branch="feat/x",
        worktrees={"cli": passive_wt},
        passive_repos={"cli"},
    )
    sm.save(state)

    # Initialize the passive dir as a worktree of the same git history so
    # `git commit` has a valid context.
    import subprocess
    subprocess.run(["git", "worktree", "add", "--detach", str(passive_wt), "HEAD"],
                   cwd=repo, check=True, capture_output=True)
    # Install the hook into the passive worktree's git dir
    runner.invoke(app, ["init", "--install-hooks"])

    (passive_wt / "p.py").write_text("p\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "p.py"], cwd=passive_wt, check=True, capture_output=True, env=env)
    result = subprocess.run(
        ["git", "commit", "-m", "should refuse"],
        cwd=passive_wt, capture_output=True, text=True, env=env,
    )
    assert result.returncode != 0
    assert "passive worktree" in result.stderr.lower()
```

- [ ] **Step 6: Run test**

```
uv run pytest tests/test_hook_integration.py::test_commit_in_passive_worktree_refused -v
```

- [ ] **Step 7: Run full suite**

```
uv run pytest -q
```

- [ ] **Step 8: Commit**

```bash
git add src/mship/cli/internal.py tests/cli/test_internal.py tests/test_hook_integration.py
git commit -m "feat(hook): pre-commit refuses commits in passive worktrees"
mship journal "phase 4: pre-commit refuses passive worktree commits" --action committed
```

---

## Phase 5 — Audit extensions

Add `passive_drift`, `passive_dirty_worktree`, `passive_fetch_failed` issue codes; surface them in `audit_repos`.

### Task 5.1: Add a passive-aware audit pass

**Files:**
- Modify: `src/mship/core/repo_state.py`
- Test: `tests/core/test_repo_state.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_repo_state.py`:

```python
def test_audit_passive_drift_warns(tmp_path):
    """Passive worktree behind origin/<ref> emits a warn-level passive_drift issue."""
    import subprocess
    from mship.core.repo_state import audit_passive_worktrees
    # Set up: bare repo, clone, push two commits, passive worktree at the first.
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    src = tmp_path / "src"
    subprocess.run(["git", "clone", "-q", str(bare), str(src)],
                   check=True, capture_output=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "c1"], cwd=src,
                   check=True, capture_output=True, env={**__import__("os").environ, **env})
    sha1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src,
                          check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "c2"], cwd=src,
                   check=True, capture_output=True, env={**__import__("os").environ, **env})
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)
    # Make a worktree of `src` at the OLD sha1 — drift exists relative to origin/main
    passive = tmp_path / "passive"
    subprocess.run(["git", "worktree", "add", "--detach", str(passive), sha1],
                   cwd=src, check=True, capture_output=True)
    issues = audit_passive_worktrees(
        passive_paths={"shared": passive},
        ref_per_repo={"shared": "main"},
        canonical_paths={"shared": src},
    )
    codes = [i.code for i in issues["shared"]]
    assert "passive_drift" in codes


def test_audit_passive_fetch_failed_errors(tmp_path):
    """Fetch failure produces an error-level passive_fetch_failed issue."""
    import subprocess
    from mship.core.repo_state import audit_passive_worktrees
    # Repo with no remote — fetch will fail.
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)],
                   check=True, capture_output=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "c"], cwd=src,
                   check=True, capture_output=True, env={**__import__("os").environ, **env})
    passive = tmp_path / "passive"
    subprocess.run(["git", "worktree", "add", "--detach", str(passive), "main"],
                   cwd=src, check=True, capture_output=True)
    issues = audit_passive_worktrees(
        passive_paths={"shared": passive},
        ref_per_repo={"shared": "main"},
        canonical_paths={"shared": src},
    )
    codes = [(i.code, i.severity) for i in issues["shared"]]
    assert ("passive_fetch_failed", "error") in codes
```

- [ ] **Step 2: Run tests, expect FAIL**

```
uv run pytest tests/core/test_repo_state.py::test_audit_passive_drift_warns tests/core/test_repo_state.py::test_audit_passive_fetch_failed_errors -v
```

Expected: FAIL — `audit_passive_worktrees` doesn't exist.

- [ ] **Step 3: Implement `audit_passive_worktrees`**

In `src/mship/core/repo_state.py`, add at the end of the file:

```python
def audit_passive_worktrees(
    passive_paths: dict[str, Path],
    ref_per_repo: dict[str, str],
    canonical_paths: dict[str, Path],
) -> dict[str, list[Issue]]:
    """Audit passive worktrees for drift / dirtiness / fetch failure.

    For each passive repo:
      - Run `git fetch origin <ref>` against the canonical checkout. If it fails,
        emit `passive_fetch_failed` (error). Skip remaining checks.
      - Compare `<passive>/HEAD` against `<canonical>/origin/<ref>`. If they
        differ, emit `passive_drift` (warn).
      - Run `git status --porcelain` in the passive worktree. If non-empty
        after filtering untracked, emit `passive_dirty_worktree` (warn).

    Returns a per-repo list of issues (empty list if clean).
    """
    import subprocess
    out: dict[str, list[Issue]] = {}
    for name, passive in passive_paths.items():
        issues: list[Issue] = []
        ref = ref_per_repo.get(name)
        canonical = canonical_paths.get(name)
        if ref is None or canonical is None:
            out[name] = issues
            continue

        fetch = subprocess.run(
            ["git", "fetch", "origin", ref],
            cwd=canonical, capture_output=True, text=True, check=False, timeout=60,
        )
        if fetch.returncode != 0:
            issues.append(Issue(
                "passive_fetch_failed", "error",
                f"git fetch origin {ref} failed: {fetch.stderr.strip()[:160]}",
            ))
            out[name] = issues
            continue

        rev_passive = subprocess.run(
            ["git", "-C", str(passive), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
        rev_origin = subprocess.run(
            ["git", "-C", str(canonical), "rev-parse", f"origin/{ref}"],
            capture_output=True, text=True, check=False,
        )
        if (rev_passive.returncode == 0 and rev_origin.returncode == 0
                and rev_passive.stdout.strip() != rev_origin.stdout.strip()):
            issues.append(Issue(
                "passive_drift", "warn",
                f"passive HEAD {rev_passive.stdout.strip()[:8]} drifted "
                f"from origin/{ref} ({rev_origin.stdout.strip()[:8]})",
            ))

        status = subprocess.run(
            ["git", "-C", str(passive), "status", "--porcelain"],
            capture_output=True, text=True, check=False,
        )
        if status.returncode == 0:
            modified = sum(
                1 for line in status.stdout.splitlines()
                if line.strip() and not line.startswith("??")
            )
            if modified:
                issues.append(Issue(
                    "passive_dirty_worktree", "warn",
                    f"{modified} modified file(s) in passive worktree",
                ))
        out[name] = issues
    return out
```

- [ ] **Step 4: Run tests, expect PASS**

```
uv run pytest tests/core/test_repo_state.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/repo_state.py tests/core/test_repo_state.py
git commit -m "feat(audit): passive_drift, passive_fetch_failed, passive_dirty_worktree"
mship journal "phase 5: passive audit issue codes" --action committed
```

### Task 5.2: Wire passive audit into `mship audit` CLI

**Files:**
- Modify: `src/mship/cli/audit.py`
- Test: `tests/cli/` — add a smoke test that `mship audit` lists passive issues

- [ ] **Step 1: Write the failing test**

Examine `src/mship/cli/audit.py` for the existing structure. Find where it builds the report and prints. Add an integration test in `tests/cli/test_audit.py` (create file if absent):

```python
def test_audit_includes_passive_repos(tmp_path, monkeypatch):
    """`mship audit` output includes passive repos with their drift/fetch issues."""
    import subprocess
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    bare = tmp_path / "shared.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    src = tmp_path / "shared"
    subprocess.run(["git", "clone", "-q", str(bare), str(src)],
                   check=True, capture_output=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    import os
    full_env = {**os.environ, **env}
    subprocess.run(["git", "-c", f"user.email={env['GIT_AUTHOR_EMAIL']}",
                    "-c", f"user.name={env['GIT_AUTHOR_NAME']}",
                    "commit", "--allow-empty", "-qm", "init"],
                   cwd=src, check=True, capture_output=True, env=full_env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=src, check=True, capture_output=True, env=full_env)
    subprocess.run(["git", "-c", f"user.email={env['GIT_AUTHOR_EMAIL']}",
                    "-c", f"user.name={env['GIT_AUTHOR_NAME']}",
                    "commit", "-qm", "add taskfile"],
                   cwd=src, check=True, capture_output=True, env=full_env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
        "    base_branch: main\n    expected_branch: main\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()

    # Passive worktree at an older sha so origin/main has advanced — but here both refs are at HEAD; the audit is just smoke.
    passive = tmp_path / ".worktrees" / "x" / "shared"
    subprocess.run(["git", "worktree", "add", "--detach", str(passive), "HEAD"],
                   cwd=src, check=True, capture_output=True)

    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": passive},
            passive_repos={"shared"},
        )
    }))

    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["audit", "--json"])
        assert result.exit_code == 0, result.output
        # The report should mention shared as a passive entry (regardless of issues).
        import json
        report = json.loads(result.stdout)
        names = [r["name"] for r in report["repos"]]
        assert "shared" in names
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
```

- [ ] **Step 2: Run test, expect FAIL or PASS depending on existing audit behavior**

```
uv run pytest tests/cli/test_audit.py -v
```

If it passes (audit already reports `shared` because it's in config.repos), good — we're confirming no regression. If it fails (audit doesn't see passive repos at all), proceed to step 3.

- [ ] **Step 3: Wire passive results into audit CLI**

In `src/mship/cli/audit.py`, after the existing `audit_repos(...)` call, also call `audit_passive_worktrees` for passive repos found in state.yaml. Merge the per-repo issue lists into the existing `RepoAudit` entries (or append a new `RepoAudit` for purely-passive repos not in `audit_names`).

The exact code depends on the current structure of `cli/audit.py`. Read it first; then for each task in state, for each `passive_repo`, build a `passive_paths`, `ref_per_repo`, `canonical_paths` dict and call `audit_passive_worktrees`. Append issues to the matching `RepoAudit` (rebuilding the tuple).

- [ ] **Step 4: Run test, expect PASS**

```
uv run pytest tests/cli/test_audit.py -v && uv run pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/audit.py tests/cli/test_audit.py
git commit -m "feat(audit): surface passive worktree issues in audit output"
mship journal "phase 5.2: audit CLI surfaces passive issues" --action committed
```

---

## Phase 6 — Sync refresh for passive worktrees

### Task 6.1: Add passive refresh to sync

**Files:**
- Modify: `src/mship/core/repo_sync.py`, `src/mship/cli/sync.py`
- Test: `tests/cli/test_sync.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/cli/test_sync.py`:

```python
def test_sync_refreshes_passive_worktree(tmp_path, monkeypatch):
    """`mship sync` re-fetches and resets passive worktrees to origin/<ref>."""
    import subprocess
    import os
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    full_env = {**os.environ, **env}

    bare = tmp_path / "shared.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    src = tmp_path / "shared"
    subprocess.run(["git", "clone", "-q", str(bare), str(src)],
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "c1"],
                   cwd=src, check=True, capture_output=True, env=full_env)
    sha1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src,
                          check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True, env=full_env)
    subprocess.run(["git", "commit", "-qm", "c2"], cwd=src, check=True, capture_output=True, env=full_env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=src,
                   check=True, capture_output=True)
    sha2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src,
                          check=True, capture_output=True, text=True).stdout.strip()

    # Passive worktree at OLD sha1
    passive = tmp_path / ".worktrees" / "x" / "shared"
    subprocess.run(["git", "worktree", "add", "--detach", str(passive), sha1],
                   cwd=src, check=True, capture_output=True)

    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
        "    base_branch: main\n    expected_branch: main\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": passive},
            passive_repos={"shared"},
        )
    }))

    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["sync"])
        assert result.exit_code in (0, 1), result.output  # 1 acceptable if canonical has issues
        head_after = subprocess.run(
            ["git", "-C", str(passive), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head_after == sha2, "passive worktree should be reset to origin/main HEAD"
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()


def test_sync_no_passive_skips_passive_refresh(tmp_path, monkeypatch):
    """`mship sync --no-passive` leaves passive worktrees alone."""
    # Same setup as above, but pass --no-passive and assert HEAD unchanged.
    # (Skipped here for brevity — copy the setup, change runner.invoke args
    # to ["sync", "--no-passive"], and assert head_after == sha1.)
    pass
```

- [ ] **Step 2: Run test, expect FAIL**

```
uv run pytest tests/cli/test_sync.py::test_sync_refreshes_passive_worktree -v
```

- [ ] **Step 3: Add `refresh_passive_worktrees` to `repo_sync.py`**

In `src/mship/core/repo_sync.py`, add at the end:

```python
def refresh_passive_worktrees(
    state_manager,
    config: WorkspaceConfig,
) -> list["SyncResult"]:
    """Re-fetch and reset each passive worktree to `origin/<expected || base>`.

    Safe by construction: passive worktrees are detached HEAD, mship-managed,
    and pre-commit-hook-protected. Nothing the user could lose by hard reset.
    Returns a SyncResult per (task, repo) tuple for reporting.
    """
    import subprocess
    state = state_manager.load()
    results: list[SyncResult] = []
    for task in state.tasks.values():
        for repo_name in task.passive_repos:
            wt = task.worktrees.get(repo_name)
            if wt is None or not Path(wt).exists():
                continue
            repo_cfg = config.repos.get(repo_name)
            if repo_cfg is None:
                continue
            ref = repo_cfg.expected_branch or repo_cfg.base_branch
            if ref is None:
                results.append(SyncResult(
                    name=f"{task.slug}/{repo_name}", status="skipped",
                    message="no expected_branch or base_branch declared",
                ))
                continue
            canonical = repo_cfg.path
            fetch = subprocess.run(
                ["git", "fetch", "origin", ref], cwd=canonical,
                capture_output=True, text=True, check=False, timeout=60,
            )
            if fetch.returncode != 0:
                results.append(SyncResult(
                    name=f"{task.slug}/{repo_name}", status="skipped",
                    message=f"fetch failed: {fetch.stderr.strip()[:160]}",
                ))
                continue
            reset = subprocess.run(
                ["git", "-C", str(wt), "reset", "--hard", f"origin/{ref}"],
                capture_output=True, text=True, check=False,
            )
            if reset.returncode == 0:
                results.append(SyncResult(
                    name=f"{task.slug}/{repo_name}", status="fast_forwarded",
                    message=f"reset to origin/{ref}",
                ))
            else:
                results.append(SyncResult(
                    name=f"{task.slug}/{repo_name}", status="skipped",
                    message=f"reset failed: {reset.stderr.strip()[:160]}",
                ))
    return results
```

- [ ] **Step 4: Wire `--no-passive` into `cli/sync.py`**

Replace the `def sync(...)` parameters:

```python
    def sync(
        repos: Optional[str] = typer.Option(None, "--repos", help="Comma-separated repo names"),
        no_passive: bool = typer.Option(
            False, "--no-passive",
            help="Skip refreshing passive worktrees (default: include).",
        ),
    ):
```

After the existing sync results loop (after `output.print(...)` block), add:

```python
        if not no_passive:
            from mship.core.repo_sync import refresh_passive_worktrees
            passive_results = refresh_passive_worktrees(
                container.state_manager(), config,
            )
            for r in passive_results:
                if r.status == "fast_forwarded":
                    output.print(f"  [green]{r.name}[/green]: {r.message}")
                else:
                    output.print(f"  [yellow]{r.name}[/yellow]: skipped ({r.message})")
```

- [ ] **Step 5: Run tests, expect PASS**

```
uv run pytest tests/cli/test_sync.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/mship/core/repo_sync.py src/mship/cli/sync.py tests/cli/test_sync.py
git commit -m "feat(sync): refresh passive worktrees, --no-passive opt-out"
mship journal "phase 6: sync refreshes passive worktrees" --action committed
```

---

## Phase 7 — Switch / phase / test guards for passive

### Task 7.1: Switch warns on passive

**Files:**
- Modify: `src/mship/cli/switch.py`
- Test: `tests/cli/test_switch.py`

- [ ] **Step 1: Read existing switch CLI**

```bash
cat src/mship/cli/switch.py
```

Identify where the handoff is rendered and where `active_repo` is set on the task.

- [ ] **Step 2: Write the failing test**

Append to `tests/cli/test_switch.py`:

```python
def test_switch_to_passive_warns(tmp_path, monkeypatch):
    """`mship switch <passive-repo>` succeeds but prints a passive warning."""
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n"
        "  api:\n    path: ./api\n    type: service\n    base_branch: main\n    expected_branch: main\n"
        "  shared:\n    path: ./shared\n    type: library\n    base_branch: main\n    expected_branch: main\n"
    )
    for n in ("api", "shared"):
        d = tmp_path / n
        d.mkdir()
        (d / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
        import subprocess
        subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    api_wt = tmp_path / ".worktrees" / "x" / "api"
    shared_wt = tmp_path / ".worktrees" / "x" / "shared"
    api_wt.mkdir(parents=True); shared_wt.mkdir(parents=True)
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["api"], branch="feat/x",
            worktrees={"api": api_wt, "shared": shared_wt},
            passive_repos={"shared"},
        )
    }))
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    monkeypatch.chdir(api_wt)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["switch", "shared"])
        assert result.exit_code == 0, result.output
        assert "passive" in result.output.lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
```

- [ ] **Step 3: Run test, expect FAIL**

```
uv run pytest tests/cli/test_switch.py::test_switch_to_passive_warns -v
```

- [ ] **Step 4: Add the warning in `cli/switch.py`**

After the switch operation succeeds and the handoff is built, check `repo_name in task.passive_repos`. If true, print a yellow warning before the normal handoff output:

```python
        if repo_name in t.passive_repos:
            output.print(
                f"[yellow]⚠[/yellow] Switched to `{repo_name}` (passive — read-only on "
                f"`{config.repos[repo_name].expected_branch or config.repos[repo_name].base_branch}`).\n"
                f"  To edit, close this task and respawn with `--repos {repo_name},...`"
            )
```

(Adapt variable names to match the local context in `switch.py`.)

- [ ] **Step 5: Run test, expect PASS; run full suite**

```
uv run pytest tests/cli/test_switch.py -v && uv run pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/switch.py tests/cli/test_switch.py
git commit -m "feat(switch): warn when target repo is passive"
mship journal "phase 7.1: switch to passive warns" --action committed
```

### Task 7.2: phase + test refuse passive active_repo

**Files:**
- Modify: `src/mship/cli/phase.py`, `src/mship/cli/exec.py` (the `test` command)
- Test: extend `tests/cli/test_phase.py` and `tests/cli/test_exec.py`

- [ ] **Step 1: Write failing tests for both**

Append to `tests/cli/test_phase.py`:

```python
def test_phase_refuses_when_active_repo_is_passive(tmp_path, monkeypatch):
    """`mship phase dev` errors if active_repo is passive."""
    from datetime import datetime, timezone
    from typer.testing import CliRunner
    from mship.cli import app, container
    from mship.core.state import StateManager, Task, WorkspaceState

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    StateManager(state_dir).save(WorkspaceState(tasks={
        "x": Task(
            slug="x", description="x", phase="plan",
            created_at=datetime.now(timezone.utc),
            affected_repos=["shared"], branch="feat/x",
            worktrees={"shared": tmp_path / "wt"},
            passive_repos={"shared"},
            active_repo="shared",
        )
    }))
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["phase", "dev"])
        assert result.exit_code != 0
        assert "passive" in (result.output or "").lower()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
```

Append a similar test to `tests/cli/test_exec.py` (or wherever `mship test` is exercised) with the same shape, asserting `mship test` errors out.

- [ ] **Step 2: Run tests, expect FAIL**

```
uv run pytest tests/cli/test_phase.py::test_phase_refuses_when_active_repo_is_passive -v
```

- [ ] **Step 3: Add the guards**

In `src/mship/cli/phase.py`, near the top of the command body (before the soft-gate checks), add:

```python
        if t.active_repo and t.active_repo in t.passive_repos:
            output.error(
                f"Cannot transition phase: active_repo '{t.active_repo}' is passive. "
                f"Switch to an affected repo first, or close & respawn with "
                f"`--repos {t.active_repo},...` to make it editable."
            )
            raise typer.Exit(code=1)
```

Repeat the equivalent guard in `src/mship/cli/exec.py` for `mship test`. (Find the test command body; add the same check before invoking the test runner.)

- [ ] **Step 4: Run tests, expect PASS; run full suite**

```
uv run pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mship/cli/phase.py src/mship/cli/exec.py tests/cli/test_phase.py tests/cli/test_exec.py
git commit -m "feat(phase,test): refuse when active_repo is passive"
mship journal "phase 7.2: phase/test refuse passive active_repo" --action committed
```

---

## Phase 8 — Prune + doctor + miscellaneous

### Task 8.1: Prune scans hub layout

**Files:**
- Modify: `src/mship/core/prune.py:30-65`
- Test: `tests/core/test_prune.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_prune.py`:

```python
def test_prune_detects_hub_layout_orphan(tmp_path):
    """An orphan dir under <workspace>/.worktrees/<slug>/<repo>/ is detected."""
    import subprocess
    from mship.core.config import ConfigLoader
    from mship.core.state import StateManager
    from mship.core.prune import PruneManager
    from mship.util.git import GitRunner
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  shared:\n    path: ./shared\n    type: library\n"
    )
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "init", "-q", str(tmp_path / "shared")],
                   check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"],
                   cwd=tmp_path / "shared", check=True, capture_output=True)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    sm = StateManager(state_dir)
    config = ConfigLoader.load(tmp_path / "mothership.yaml")
    # Seed an orphan: hub-style worktree with no state entry
    orphan = tmp_path / ".worktrees" / "stale-task" / "shared"
    subprocess.run(["git", "worktree", "add", str(orphan), "-b", "feat/stale"],
                   cwd=tmp_path / "shared", check=True, capture_output=True)
    pm = PruneManager(config, sm, GitRunner())
    orphans = pm.scan()
    paths = {str(o.path.resolve()) for o in orphans}
    assert str(orphan.resolve()) in paths
```

- [ ] **Step 2: Run test, expect FAIL**

```
uv run pytest tests/core/test_prune.py::test_prune_detects_hub_layout_orphan -v
```

Expected: FAIL — current `scan` only walks `<repo>/.worktrees/`, not the workspace hub.

- [ ] **Step 3: Extend `PruneManager.scan`**

In `src/mship/core/prune.py`, in `scan()`, after the existing per-repo `<repo>/.worktrees` walk, add a workspace-root walk:

```python
        # Hub layout: scan <workspace>/.worktrees/<slug>/<repo>/
        # Workspace root is the parent dir of mothership.yaml — not stored on
        # WorkspaceConfig, so derive from any repo's path.parent.
        candidates: set[Path] = set()
        for repo_cfg in self._config.repos.values():
            candidates.add(repo_cfg.path.parent)
        for ws_root in candidates:
            hub_root = ws_root / ".worktrees"
            if not hub_root.is_dir():
                continue
            for slug_dir in hub_root.iterdir():
                if not slug_dir.is_dir():
                    continue
                for repo_dir in slug_dir.iterdir():
                    if not repo_dir.is_dir():
                        continue
                    if not (repo_dir / ".git").exists():
                        continue
                    resolved = str(repo_dir.resolve())
                    if resolved in tracked_paths:
                        continue
                    # Find the matching repo by path.parent matching ws_root
                    matched_repo = None
                    for name, rc in self._config.repos.items():
                        if rc.path.parent.resolve() == ws_root.resolve():
                            # Heuristic: orphan in hub, attribute to a repo
                            # so worktree_remove gets called against the right
                            # canonical checkout. If multiple repos share a
                            # workspace, attribute by directory name.
                            if rc.path.name == repo_dir.name:
                                matched_repo = name
                                break
                    if matched_repo is None:
                        # Unknown repo dir under hub — best-effort, skip
                        continue
                    orphans.append(OrphanedWorktree(
                        repo=matched_repo,
                        path=repo_dir,
                        reason="not_in_state",
                    ))
```

- [ ] **Step 4: Run test, expect PASS**

```
uv run pytest tests/core/test_prune.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/prune.py tests/core/test_prune.py
git commit -m "feat(prune): scan hub-layout worktrees alongside legacy per-repo"
mship journal "phase 8.1: prune covers hub layout" --action committed
```

### Task 8.2: Doctor checks workspace `.gitignore`

**Files:**
- Modify: `src/mship/core/doctor.py`
- Test: `tests/core/test_doctor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_doctor.py`:

```python
def test_doctor_warns_when_workspace_gitignore_missing_worktrees(tmp_path):
    """If workspace root is a git repo and .gitignore lacks `.worktrees`, doctor warns."""
    import subprocess
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.core.state import StateManager
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\nrepos:\n  a:\n    path: ./a\n    type: service\n"
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    config = ConfigLoader.load(tmp_path / "mothership.yaml")
    checker = DoctorChecker(config, StateManager(state_dir), workspace_root=tmp_path)
    report = checker.run()
    relevant = [c for c in report.checks if "worktrees" in c.message.lower()]
    assert any(c.severity == "warn" for c in relevant), [c.message for c in report.checks]
```

(Confirm `DoctorChecker.__init__` signature; if it doesn't take `workspace_root`, adapt the test or extend the constructor.)

- [ ] **Step 2: Run test, expect FAIL**

```
uv run pytest tests/core/test_doctor.py::test_doctor_warns_when_workspace_gitignore_missing_worktrees -v
```

- [ ] **Step 3: Add the check in `DoctorChecker.run`**

In `src/mship/core/doctor.py`, inside `DoctorChecker.run`, append a check:

```python
        # Workspace .gitignore should include .worktrees if root is a git repo
        ws = self._workspace_root  # adapt to wherever workspace root lives on the checker
        if ws and (ws / ".git").exists():
            gi = ws / ".gitignore"
            entries = gi.read_text().splitlines() if gi.exists() else []
            if ".worktrees" not in entries:
                checks.append(CheckResult(
                    severity="warn",
                    message=f"workspace .gitignore missing `.worktrees` entry (will be added on next spawn)",
                ))
```

If `DoctorChecker` doesn't currently know workspace_root, plumb it through (constructor parameter, set from container).

- [ ] **Step 4: Run test, expect PASS**

```
uv run pytest tests/core/test_doctor.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/doctor.py tests/core/test_doctor.py
git commit -m "feat(doctor): check workspace .gitignore for .worktrees"
mship journal "phase 8.2: doctor checks workspace gitignore" --action committed
```

### Task 8.3: Final integration — full hub-layout end-to-end test

**Files:**
- Test: `tests/test_integration.py`

- [ ] **Step 1: Write the integration test**

Append to `tests/test_integration.py`:

```python
def test_full_hub_layout_e2e(tmp_path, monkeypatch):
    """Spawn → commit in affected → audit → close — under hub layout, with passive."""
    import os, subprocess
    from typer.testing import CliRunner
    from mship.cli import app, container

    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    # Two repos: api depends on shared
    for n in ("api", "shared"):
        d = tmp_path / n
        d.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(d)],
                       check=True, capture_output=True)
        (d / "Taskfile.yml").write_text("version: '3'\ntasks:\n  setup:\n    cmds:\n      - echo ok\n")
        (d / "README.md").write_text(n)
        subprocess.run(["git", "add", "."], cwd=d, check=True, capture_output=True, env=env)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True, capture_output=True, env=env)

    (tmp_path / "mothership.yaml").write_text(
        "workspace: e2e\nrepos:\n"
        "  api:\n    path: ./api\n    type: service\n    base_branch: main\n    expected_branch: main\n    depends_on: [shared]\n"
        "  shared:\n    path: ./shared\n    type: library\n    base_branch: main\n    expected_branch: main\n"
    )
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    monkeypatch.chdir(tmp_path)
    try:
        runner = CliRunner()
        # Spawn affected=api, expect shared to be passive
        result = runner.invoke(app, ["spawn", "feature", "--repos", "api",
                                     "--skip-setup", "--force-audit", "--offline"])
        assert result.exit_code == 0, result.output

        from mship.core.state import StateManager
        state = StateManager(tmp_path / ".mothership").load()
        task = state.tasks["feature"]
        # Hub layout: both worktrees as siblings under <workspace>/.worktrees/feature/
        hub = tmp_path / ".worktrees" / "feature"
        assert task.worktrees["api"] == hub / "api"
        assert task.worktrees["shared"] == hub / "shared"
        # Passive set
        assert task.passive_repos == {"shared"}
        # Sibling resolution (the win case)
        from_api = task.worktrees["api"] / ".." / "shared"
        assert from_api.resolve() == task.worktrees["shared"].resolve()
        # Single .mship-workspace marker
        assert (hub / ".mship-workspace").is_file()
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
```

- [ ] **Step 2: Run test, expect PASS**

```
uv run pytest tests/test_integration.py::test_full_hub_layout_e2e -v
```

- [ ] **Step 3: Run full suite**

```
uv run pytest -q
```

Expected: 0 failures.

- [ ] **Step 4: Commit + version bump**

Bump `pyproject.toml` version `0.1.1 → 0.2.0`:

```bash
sed -i 's/^version = "0.1.1"$/version = "0.2.0"/' pyproject.toml
grep '^version' pyproject.toml  # confirm 0.2.0
```

```bash
git add tests/test_integration.py pyproject.toml
git commit -m "chore: bump to 0.2.0; add full hub-layout e2e test"
mship journal "phase 8.3: e2e green; version bumped to 0.2.0" --action committed --test-state pass
```

---

## Done

After all phases:
1. Run `mship test` to record final test pass.
2. Run `mship phase review` (then `run` if applicable).
3. Run `mship finish --body-file docs/superpowers/specs/2026-04-28-hub-worktree-layout-design.md` (or write a dedicated PR body).
4. Note: each phase is a natural PR. Either bundle (one big PR with the full feature) or split (8 PRs, merged in order). For agent-driven execution, bundling reduces churn — but ask the human reviewer.
