"""Tests for the pure `build_context` builder (no CLI, no real git)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from mship.core.config import WorkspaceConfig, RepoConfig
from mship.core.context import (
    FOR_VALUES,
    KIND_VALUES,
    SCHEMA_VERSION,
    AudienceError,
    build_context,
)
from mship.core.log import LogManager
from mship.core.reconcile.cache import CachePayload, ReconcileCache
from mship.core.state import Task, TestResult, WorkspaceState
from mship.core.workspace_meta import write_last_sync_at


def _config(tmp_path: Path) -> WorkspaceConfig:
    """Minimal WorkspaceConfig that satisfies the model validators."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    return WorkspaceConfig(
        workspace="t",
        repos={"repo": RepoConfig(path=repo_dir, type="library")},
    )


def _task(slug: str, **overrides) -> Task:
    base = dict(
        slug=slug,
        description=slug,
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["repo"],
        worktrees={},
        branch=f"feat/{slug}",
        base_branch="main",
    )
    base.update(overrides)
    return Task(**base)


def _fake_git_count(answers: dict[tuple[str, str], Optional[int]]):
    """Build a GitCounter that looks up answers by (worktree-name, ref-spec)."""
    def _count(wt: Path, ref: str) -> Optional[int]:
        return answers.get((wt.name, ref))
    return _count


def _no_binary_check() -> Optional[bool]:
    return None


def _build(state, config, log_manager, cwd, state_dir=None, **kw):
    kw.setdefault("git_count", lambda *_: None)
    kw.setdefault("binary_check", _no_binary_check)
    kw.setdefault("dirty_check", lambda _p: None)
    return build_context(
        state, config, log_manager, cwd,
        state_dir=state_dir if state_dir is not None else cwd,
        **kw,
    )


def test_empty_state_returns_no_active_tasks(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path)
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["active_tasks"] == []
    assert out["cwd_matches_task"] is None
    assert out["cwd_matches_repo"] is None


def test_finished_tasks_are_filtered_out(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={
        "live": _task("live"),
        "done": _task("done", finished_at=datetime.now(timezone.utc)),
    })
    out = _build(state, _config(tmp_path), log_mgr, tmp_path)
    slugs = [t["slug"] for t in out["active_tasks"]]
    assert slugs == ["live"]


def test_task_payload_shape(tmp_path: Path):
    wt = tmp_path / "wt-foo"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    log_mgr.append("foo", "did a thing")
    state = WorkspaceState(tasks={"foo": _task(
        "foo",
        worktrees={"repo": wt},
        active_repo="repo",
        pr_urls={"repo": "https://example/pr/1"},
        test_iteration=3,
        test_results={"repo": TestResult(status="pass", at=datetime.now(timezone.utc))},
    )})

    git = _fake_git_count({
        (wt.name, "@{u}..HEAD"): 2,
        (wt.name, "main..HEAD"): 4,
        (wt.name, "main..origin/main"): 1,
    })
    out = _build(state, _config(tmp_path), log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["slug"] == "foo"
    assert task["branch"] == "feat/foo"
    assert task["base_branch"] == "main"
    assert task["worktrees"] == {"repo": str(wt)}
    assert task["active_repo"] == "repo"
    assert task["ahead_of_origin"] == {"repo": 2}
    assert task["ahead_of_base"] == {"repo": 4}
    assert task["base_behind_origin"] == {"repo": 1}
    assert task["pr_urls"] == {"repo": "https://example/pr/1"}
    assert task["last_test_state"] == "pass"
    assert task["last_test_iteration"] == 3
    assert task["last_log_entry_at"] is not None


def test_ahead_of_base_is_null_when_base_branch_unset(tmp_path: Path):
    wt = tmp_path / "wt-x"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"x": _task(
        "x", worktrees={"repo": wt}, base_branch=None,
    )})
    git = _fake_git_count({(wt.name, "@{u}..HEAD"): 1})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["ahead_of_base"] == {"repo": None}
    assert task["ahead_of_origin"] == {"repo": 1}


def _config_with_base(tmp_path: Path, base_branch: Optional[str]) -> WorkspaceConfig:
    """Like `_config` but the repo declares `base_branch` in mothership.yaml."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    return WorkspaceConfig(
        workspace="t",
        repos={"repo": RepoConfig(path=repo_dir, type="library", base_branch=base_branch)},
    )


def test_task_payload_uses_repo_config_base_branch_not_task_field(tmp_path: Path):
    """repo_config.base_branch="dev" (no override) drives both the top-level
    base_branch field and the ahead_of_base count — not task.base_branch,
    which dispatch/context used to read directly (MOS-229)."""
    wt = tmp_path / "wt-foo"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"foo": _task(
        "foo", worktrees={"repo": wt}, active_repo="repo", base_branch="staging",
    )})
    cfg = _config_with_base(tmp_path, "dev")
    git = _fake_git_count({(wt.name, "dev..HEAD"): 4})
    out = _build(state, cfg, log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["base_branch"] == "dev"
    assert task["ahead_of_base"] == {"repo": 4}


def test_task_payload_base_override_wins_over_repo_config(tmp_path: Path):
    """task.base_override (the --base pin) beats repo_config.base_branch."""
    wt = tmp_path / "wt-bar"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"bar": _task(
        "bar", worktrees={"repo": wt}, active_repo="repo",
        base_branch="main", base_override="stacked",
    )})
    cfg = _config_with_base(tmp_path, "dev")
    git = _fake_git_count({(wt.name, "stacked..HEAD"): 2})
    out = _build(state, cfg, log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["base_branch"] == "stacked"
    assert task["ahead_of_base"] == {"repo": 2}


def test_task_payload_repo_missing_from_config_falls_back_without_error(tmp_path: Path):
    """A repo referenced by the task but absent from mothership.yaml must not
    crash — resolve_base tolerates a missing repo_config and context falls
    back to task.base_branch, same as before this fix."""
    wt = tmp_path / "wt-baz"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"baz": _task(
        "baz", worktrees={"missing-repo": wt}, active_repo="missing-repo",
        base_branch="main",
    )})
    cfg = _config(tmp_path)  # only declares "repo", not "missing-repo"
    git = _fake_git_count({(wt.name, "main..HEAD"): 1})
    out = _build(state, cfg, log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["base_branch"] == "main"
    assert task["ahead_of_base"] == {"missing-repo": 1}


def test_task_payload_scalar_base_uses_task_base_when_no_active_repo(tmp_path: Path):
    """With no active_repo, the scalar base_branch is the task's own base, NOT
    the first inserted worktree's repo config (Greptile, MOS-229: avoids a
    user-facing value depending on dict order). Per-repo ahead_of_base still
    resolves against each repo's configured base."""
    wt = tmp_path / "wt-none"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"none": _task(
        "none", worktrees={"repo": wt}, active_repo=None, base_branch="staging",
    )})
    cfg = _config_with_base(tmp_path, "dev")
    git = _fake_git_count({(wt.name, "dev..HEAD"): 3})
    out = _build(state, cfg, log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["base_branch"] == "staging"          # task base, not repo's "dev"
    assert task["ahead_of_base"] == {"repo": 3}       # per-repo still uses "dev"


def test_base_behind_origin_reports_commits_behind(tmp_path: Path):
    """MOS-203: an agent should be able to gate on how far the repo's base is
    behind origin/<base> (read-only — reflects the last fetch, no fetch here)
    instead of eyeballing last_workspace_fetch_at."""
    wt = tmp_path / "wt-foo"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"foo": _task("foo", worktrees={"repo": wt})})
    git = _fake_git_count({(wt.name, "main..origin/main"): 5})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["base_behind_origin"] == {"repo": 5}


def test_base_behind_origin_zero_when_base_current(tmp_path: Path):
    wt = tmp_path / "wt-foo"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"foo": _task("foo", worktrees={"repo": wt})})
    git = _fake_git_count({(wt.name, "main..origin/main"): 0})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["base_behind_origin"] == {"repo": 0}


def test_base_behind_origin_null_when_base_branch_unset(tmp_path: Path):
    wt = tmp_path / "wt-x"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"x": _task(
        "x", worktrees={"repo": wt}, base_branch=None,
    )})
    git = _fake_git_count({(wt.name, "@{u}..HEAD"): 1})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path, git_count=git)
    task = out["active_tasks"][0]
    assert task["base_behind_origin"] == {"repo": None}


def test_cwd_inside_worktree_populates_match_fields(tmp_path: Path):
    wt = tmp_path / "wt-match"
    (wt / "src").mkdir(parents=True)
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"m": _task("m", worktrees={"repo": wt})})

    out = _build(state, _config(tmp_path), log_mgr, wt / "src")
    assert out["cwd_matches_task"] == "m"
    assert out["cwd_matches_repo"] == "repo"


def test_cwd_outside_any_worktree_yields_none(tmp_path: Path):
    wt = tmp_path / "wt-other"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"o": _task("o", worktrees={"repo": wt})})

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = _build(state, _config(tmp_path), log_mgr, elsewhere)
    assert out["cwd_matches_task"] is None
    assert out["cwd_matches_repo"] is None


def test_finished_task_does_not_capture_cwd(tmp_path: Path):
    """A finished task's worktree shouldn't claim cwd — it's stale."""
    wt = tmp_path / "wt-done"
    wt.mkdir()
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"d": _task(
        "d", worktrees={"repo": wt}, finished_at=datetime.now(timezone.utc),
    )})
    out = _build(state, _config(tmp_path), log_mgr, wt)
    assert out["cwd_matches_task"] is None
    assert out["cwd_matches_repo"] is None


def test_binary_check_passthrough(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(
        WorkspaceState(), _config(tmp_path), log_mgr, tmp_path,
        binary_check=lambda: False,
    )
    assert out["mship_binary_matches_editable_install"] is False


def test_no_test_results_yields_null_state(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"q": _task("q")})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path)
    task = out["active_tasks"][0]
    assert task["last_test_state"] is None
    assert task["last_test_iteration"] == 0


def test_most_recent_test_result_wins(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 4, 1, tzinfo=timezone.utc)
    state = WorkspaceState(tasks={"r": _task(
        "r",
        test_results={
            "a": TestResult(status="pass", at=older),
            "b": TestResult(status="fail", at=newer),
        },
    )})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path)
    assert out["active_tasks"][0]["last_test_state"] == "fail"


# --- Tier 2: drift, main_checkout_clean, fetch/drift timestamps -----------


def test_drift_unknown_when_no_cache(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"u": _task("u")})
    out = _build(state, _config(tmp_path), log_mgr, tmp_path)
    assert out["active_tasks"][0]["drift"] == "unknown"
    assert out["last_drift_check_at"] is None


def test_drift_read_from_cache(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"a": _task("a"), "b": _task("b")})

    cache = ReconcileCache(tmp_path)
    fetched = time.time()
    cache.write(CachePayload(
        fetched_at=fetched, ttl_seconds=300,
        results={
            "a": {"state": "merged", "pr_url": None, "pr_number": None,
                  "base": None, "merge_commit": None, "updated_at": None},
            # 'b' deliberately absent -> "unknown"
        },
        ignored=[],
    ))

    out = _build(state, _config(tmp_path), log_mgr, tmp_path,
                 state_dir=tmp_path, cache=cache)
    by_slug = {t["slug"]: t for t in out["active_tasks"]}
    assert by_slug["a"]["drift"] == "merged"
    assert by_slug["b"]["drift"] == "unknown"
    assert out["last_drift_check_at"] is not None


def test_drift_unknown_when_cache_entry_malformed(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"x": _task("x")})

    cache = ReconcileCache(tmp_path)
    cache.write(CachePayload(
        fetched_at=time.time(), ttl_seconds=300,
        results={"x": {"not_a_state_field": "junk"}},
        ignored=[],
    ))

    out = _build(state, _config(tmp_path), log_mgr, tmp_path,
                 state_dir=tmp_path, cache=cache)
    assert out["active_tasks"][0]["drift"] == "unknown"


def test_main_checkout_clean_dispatches_per_repo(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    cfg = _config(tmp_path)
    seen: list[Path] = []

    def dirty(p: Path) -> Optional[bool]:
        seen.append(p)
        return False  # clean

    out = _build(
        WorkspaceState(), cfg, log_mgr, tmp_path,
        dirty_check=dirty,
    )
    assert out["main_checkout_clean"] == {"repo": True}
    assert seen == [cfg.repos["repo"].path]


def test_main_checkout_clean_reports_dirty(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(
        WorkspaceState(), _config(tmp_path), log_mgr, tmp_path,
        dirty_check=lambda _p: True,
    )
    assert out["main_checkout_clean"] == {"repo": False}


def test_main_checkout_clean_unknown_on_error(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(
        WorkspaceState(), _config(tmp_path), log_mgr, tmp_path,
        dirty_check=lambda _p: None,
    )
    assert out["main_checkout_clean"] == {"repo": None}


def test_main_checkout_clean_skips_git_root_children(tmp_path: Path):
    """Repos with `git_root` set share their parent's checkout — don't double-report."""
    log_mgr = LogManager(tmp_path / "logs")

    parent_dir = tmp_path / "mono"
    parent_dir.mkdir()
    (parent_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    child_dir = parent_dir / "pkg"
    child_dir.mkdir()
    (child_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg = WorkspaceConfig(
        workspace="t",
        repos={
            "mono": RepoConfig(path=parent_dir, type="service"),
            "pkg": RepoConfig(path=child_dir, type="library", git_root="mono"),
        },
    )

    out = _build(
        WorkspaceState(), cfg, log_mgr, tmp_path,
        dirty_check=lambda _p: False,
    )
    assert out["main_checkout_clean"] == {"mono": True}
    assert "pkg" not in out["main_checkout_clean"]


def test_last_workspace_fetch_at_null_when_unset(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path)
    assert out["last_workspace_fetch_at"] is None


def test_last_workspace_fetch_at_round_trips(tmp_path: Path):
    log_mgr = LogManager(tmp_path / "logs")
    when = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
    write_last_sync_at(tmp_path, when)
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path,
                 state_dir=tmp_path)
    assert out["last_workspace_fetch_at"] == when.isoformat()


# --- MOS-100: --for/--kind audience-shaped output --------------------------


def test_no_for_flag_omits_audience_key(tmp_path: Path):
    """ac1/ac8: default (no --for) has no `audience` key at all."""
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path)
    assert "audience" not in out


def test_base_payload_identical_regardless_of_for_kind(tmp_path: Path):
    """ac10: every base factual field is quoted verbatim whether or not
    --for/--kind is supplied — only the trailing `audience` key differs."""
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"foo": _task("foo")})
    cfg = _config(tmp_path)
    baseline = _build(state, cfg, log_mgr, tmp_path)
    shaped = _build(state, cfg, log_mgr, tmp_path, for_="reviewer", kind="spec")
    shaped_without_audience = {k: v for k, v in shaped.items() if k != "audience"}
    assert shaped_without_audience == baseline


@pytest.mark.parametrize("for_value", ["claude-code", "codex"])
def test_implementer_audience_instructions(tmp_path: Path, for_value: str):
    """ac2: claude-code and codex both get the implementer framing — work
    from the worktree, never commit to main, commit via `mship commit`,
    journal investigation via `mship debug hypothesis`."""
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_=for_value)
    audience = out["audience"]
    assert audience["for"] == for_value
    assert audience["kind"] is None
    text = audience["instructions"]
    assert "worktree" in text
    assert "main" in text
    assert "mship commit" in text
    assert "mship debug hypothesis" in text


def test_claude_code_and_codex_share_identical_instructions(tmp_path: Path):
    """q2: claude-code and codex are designed to share one implementer string."""
    log_mgr = LogManager(tmp_path / "logs")
    cfg = _config(tmp_path)
    cc = _build(WorkspaceState(), cfg, log_mgr, tmp_path, for_="claude-code")
    codex = _build(WorkspaceState(), cfg, log_mgr, tmp_path, for_="codex")
    assert cc["audience"]["instructions"] == codex["audience"]["instructions"]


def test_human_audience_instructions(tmp_path: Path):
    """ac3: --for human gets a prose-style human summary instruction."""
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_="human")
    audience = out["audience"]
    assert audience["for"] == "human"
    assert audience["kind"] is None
    assert "status" in audience["instructions"] or "summary" in audience["instructions"]


def test_reviewer_spec_audience_instructions(tmp_path: Path):
    """ac4: --for reviewer --kind spec instructs verifying against the task
    description/plan and flagging over-/under-building."""
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_="reviewer", kind="spec")
    audience = out["audience"]
    assert audience["for"] == "reviewer"
    assert audience["kind"] == "spec"
    text = audience["instructions"]
    assert "spec" in text.lower() or "task description" in text.lower()
    assert "over-built" in text or "under-built" in text


def test_reviewer_code_quality_audience_instructions(tmp_path: Path):
    """ac5: --for reviewer --kind code-quality instructs inspecting the diff
    for maintainability, naming, test quality, and regressions."""
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_="reviewer", kind="code-quality")
    audience = out["audience"]
    assert audience["for"] == "reviewer"
    assert audience["kind"] == "code-quality"
    text = audience["instructions"]
    for term in ("maintainability", "naming", "test", "regression"):
        assert term in text.lower()


def test_kind_without_for_raises(tmp_path: Path):
    """ac6: --kind without --for at all is rejected."""
    log_mgr = LogManager(tmp_path / "logs")
    with pytest.raises(AudienceError):
        _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, kind="spec")


def test_kind_with_non_reviewer_for_raises(tmp_path: Path):
    """ac6: --kind with a non-reviewer --for is rejected."""
    log_mgr = LogManager(tmp_path / "logs")
    with pytest.raises(AudienceError):
        _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_="human", kind="spec")


def test_reviewer_without_kind_raises(tmp_path: Path):
    """ac7: --for reviewer without --kind is rejected (kind is required)."""
    log_mgr = LogManager(tmp_path / "logs")
    with pytest.raises(AudienceError):
        _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_="reviewer")


def test_unknown_for_value_raises(tmp_path: Path):
    """Unknown --for values are rejected with a clear error, not silently accepted."""
    log_mgr = LogManager(tmp_path / "logs")
    with pytest.raises(AudienceError):
        _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_="gemini")


def test_unknown_kind_value_raises(tmp_path: Path):
    """Unknown --kind values (even with --for reviewer) are rejected."""
    log_mgr = LogManager(tmp_path / "logs")
    with pytest.raises(AudienceError):
        _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_="reviewer", kind="bogus")


def test_audience_block_exact_shape(tmp_path: Path):
    """ac8: the audience block is exactly {"for", "kind", "instructions"} —
    no extra keys, no missing ones."""
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_="reviewer", kind="code-quality")
    assert set(out["audience"].keys()) == {"for", "kind", "instructions"}


def test_audience_kind_is_null_for_non_reviewer(tmp_path: Path):
    """ac8: `kind` is null (not omitted) for audiences other than reviewer."""
    log_mgr = LogManager(tmp_path / "logs")
    out = _build(WorkspaceState(), _config(tmp_path), log_mgr, tmp_path, for_="human")
    assert out["audience"]["kind"] is None


def test_no_synthesized_fields_added(tmp_path: Path):
    """ac11: no inferred/synthesized field (e.g. current_hypothesis,
    next_recommended_action) sneaks into the payload alongside audience."""
    log_mgr = LogManager(tmp_path / "logs")
    state = WorkspaceState(tasks={"foo": _task("foo")})
    cfg = _config(tmp_path)
    baseline_keys = set(_build(state, cfg, log_mgr, tmp_path).keys())
    shaped_keys = set(_build(state, cfg, log_mgr, tmp_path, for_="reviewer", kind="spec").keys())
    assert shaped_keys - baseline_keys == {"audience"}


def test_for_values_and_kind_values_are_the_documented_closed_set():
    """q1: the closed set of --for values is claude-code/codex/human/reviewer;
    --kind is spec/code-quality."""
    assert set(FOR_VALUES) == {"claude-code", "codex", "human", "reviewer"}
    assert set(KIND_VALUES) == {"spec", "code-quality"}
