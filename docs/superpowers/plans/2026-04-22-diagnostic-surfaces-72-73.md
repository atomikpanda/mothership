# Diagnostic Surfaces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two narrow diagnostic improvements: spawn/doctor warn when a `symlink_dirs` target is ignored as a directory but the symlink itself slips through (#72); `mship close` names the reason behind a "pr state unknown" result instead of swallowing it (#73).

**Architecture:** Add one helper in `core/worktree.py` that runs two `git check-ignore` probes to detect the footgun; call it from `_create_symlinks` (spawn path) and `DoctorChecker.run` (doctor path). Separately, change `PRManager.check_pr_state` to return a `PrStateResult` NamedTuple with `state` + `reason`; classify the reason via substring matching on gh stderr; update `close`'s single caller and its log message.

**Tech Stack:** Python 3.14 (NamedTuple), pytest, shell via ShellRunner for git + gh.

**Reference spec:** `docs/superpowers/specs/2026-04-22-diagnostic-surfaces-72-73-design.md`

---

## File structure

**Modified files:**
- `src/mship/core/worktree.py` — add `_symlink_gitignore_footgun` helper; call it from `_create_symlinks`.
- `src/mship/core/doctor.py` — new per-repo loop checking `symlink_dirs` entries via the same helper.
- `src/mship/core/pr.py` — `check_pr_state` returns `PrStateResult`; new classification helper `_classify_pr_state_reason`.
- `src/mship/cli/worktree.py` — `close` unpacks the new return; log message includes reason on unknown.
- `tests/core/test_worktree.py` — helper truth-table tests + spawn integration test.
- `tests/core/test_doctor.py` — doctor-row integration test.
- `tests/core/test_pr.py` — update 5 existing `check_pr_state_*` tests; add classification tests.
- `tests/cli/test_worktree.py` — close integration test (rate-limit reason surfaces).

**No new files.** Each change fits naturally in the existing module.

**Task ordering rationale:** Task 1 (helper + spawn) is foundational — Task 2's doctor integration reuses the same helper. Task 3 (#73) is independent of the first two and could be done in parallel, but sequencing keeps commits clean. Task 4 is smoke + PR.

---

## Task 1: `_symlink_gitignore_footgun` helper + spawn integration

**Files:**
- Modify: `src/mship/core/worktree.py` — add helper; call from `_create_symlinks`.
- Modify: `tests/core/test_worktree.py` — 4 truth-table tests + 1 spawn-path regression test.

**Context:** The helper detects the narrow case where `.gitignore` has `name/` (dir form) but not `name` — so git ignores the directory but treats the symlink (which git sees as a file) as untracked. `git check-ignore <path>` returns 0 when ignored, 1 when not, >1 on error. Probe both `name/` and `name` and compare.

- [ ] **Step 1.1: Write failing truth-table tests**

Append to `tests/core/test_worktree.py`:

```python
import subprocess


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def test_footgun_fires_when_only_dir_form_ignored(tmp_path: Path):
    """`.gitignore` has `foo/` but not `foo` → footgun. See #72."""
    from mship.core.worktree import _symlink_gitignore_footgun
    repo = tmp_path / "r"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("foo/\n")
    assert _symlink_gitignore_footgun(repo, "foo") is True


def test_footgun_silent_when_plain_name_ignored(tmp_path: Path):
    """`.gitignore` has `foo` (no slash) → matches both; no footgun."""
    from mship.core.worktree import _symlink_gitignore_footgun
    repo = tmp_path / "r"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("foo\n")
    assert _symlink_gitignore_footgun(repo, "foo") is False


def test_footgun_silent_when_both_forms_ignored(tmp_path: Path):
    from mship.core.worktree import _symlink_gitignore_footgun
    repo = tmp_path / "r"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("foo\nfoo/\n")
    assert _symlink_gitignore_footgun(repo, "foo") is False


def test_footgun_silent_when_neither_form_ignored(tmp_path: Path):
    """Legitimate tracked symlink case — no warning."""
    from mship.core.worktree import _symlink_gitignore_footgun
    repo = tmp_path / "r"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text("unrelated\n")
    assert _symlink_gitignore_footgun(repo, "foo") is False
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_worktree.py -v -k footgun`
Expected: FAIL — `ImportError: cannot import name '_symlink_gitignore_footgun'`.

- [ ] **Step 1.3: Add the helper**

Edit `src/mship/core/worktree.py`. Add this module-level helper right above the `_create_symlinks` method (which is currently around line 136). Note the helper uses `subprocess` directly (not the ShellRunner dep-injected elsewhere in this module) — matches existing style in this file's `_copy_bind_files` / `_create_symlinks`, which also do direct filesystem calls without the runner.

```python
def _symlink_gitignore_footgun(repo_path: Path, name: str) -> bool:
    """Return True when `.gitignore` ignores `<name>/` (dir form) but not `<name>` alone.

    This is the specific footgun that breaks `symlink_dirs`: git treats the
    symlink as a file, not a directory, so a dir-only ignore pattern
    (`foo/`) doesn't match the symlink (`foo`), and it shows up as untracked.

    Probes via `git check-ignore` — exit 0 = ignored, 1 = not ignored, >1 = error.
    On any error we bail to False (no warning) to avoid false positives.
    """
    import subprocess

    def _ignored(path_fragment: str) -> bool:
        try:
            r = subprocess.run(
                ["git", "check-ignore", "--", path_fragment],
                cwd=repo_path, capture_output=True, text=True, check=False,
            )
        except OSError:
            return False
        return r.returncode == 0

    dir_ignored = _ignored(f"{name}/")
    file_ignored = _ignored(name)
    return dir_ignored and not file_ignored
```

Place the helper at module scope (not inside `WorktreeManager`) so tests can import it directly as shown in the tests above.

- [ ] **Step 1.4: Run helper tests to verify they pass**

Run: `uv run pytest tests/core/test_worktree.py -v -k footgun`
Expected: 4 passed.

- [ ] **Step 1.5: Wire into `_create_symlinks`**

Still in `src/mship/core/worktree.py`, update `_create_symlinks` (currently around lines 136-174). Find the block:

```python
            if target.is_symlink():
                target.unlink()

            target.symlink_to(source.resolve())
```

Replace with:

```python
            if target.is_symlink():
                target.unlink()

            target.symlink_to(source.resolve())

            # Detect the `.gitignore has 'foo/' but not 'foo'` footgun — the
            # symlink would show as untracked. See #72.
            if _symlink_gitignore_footgun(worktree_path, dir_name):
                warnings.append(
                    f"{repo_name}: symlink '{dir_name}' is not ignored — "
                    f"git treats it as an untracked file. "
                    f"Add '{dir_name}' (not just '{dir_name}/') to .gitignore."
                )
```

The check runs inside the worktree (where the symlink lives), not the source repo — the worktree shares `.git` with the parent, so `.gitignore` patterns apply uniformly.

- [ ] **Step 1.6: Write failing integration test for spawn**

Append to `tests/core/test_worktree.py`:

```python
def test_create_symlinks_warns_on_dir_form_gitignore_footgun(tmp_path: Path):
    """Spawn path: `.gitignore` has `foo/` and `symlink_dirs: [foo]` → warning. See #72."""
    from mship.core.config import RepoConfig, WorkspaceConfig
    from mship.core.state import StateManager
    from mship.core.worktree import WorktreeManager
    from mship.core.graph import RepoGraph
    from mship.util.shell import ShellRunner
    from unittest.mock import MagicMock

    # Source repo with `foo/` directory + `.gitignore` ignoring `foo/` only.
    source = tmp_path / "source"
    _init_git_repo(source)
    (source / "foo").mkdir()
    (source / "foo" / "data.txt").write_text("x")
    (source / ".gitignore").write_text("foo/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=source, check=True)

    repo_cfg = RepoConfig(
        path=source, type="service", symlink_dirs=["foo"],
    )
    cfg = WorkspaceConfig(workspace="t", repos={"src": repo_cfg})
    mgr = WorktreeManager(
        config=cfg,
        state_manager=MagicMock(spec=StateManager),
        shell=MagicMock(spec=ShellRunner),
        graph=RepoGraph(cfg),
    )

    # Call `_create_symlinks` directly so we don't exercise the whole spawn pipeline.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    # Initialize git in the worktree so check-ignore has something to resolve.
    _init_git_repo(worktree)
    (worktree / ".gitignore").write_text("foo/\n")
    warnings = mgr._create_symlinks("src", repo_cfg, worktree)
    assert any("foo" in w and "not ignored" in w for w in warnings), warnings


def test_create_symlinks_no_warn_when_plain_name_ignored(tmp_path: Path):
    """Regression: `.gitignore` with `foo` (no slash) → NO warning."""
    from mship.core.config import RepoConfig, WorkspaceConfig
    from mship.core.state import StateManager
    from mship.core.worktree import WorktreeManager
    from mship.core.graph import RepoGraph
    from mship.util.shell import ShellRunner
    from unittest.mock import MagicMock

    source = tmp_path / "source"
    _init_git_repo(source)
    (source / "foo").mkdir()
    (source / ".gitignore").write_text("foo\n")  # no trailing slash
    subprocess.run(["git", "add", ".gitignore"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=source, check=True)

    repo_cfg = RepoConfig(path=source, type="service", symlink_dirs=["foo"])
    cfg = WorkspaceConfig(workspace="t", repos={"src": repo_cfg})
    mgr = WorktreeManager(
        config=cfg,
        state_manager=MagicMock(spec=StateManager),
        shell=MagicMock(spec=ShellRunner),
        graph=RepoGraph(cfg),
    )

    worktree = tmp_path / "wt"
    worktree.mkdir()
    _init_git_repo(worktree)
    (worktree / ".gitignore").write_text("foo\n")
    warnings = mgr._create_symlinks("src", repo_cfg, worktree)
    assert not any("not ignored" in w for w in warnings), warnings
```

- [ ] **Step 1.7: Run spawn integration tests**

Run: `uv run pytest tests/core/test_worktree.py -v -k "symlinks_warns or symlinks_no_warn or footgun"`
Expected: 6 passed (4 unit + 2 spawn).

- [ ] **Step 1.8: Run the broader worktree tests for regressions**

Run: `uv run pytest tests/core/test_worktree.py -q 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 1.9: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat(worktree): warn when symlink_dir is ignored as dir but not as file"
mship journal "#72: _symlink_gitignore_footgun helper + spawn-path warning when .gitignore has 'foo/' but not 'foo'" --action committed
```

---

## Task 2: Doctor integration for #72

**Files:**
- Modify: `src/mship/core/doctor.py` — new per-repo check after the existing pre-commit-hook block.
- Modify: `tests/core/test_doctor.py` — integration test.

**Context:** Doctor already iterates `self._config.repos.items()` and emits per-repo `CheckResult` rows. Add a new loop (or extend the existing one) that consults the same helper from Task 1 for each repo's `symlink_dirs` entries.

- [ ] **Step 2.1: Write failing doctor test**

Append to `tests/core/test_doctor.py`:

```python
def test_doctor_warns_on_symlink_gitignore_footgun(workspace: Path):
    """Doctor row per symlink_dir whose `.gitignore` has dir-form only. See #72."""
    import subprocess
    from mship.core.config import ConfigLoader
    from mship.core.doctor import DoctorChecker
    from mship.util.shell import ShellRunner
    from unittest.mock import MagicMock
    from mship.util.shell import ShellResult

    # Workspace fixture already has a `shared` repo. Make it a git repo with
    # `foo/` in .gitignore and `symlink_dirs: [foo]`.
    import yaml
    cfg_path = workspace / "mothership.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["repos"]["shared"]["symlink_dirs"] = ["foo"]
    cfg_path.write_text(yaml.safe_dump(cfg))

    shared = workspace / "shared"
    shared.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=shared, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=shared, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=shared, check=True)
    (shared / ".gitignore").write_text("foo/\n")
    (shared / "Taskfile.yml").write_text("version: '3'\ntasks: {test: {cmds: ['true']}, run: {cmds: ['true']}}\n")
    subprocess.run(["git", "add", "."], cwd=shared, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=shared, check=True)

    config = ConfigLoader.load(workspace / "mothership.yaml")
    shell = MagicMock(spec=ShellRunner)
    shell.run.return_value = ShellResult(
        returncode=0, stdout="test\nrun\nlint\nsetup\n", stderr="",
    )

    report = DoctorChecker(config, shell, state_dir=workspace / ".mothership").run()
    rows = [c for c in report.checks if "symlink-ignore" in c.name]
    assert len(rows) == 1, [c.name for c in report.checks]
    assert rows[0].status == "warn"
    assert "foo" in rows[0].message
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `uv run pytest tests/core/test_doctor.py -v -k symlink_gitignore`
Expected: FAIL — no such row in the report.

- [ ] **Step 2.3: Add the doctor check**

Edit `src/mship/core/doctor.py`. Find the end of the existing per-repo loop (after the pre-commit-hook block, before the `# gh CLI` block — around line 220 in the current file). Add this block:

```python
        # Symlink-gitignore footgun check (#72).
        from mship.core.worktree import _symlink_gitignore_footgun
        for name, repo in self._config.repos.items():
            if not repo.symlink_dirs:
                continue
            if repo.git_root is not None:
                parent = self._config.repos[repo.git_root]
                check_path = Path(parent.path).resolve()
            else:
                check_path = Path(repo.path).resolve()
            if not (check_path / ".git").exists():
                continue  # can't check-ignore without a git repo
            for dir_name in repo.symlink_dirs:
                if _symlink_gitignore_footgun(check_path, dir_name):
                    report.checks.append(CheckResult(
                        name=f"{name}/symlink-ignore",
                        status="warn",
                        message=(
                            f"symlink '{dir_name}' is not ignored — "
                            f"add '{dir_name}' (no trailing slash) to .gitignore"
                        ),
                    ))
```

Placement: after the existing `Pre-commit hook presence per unique git root` block, before the `# gh CLI` block. One row per `symlink_dirs` entry that hits the footgun.

- [ ] **Step 2.4: Run test to verify it passes**

Run: `uv run pytest tests/core/test_doctor.py -v -k symlink_gitignore`
Expected: 1 passed.

- [ ] **Step 2.5: Run broader doctor tests for regressions**

Run: `uv run pytest tests/core/test_doctor.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 2.6: Commit**

```bash
git add src/mship/core/doctor.py tests/core/test_doctor.py
git commit -m "feat(doctor): warn row per symlink_dir ignored as dir but not as file"
mship journal "#72: doctor now emits {repo}/symlink-ignore warn row for each symlink_dirs entry where only the dir form is gitignored" --action committed
```

---

## Task 3: `check_pr_state` returns `PrStateResult` with reason

**Files:**
- Modify: `src/mship/core/pr.py` — `check_pr_state` returns NamedTuple; add `_classify_pr_state_reason`.
- Modify: `src/mship/cli/worktree.py` — unpack in `close`; surface reason in log message at line 502.
- Modify: `tests/core/test_pr.py` — update 5 existing `check_pr_state_*` tests; add classification tests.
- Modify: `tests/cli/test_worktree.py` — add close integration test.

**Context:** `check_pr_state` is called only at `src/mship/cli/worktree.py:471` today. Return tuple is `(state, reason)` — state is what it was before; reason is empty for known states, classified string for unknown.

- [ ] **Step 3.1: Write failing classification tests**

Append to `tests/core/test_pr.py`:

```python
import pytest


@pytest.mark.parametrize("stderr,expected", [
    ("GraphQL: API rate limit exceeded for user ID 1", "rate limited"),
    ("You have exceeded a secondary rate limit", "rate limited"),
    ("authentication required; run 'gh auth login'", "gh not authenticated"),
    ("error: not logged in", "gh not authenticated"),
    ("could not resolve host: api.github.com", "network error"),
    ("connection timed out", "network error"),
    ("could not find pull request", "not found"),
    ("GraphQL: Could not resolve to a PullRequest", "not found"),
    ("HTTP 404: Not Found", "not found"),
])
def test_classify_pr_state_reason_signatures(stderr, expected):
    from mship.core.pr import _classify_pr_state_reason
    assert _classify_pr_state_reason(returncode=1, stderr=stderr, raw_state="") == expected


def test_classify_pr_state_reason_unmapped_state():
    from mship.core.pr import _classify_pr_state_reason
    # returncode 0, raw state is something we don't map (e.g. DRAFT).
    reason = _classify_pr_state_reason(returncode=0, stderr="", raw_state="DRAFT")
    assert reason == "unmapped state: DRAFT"


def test_classify_pr_state_reason_gh_not_installed():
    from mship.core.pr import _classify_pr_state_reason
    assert _classify_pr_state_reason(returncode=127, stderr="", raw_state="") == "gh not installed"


def test_classify_pr_state_reason_other_excerpt():
    """Unmatched stderr falls into 'other: <80-char excerpt>'."""
    from mship.core.pr import _classify_pr_state_reason
    stderr = "some unexpected error message we haven't classified: very long " * 3
    reason = _classify_pr_state_reason(returncode=1, stderr=stderr, raw_state="")
    assert reason.startswith("other: ")
    assert len(reason) <= len("other: ") + 80


def test_check_pr_state_returns_pr_state_result_tuple(mock_shell):
    """Return value is a NamedTuple with .state and .reason."""
    from mship.core.pr import PRManager, PrStateResult
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="MERGED\n", stderr="")
    result = PRManager(mock_shell).check_pr_state("https://x/1")
    assert isinstance(result, PrStateResult)
    assert result.state == "merged"
    assert result.reason == ""


def test_check_pr_state_unknown_rate_limit_surfaces_reason(mock_shell):
    from mship.core.pr import PRManager
    mock_shell.run.return_value = ShellResult(
        returncode=1, stdout="",
        stderr="GraphQL: API rate limit exceeded for user ID 1",
    )
    result = PRManager(mock_shell).check_pr_state("https://x/1")
    assert result.state == "unknown"
    assert result.reason == "rate limited"
```

- [ ] **Step 3.2: Update the 5 existing `check_pr_state_*` tests**

The existing tests in `tests/core/test_pr.py` assert on a string return:

```python
assert mgr.check_pr_state("...") == "merged"
```

These five (lines ~190-220) must be updated to compare on `.state`:

```python
assert mgr.check_pr_state("...").state == "merged"
```

Apply the same pattern to all 5:
- `test_check_pr_state_merged`
- `test_check_pr_state_closed`
- `test_check_pr_state_open`
- `test_check_pr_state_unknown_on_failure`
- `test_check_pr_state_unknown_on_unexpected_output`

- [ ] **Step 3.3: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_pr.py -v -k "check_pr_state or classify"`
Expected: new tests fail (`ImportError` for `PrStateResult` / `_classify_pr_state_reason`); updated existing tests fail with `AttributeError: 'str' object has no attribute 'state'` — the old code still returns a string.

- [ ] **Step 3.4: Implement the change**

Edit `src/mship/core/pr.py`. At the top of the file (after existing imports), add:

```python
from typing import NamedTuple


class PrStateResult(NamedTuple):
    """Result of `PRManager.check_pr_state`.

    `state` is one of `merged` / `closed` / `open` / `unknown`. `reason` is
    empty for known states; for `unknown` it's a classified label
    (`rate limited`, `gh not authenticated`, `network error`, `not found`,
    `unmapped state: <raw>`, `gh not installed`, or `other: <excerpt>`).
    Callers include `reason` in log messages so users can act on the cause.
    """
    state: str
    reason: str


def _classify_pr_state_reason(returncode: int, stderr: str, raw_state: str) -> str:
    """Classify why `gh pr view` produced an unknown state. See #73."""
    if returncode == 127:
        return "gh not installed"
    if returncode == 0 and raw_state:
        return f"unmapped state: {raw_state.strip()}"
    s = stderr.lower()
    if "rate limit" in s:
        return "rate limited"
    if (
        "authentication" in s
        or "not logged in" in s
        or "gh auth login" in s
    ):
        return "gh not authenticated"
    if (
        "could not resolve host" in s
        or "network is unreachable" in s
        or "connection timed out" in s
    ):
        return "network error"
    if (
        "not found" in s
        or "could not find pull request" in s
        or "could not resolve to a pullrequest" in s
        or "http 404" in s
    ):
        return "not found"
    excerpt = stderr.strip()[:80]
    return f"other: {excerpt}" if excerpt else "other: (no stderr)"
```

Then replace `check_pr_state` (currently around line 262) entirely:

```python
    def check_pr_state(self, pr_url: str) -> PrStateResult:
        """Return (state, reason) for a PR URL.

        state: 'merged' | 'closed' | 'open' | 'unknown'.
        reason: empty string for known states; classified label for unknown
        (see `_classify_pr_state_reason`).
        """
        result = self._shell.run(
            f"gh pr view {shlex.quote(pr_url)} --json state -q .state",
            cwd=Path("."),
        )
        raw = result.stdout.strip().upper()
        mapping = {"MERGED": "merged", "CLOSED": "closed", "OPEN": "open"}
        if result.returncode == 0 and raw in mapping:
            return PrStateResult(state=mapping[raw], reason="")
        reason = _classify_pr_state_reason(
            returncode=result.returncode,
            stderr=result.stderr,
            raw_state=raw if result.returncode == 0 else "",
        )
        return PrStateResult(state="unknown", reason=reason)
```

- [ ] **Step 3.5: Run `test_pr.py` to verify all pass**

Run: `uv run pytest tests/core/test_pr.py -v 2>&1 | tail -15`
Expected: all pass (5 updated + new classification + new return-type test).

- [ ] **Step 3.6: Update the `close` handler**

Edit `src/mship/cli/worktree.py`. Find the block around line 470:

```python
            for url in task.pr_urls.values():
                pr_states.append(pr_mgr.check_pr_state(url))
```

Replace with:

```python
            pr_state_results: list = []
            for url in task.pr_urls.values():
                pr_state_results.append(pr_mgr.check_pr_state(url))
            pr_states = [r.state for r in pr_state_results]
```

Then find the unknown-fallback log message (currently line 502):

```python
        else:
            log_msg = "closed: pr state unknown"
```

Replace with:

```python
        else:
            # Surface the classification reason so users can act on the cause
            # (auth, network, rate limit, etc). See #73.
            unknown_reasons = [
                r.reason for r in pr_state_results
                if r.state == "unknown" and r.reason
            ]
            if unknown_reasons:
                log_msg = f"closed: pr state unknown ({unknown_reasons[0]})"
            else:
                log_msg = "closed: pr state unknown"
```

Note: `pr_state_results` was introduced in the previous edit; it's now in scope where the log message is built. If `pr_states` is also referenced later in the same function (routing on states), leave those usages untouched — they read `pr_states` which is the list of `.state` strings.

- [ ] **Step 3.7: Write failing close integration test**

Append to `tests/cli/test_worktree.py`:

```python
def test_close_logs_rate_limit_reason_when_pr_state_unknown(configured_git_app: Path):
    """When gh pr view fails with rate-limit stderr, close surfaces the reason. See #73."""
    from mship.cli import container as cli_container
    from mship.util.shell import ShellResult, ShellRunner
    from unittest.mock import MagicMock

    runner.invoke(app, ["spawn", "rate-limit close", "--repos", "shared"])
    # Set a pr_url manually so close actually calls gh pr view.
    import yaml
    state_path = configured_git_app / ".mothership" / "state.yaml"
    data = yaml.safe_load(state_path.read_text())
    data["tasks"]["rate-limit-close"]["pr_urls"] = {
        "shared": "https://github.com/org/repo/pull/1"
    }
    data["tasks"]["rate-limit-close"]["finished_at"] = "2026-04-22T00:00:00Z"
    state_path.write_text(yaml.safe_dump(data))

    def mock_run(cmd, cwd, env=None):
        if "gh pr view" in cmd and "--json state" in cmd:
            return ShellResult(
                returncode=1, stdout="",
                stderr="GraphQL: API rate limit exceeded for user ID 1",
            )
        if "gh pr view" in cmd:
            return ShellResult(returncode=1, stdout="", stderr="err")
        if "git log" in cmd or "merge-base" in cmd or "rev-parse" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run.side_effect = mock_run
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok", stderr="")
    cli_container.shell.override(mock_shell)
    try:
        # --skip-pr-check would short-circuit; don't set it. --force bypasses
        # open-PR refusal.
        result = runner.invoke(
            app, ["close", "rate-limit-close", "-y", "--force", "--bypass-base-ancestry"],
        )
        # Read the journal to find the close log message.
        log_path = configured_git_app / ".mothership" / "logs" / "rate-limit-close.md"
        if log_path.exists():
            log_content = log_path.read_text()
        else:
            log_content = result.output
        assert "rate limited" in log_content or "rate limited" in result.output, (
            f"result.output={result.output!r}\nlog={log_content!r}"
        )
    finally:
        cli_container.shell.reset_override()
```

- [ ] **Step 3.8: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_pr.py tests/cli/test_worktree.py -q 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 3.9: Run broader suite for regressions**

Run: `uv run pytest tests/ --ignore=tests/core/view/test_web_port.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 3.10: Commit**

```bash
git add src/mship/core/pr.py src/mship/cli/worktree.py tests/core/test_pr.py tests/cli/test_worktree.py
git commit -m "feat(pr): check_pr_state returns (state, reason); close surfaces unknown cause"
mship journal "#73: check_pr_state returns PrStateResult(state, reason) with classified reason from gh stderr; close log message includes (<reason>) when any PR is unknown" --action committed
```

---

## Task 4: Smoke + PR

**Files:**
- None (verification + PR only).

**Context:** Reinstall mship, run a quick smoke on each fix, open the PR.

- [ ] **Step 4.1: Reinstall**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/diag-surfaces
uv tool install --reinstall --from . mothership
```

- [ ] **Step 4.2: #72 smoke — verify doctor warns on dir-form-only ignore**

```bash
rm -rf /tmp/symlink-smoke
mkdir -p /tmp/symlink-smoke/origin /tmp/symlink-smoke/repo
cd /tmp/symlink-smoke/repo
git init -q
git config user.email t@t
git config user.name t
mkdir shared-dir
echo "data" > shared-dir/file.txt
cat > .gitignore <<'EOF'
shared-dir/
EOF
cat > Taskfile.yml <<'EOF'
version: '3'
tasks:
  test: {cmds: ['true']}
  run: {cmds: ['true']}
EOF
git add .gitignore Taskfile.yml
git commit -qm "init"
cd /tmp/symlink-smoke
cat > mothership.yaml <<'EOF'
workspace: smoke
repos:
  r:
    path: ./repo
    type: service
    symlink_dirs: [shared-dir]
EOF
mkdir -p .mothership
mship doctor 2>&1 | grep -i "symlink-ignore\|shared-dir"
```

Expected output contains:
```
warn    r/symlink-ignore   symlink 'shared-dir' is not ignored ...
```

Cleanup: `rm -rf /tmp/symlink-smoke`.

- [ ] **Step 4.3: #73 smoke — reason surfaces on classification**

Classification itself is unit-tested; a full end-to-end smoke needs a real rate-limited gh, which we can't reliably reproduce. Instead, verify the helper end-to-end with a Python one-liner:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/diag-surfaces
uv run python -c "
from mship.core.pr import _classify_pr_state_reason
print(_classify_pr_state_reason(returncode=1, stderr='GraphQL: API rate limit exceeded', raw_state=''))
print(_classify_pr_state_reason(returncode=1, stderr='could not resolve host', raw_state=''))
print(_classify_pr_state_reason(returncode=0, stderr='', raw_state='DRAFT'))
"
```

Expected:
```
rate limited
network error
unmapped state: DRAFT
```

- [ ] **Step 4.4: Full pytest**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/diag-surfaces
uv run pytest tests/ --ignore=tests/core/view/test_web_port.py 2>&1 | tail -5
```

Expected: all pass.

- [ ] **Step 4.5: Open the PR**

Write `/tmp/diag-surfaces-body.md`:

```markdown
## Summary

Two narrow diagnostic fixes, one PR. Each is independent of the other.

### Commit 1 — `feat(worktree): warn when symlink_dir is ignored as dir but not as file`

Closes #72.

`.gitignore` with `foo/` (dir form) ignores the directory but NOT a symlink named `foo` — git treats the symlink as a file. Result: audit/finish/close flag the worktree as dirty, even though the user thought they'd ignored the path. The fix (add `foo` without the trailing slash) is tribal knowledge.

New helper `_symlink_gitignore_footgun(repo, name)` runs two `git check-ignore` probes and returns True only when `name/` is ignored but `name` is not. `_create_symlinks` calls it per symlink and appends a non-fatal warning with the exact fix.

Does NOT warn on legitimate tracked symlinks (where neither form is ignored).

### Commit 2 — `feat(doctor): warn row per symlink_dir ignored as dir but not as file`

Same check from Commit 1, surfaced in `mship doctor` as a per-repo warn row so users don't have to re-spawn to see it.

### Commit 3 — `feat(pr): check_pr_state returns (state, reason); close surfaces unknown cause`

Closes #73.

`check_pr_state` now returns `PrStateResult(state, reason)` NamedTuple. `reason` is empty for known states (`merged` / `closed` / `open`); for `unknown`, it's classified from gh stderr into one of:

- `rate limited`
- `gh not authenticated`
- `network error`
- `not found`
- `unmapped state: <raw>`
- `gh not installed`
- `other: <80-char stderr excerpt>`

`mship close`'s "pr state unknown" log message now includes the reason: `closed: pr state unknown (rate limited)` instead of the opaque `closed: pr state unknown`.

Only caller is `close` (line 471 today); updated to unpack the tuple.

## Test plan

- [x] `tests/core/test_worktree.py`: 4 truth-table unit tests for `_symlink_gitignore_footgun` + 2 spawn-path integration tests (warn + regression).
- [x] `tests/core/test_doctor.py`: 1 new test for the symlink-ignore warn row.
- [x] `tests/core/test_pr.py`: 5 existing `check_pr_state_*` tests updated to `.state` access + 4 new classification tests (9 signatures, unmapped, gh-not-installed, other-excerpt) + 1 return-type test.
- [x] `tests/cli/test_worktree.py`: 1 new close integration test (rate-limit reason in log).
- [x] Full suite: all pass.
- [x] Manual smoke: `mship doctor` emits the symlink-ignore warn row; `_classify_pr_state_reason` returns the right label for representative gh stderr signatures.

## Anti-goals preserved

- No auto-fixing `.gitignore`.
- No exhaustive gh error taxonomy — 6 signatures + "other" fallback.
- No changes to other `check_pr_state` callers (there are none today).
```

Then:

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/diag-surfaces
mship finish --body-file /tmp/diag-surfaces-body.md --title "feat(mship): diagnostic surfaces for symlink-ignore + PR state (#72 #73)"
```

Expected: PR URL returned.

---

## Done when

- [x] `_symlink_gitignore_footgun` satisfies the 4-row truth table.
- [x] `_create_symlinks` appends warning on footgun; silent otherwise.
- [x] `mship doctor` emits a `{repo}/symlink-ignore` warn row per bad entry.
- [x] `check_pr_state` returns `PrStateResult(state, reason)`.
- [x] Classification covers rate-limit / auth / network / not-found / unmapped / gh-not-installed / other.
- [x] `mship close`'s log message surfaces the reason when any PR's state is unknown.
- [x] 17+ new tests pass (4 truth-table + 2 spawn + 1 doctor + 10 pr-state + 1 close).
- [x] Full pytest green.
