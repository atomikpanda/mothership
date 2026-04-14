import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.config import ConfigLoader
from mship.core.log import LogManager
from mship.core.state import StateManager
from mship.core.switch import build_handoff, DepChange, Handoff
from mship.util.shell import ShellRunner


def _sh(*args, cwd, env=None):
    e = {**os.environ,
         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if env is not None:
        e.update(env)
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=e)


def _head_sha(path: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True,
    )
    return r.stdout.strip()



def test_handoff_first_switch_uses_merge_base_fallback(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace

    # Commit something new in shared's worktree (goes into the feat/t branch)
    (shared_wt / "new.txt").write_text("hello\n")
    _sh("git", "add", "new.txt", cwd=shared_wt)
    _sh("git", "commit", "-qm", "add new.txt", cwd=shared_wt)

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    state = sm.load()

    handoff = build_handoff(cfg, state, shell, log_mgr, repo="cli")

    assert isinstance(handoff, Handoff)
    assert handoff.repo == "cli"
    assert handoff.worktree_path == cli_wt
    assert not handoff.worktree_missing
    # First switch — fallback anchor (merge-base) picks up the new shared commit
    dep_names = [d.repo for d in handoff.dep_changes]
    assert dep_names == ["shared"]
    shared_change = handoff.dep_changes[0]
    assert shared_change.commit_count >= 1
    assert shared_change.error is None
    assert "new.txt" in shared_change.files_changed


def test_handoff_subsequent_switch_uses_stored_sha(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace

    # First snapshot: shared's current SHA stored under last_switched_at_sha['cli']['shared']
    state = sm.load()
    state.tasks["t"].last_switched_at_sha = {"cli": {"shared": _head_sha(shared_wt)}}
    sm.save(state)

    # New commit in shared after the snapshot
    (shared_wt / "new.txt").write_text("hi\n")
    _sh("git", "add", "new.txt", cwd=shared_wt)
    _sh("git", "commit", "-qm", "post-snapshot", cwd=shared_wt)

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")

    shared_change = handoff.dep_changes[0]
    assert shared_change.commit_count == 1
    assert "post-snapshot" in shared_change.commits[0]


def test_handoff_clean_deps_omitted(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    # No new commits in shared; the first-switch fallback anchor is merge-base == HEAD
    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.dep_changes == ()


def test_handoff_missing_dep_worktree(switch_workspace):
    import shutil
    workspace, shared_wt, cli_wt, sm = switch_workspace
    shutil.rmtree(shared_wt)
    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert len(handoff.dep_changes) == 1
    assert handoff.dep_changes[0].repo == "shared"
    assert handoff.dep_changes[0].error is not None
    assert "worktree" in handoff.dep_changes[0].error.lower()


def test_handoff_missing_switched_to_worktree(switch_workspace):
    import shutil
    workspace, shared_wt, cli_wt, sm = switch_workspace
    shutil.rmtree(cli_wt)
    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.worktree_missing is True


def test_handoff_includes_finished_at(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    state = sm.load()
    state.tasks["t"].finished_at = datetime.now(timezone.utc)
    sm.save(state)
    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.finished_at is not None


def test_handoff_last_log_entry(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    log_mgr.append("t", "wired Label into middleware")

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.last_log_in_repo is not None
    assert "Label" in handoff.last_log_in_repo.message


def test_handoff_drift_count_nonzero_when_dirty(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace
    (cli_wt / "dirty.txt").write_text("x\n")

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    log_mgr = LogManager(workspace / ".mothership" / "logs")
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="cli")
    assert handoff.drift_error_count >= 1


def test_handoff_prefers_repo_tagged_log_entry(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace

    log_mgr = LogManager(workspace / ".mothership" / "logs")
    log_mgr.append("t", "generic older entry")
    log_mgr.append("t", "older shared entry", repo="shared")
    log_mgr.append("t", "most recent untagged")

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="shared")
    assert handoff.last_log_in_repo is not None
    assert handoff.last_log_in_repo.message == "older shared entry"


def test_handoff_falls_back_to_latest_when_no_repo_tag(switch_workspace):
    workspace, shared_wt, cli_wt, sm = switch_workspace

    log_mgr = LogManager(workspace / ".mothership" / "logs")
    log_mgr.append("t", "untagged only")

    cfg = ConfigLoader.load(workspace / "mothership.yaml")
    shell = ShellRunner()
    handoff = build_handoff(cfg, sm.load(), shell, log_mgr, repo="shared")
    assert handoff.last_log_in_repo is not None
    assert handoff.last_log_in_repo.message == "untagged only"
