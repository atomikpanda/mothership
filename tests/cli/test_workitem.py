import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

from typer.testing import CliRunner

from mship.cli import app, container
from mship.cli.workitem import _branch_state_for, _probe_ref_names
from mship.core.run_state import RunStateRepo
from mship.core.spec import Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()

_NOW = datetime(2026, 7, 8, tzinfo=timezone.utc)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd, *args):
    import os
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, env={**os.environ, **_GIT_ENV})
    assert r.returncode == 0, r.stderr or r.stdout
    return r


def _isolate(tmp_path):
    """Point the global container at a throwaway workspace."""
    (tmp_path / "mothership.yaml").write_text("workspace: testws\nrepos: {}\n")
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    container.config.reset()  # drop any singleton cached by another test
    # state_manager/log_manager are Singletons keyed off state_dir; drop any
    # instance a prior test bound to its own tmp so they rebind to this one.
    container.state_manager.reset()
    container.log_manager.reset()


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()


def _make_origin(tmp_path):
    """A bare repo the run-state ref can push to, wired as the workspace origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=tmp_path,
                   check=True, capture_output=True, text=True)
    return origin


def _eligible_item(tmp_path, spec_id="s-1"):
    """Create an unattended, phase=ready item bound to an approved spec; return its id."""
    res = runner.invoke(app, ["item", "new", "Do the thing", "--kind", "feature"])
    assert res.exit_code == 0, res.output
    item_id = res.output.strip()
    SpecStore(tmp_path / "specs").save(Spec(
        id=spec_id, title="Do the thing", status="approved",
        created_at=_NOW, updated_at=_NOW, body="## Problem\n\nNeeds doing.\n",
    ))
    assert runner.invoke(app, ["item", "link-spec", item_id, spec_id]).exit_code == 0
    assert runner.invoke(app, ["item", "phase", item_id, "ready"]).exit_code == 0
    assert runner.invoke(app, ["item", "unattended", item_id, "--on"]).exit_code == 0
    return item_id


def test_item_run_next_emits_prompt_and_claims(tmp_path):
    _isolate(tmp_path)
    origin = _make_origin(tmp_path)
    try:
        item_id = _eligible_item(tmp_path)
        res = runner.invoke(app, ["--json", "item", "run-next"])
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["runnable"] is True
        assert payload["item_id"] == item_id
        assert "Do the thing" in payload["prompt"]
        # the claim is recorded on the shared run-state ref (readable by any run)
        assert RunStateRepo(origin, tmp_path / "verify").read_claim(item_id) is not None
    finally:
        _reset()


def test_item_run_next_noop_exit_zero_when_empty(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["--json", "item", "run-next"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output) == {"runnable": False}
    finally:
        _reset()


def test_item_bail_logs_reason_and_releases(tmp_path):
    _isolate(tmp_path)
    origin = _make_origin(tmp_path)
    try:
        item_id = _eligible_item(tmp_path)
        # Give the item a task so the derived "blocked" flag is assertable
        # (blocked is computed from a task's blocked_reason).
        sm = StateManager(tmp_path / ".mothership")
        sm.mutate(lambda s: s.tasks.__setitem__("t-1", Task(
            slug="t-1", description="d", phase="dev", created_at=_NOW,
            affected_repos=["mothership"], branch="feat/t-1")))
        assert runner.invoke(app, ["item", "link-task", item_id, "t-1"]).exit_code == 0

        # Claim it via run-next; same process => same holder, so bail can release.
        assert runner.invoke(app, ["--json", "item", "run-next"]).exit_code == 0

        res = runner.invoke(app, ["item", "bail", item_id, "--reason", "fork on auth"])
        assert res.exit_code == 0, res.output

        rs = RunStateRepo(origin, tmp_path / "verify")
        assert any("fork on auth" in e.text for e in rs.read_log(item_id))  # reason logged
        assert rs.read_claim(item_id) is None                              # claim released
        assert sm.load().tasks["t-1"].blocked_reason == "fork on auth"     # item blocked
    finally:
        _reset()


def test_item_run_next_skips_blocked_item(tmp_path):
    # FIX#1: an item whose linked task is blocked (a prior bail) must not be re-offered.
    _isolate(tmp_path)
    _make_origin(tmp_path)
    try:
        item_id = _eligible_item(tmp_path)
        sm = StateManager(tmp_path / ".mothership")
        sm.mutate(lambda s: s.tasks.__setitem__("t-1", Task(
            slug="t-1", description="d", phase="dev", created_at=_NOW,
            affected_repos=["mothership"], branch="feat/t-1",
            blocked_reason="fork on auth", blocked_at=_NOW)))
        assert runner.invoke(app, ["item", "link-task", item_id, "t-1"]).exit_code == 0

        res = runner.invoke(app, ["--json", "item", "run-next"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output) == {"runnable": False}   # blocked → not offered
    finally:
        _reset()


def test_item_bail_releases_cross_process_claim(tmp_path):
    # FIX#2 end-to-end: the claim was minted by ANOTHER process (a different holder
    # token, as run-next on a separate host/pid would). `mship item bail` must still
    # release it (authoritative release reads the recorded holder off the ref).
    _isolate(tmp_path)
    origin = _make_origin(tmp_path)
    try:
        item_id = _eligible_item(tmp_path)
        # Simulate run-next having claimed under a foreign holder (different pid/host):
        RunStateRepo(origin, tmp_path / "otherhost").try_claim(
            item_id, holder="otherhost:99999", now=_NOW)
        assert RunStateRepo(origin, tmp_path / "v0").read_claim(item_id) is not None

        res = runner.invoke(app, ["item", "bail", item_id, "--reason", "fork"])
        assert res.exit_code == 0, res.output
        assert RunStateRepo(origin, tmp_path / "verify").read_claim(item_id) is None
    finally:
        _reset()


def test_item_heartbeat_keeps_claim(tmp_path):
    # FIX#3b: `mship item heartbeat` advances a live claim's heartbeat (authoritatively
    # across processes) so a long run isn't reclaimed. Here it keeps the claim present.
    _isolate(tmp_path)
    origin = _make_origin(tmp_path)
    try:
        item_id = _eligible_item(tmp_path)
        assert runner.invoke(app, ["--json", "item", "run-next"]).exit_code == 0
        before = RunStateRepo(origin, tmp_path / "v0").read_claim(item_id)
        assert before is not None

        res = runner.invoke(app, ["item", "heartbeat", item_id])
        assert res.exit_code == 0, res.output

        after = RunStateRepo(origin, tmp_path / "verify").read_claim(item_id)
        assert after is not None
        assert after.holder == before.holder                  # same holder, not stolen
        assert after.heartbeat_at >= before.heartbeat_at      # heartbeat advanced
    finally:
        _reset()


def test_branch_state_reads_commits_ahead_from_remote(tmp_path):
    # FIX#4b: a fresh clone (no task worktree on disk) must still detect prior work by
    # reading commits-ahead from the REMOTE branch a prior bail pushed.
    member_origin = tmp_path / "member.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(member_origin)],
                   check=True, capture_output=True)
    work = tmp_path / "member-work"
    _git(tmp_path, "clone", "-q", str(member_origin), str(work))
    (work / "base.txt").write_text("base")
    _git(work, "add", "-A"); _git(work, "commit", "-m", "base")
    _git(work, "push", "-q", "-u", "origin", "main")
    _git(work, "checkout", "-q", "-b", "feat/wi-1")
    (work / "one.txt").write_text("1"); _git(work, "add", "-A"); _git(work, "commit", "-m", "c1")
    (work / "two.txt").write_text("2"); _git(work, "add", "-A"); _git(work, "commit", "-m", "c2")
    _git(work, "push", "-q", "-u", "origin", "feat/wi-1")

    # A pristine clone: main is checked out, the feature branch exists only on origin.
    fresh = tmp_path / "fresh"
    _git(tmp_path, "clone", "-q", str(member_origin), str(fresh))

    task = Task(slug="t-1", description="d", phase="dev", created_at=_NOW,
                affected_repos=["member"], branch="feat/wi-1", base_branch="main")
    state = WorkspaceState(tasks={"t-1": task})
    item = SimpleNamespace(id="wi-1", task_slugs=["t-1"])
    config = SimpleNamespace(branch_pattern="feat/{slug}",
                             repos={"member": SimpleNamespace(path=fresh)})
    log_mgr = SimpleNamespace(read=lambda slug, last=None: [])

    bs = _branch_state_for(item, state, log_mgr, config)
    assert bs.branch == "feat/wi-1"
    assert bs.commits_ahead == 2   # counted from origin, not a local worktree


def test_branch_state_fresh_start_when_branch_absent_on_remote(tmp_path):
    # No remote branch yet ⇒ a truly fresh start (commits_ahead=0, no RESUMING wrap).
    member_origin = tmp_path / "member.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(member_origin)],
                   check=True, capture_output=True)
    work = tmp_path / "member-work"
    _git(tmp_path, "clone", "-q", str(member_origin), str(work))
    (work / "base.txt").write_text("base")
    _git(work, "add", "-A"); _git(work, "commit", "-m", "base")
    _git(work, "push", "-q", "-u", "origin", "main")
    fresh = tmp_path / "fresh"
    _git(tmp_path, "clone", "-q", str(member_origin), str(fresh))

    task = Task(slug="t-1", description="d", phase="dev", created_at=_NOW,
                affected_repos=["member"], branch="feat/never-pushed", base_branch="main")
    state = WorkspaceState(tasks={"t-1": task})
    item = SimpleNamespace(id="wi-1", task_slugs=["t-1"])
    config = SimpleNamespace(branch_pattern="feat/{slug}",
                             repos={"member": SimpleNamespace(path=fresh)})
    log_mgr = SimpleNamespace(read=lambda slug, last=None: [])

    bs = _branch_state_for(item, state, log_mgr, config)
    assert bs.commits_ahead == 0


def test_probe_ref_names_differ_across_tasks(monkeypatch):
    # Greptile #3: refs/mship-probe/branch and .../base were hardcoded, so two
    # tasks' _remote_commits_ahead calls sharing one clone (or two concurrent
    # runner processes) could collide on the same probe ref mid-fetch. Namespace
    # by the (sanitized) task slug so distinct tasks never collide.
    monkeypatch.setattr("os.getpid", lambda: 4242)
    task_a = SimpleNamespace(slug="t-1")
    task_b = SimpleNamespace(slug="t-2")
    br_a, base_a = _probe_ref_names(task_a)
    br_b, base_b = _probe_ref_names(task_b)
    assert {br_a, base_a}.isdisjoint({br_b, base_b})
    assert br_a != base_a
    for ref in (br_a, base_a, br_b, base_b):
        assert ref.startswith("refs/mship-probe/")


def test_probe_ref_names_include_pid_for_cross_process_uniqueness(monkeypatch):
    # Two concurrent processes probing the SAME task (e.g. two runner hosts
    # racing item run-next) must still get distinct refs.
    task = SimpleNamespace(slug="t-1")
    monkeypatch.setattr("os.getpid", lambda: 111)
    br_1, base_1 = _probe_ref_names(task)
    monkeypatch.setattr("os.getpid", lambda: 222)
    br_2, base_2 = _probe_ref_names(task)
    assert {br_1, base_1}.isdisjoint({br_2, base_2})


def test_probe_ref_names_sanitize_unsafe_slug_chars(monkeypatch):
    # A task slug is expected to be simple, but the probe-ref builder must not
    # produce an invalid/ambiguous git ref if it ever contains ref-hostile chars.
    monkeypatch.setattr("os.getpid", lambda: 1)
    task = SimpleNamespace(slug="wi/1..2~x?*[y]: z")
    br, base = _probe_ref_names(task)
    for ref in (br, base):
        assert " " not in ref
        assert ".." not in ref
        for bad in ("~", "^", ":", "?", "*", "[", "]"):
            assert bad not in ref
        # exactly the 3 fixed separators (mship-probe/<ns>/branch|base)
        assert ref.count("/") == 3


def test_new_then_list_roundtrip(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Make capture conversational", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        res = runner.invoke(app, ["--json", "item", "list"])
        assert res.exit_code == 0, res.output
        rows = json.loads(res.output)
        assert len(rows) == 1
        assert rows[0]["title"] == "Make capture conversational"
        assert rows[0]["phase"] == "inbox"
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_new_with_invalid_kind_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "bogus"])
        assert res.exit_code == 1
        assert "invalid kind" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_phase_with_invalid_value_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()
        res = runner.invoke(app, ["item", "phase", item_id, "bogus"])
        assert res.exit_code == 1
        assert "invalid phase" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


# ---------------------------------------------------------------------------
# Lifecycle hooks (MOS-220, spec mship-lifecycle-hooks): `workitem.phase.<phase>`
# ---------------------------------------------------------------------------


def test_item_phase_fires_workitem_phase_hook(tmp_path):
    from unittest.mock import MagicMock
    from mship.util.shell import ShellResult, ShellRunner

    (tmp_path / "mothership.yaml").write_text(
        "workspace: testws\nrepos: {}\n"
        "lifecycle_hooks:\n"
        "  - on: workitem.phase.done\n"
        "    run: notify-done\n"
    )
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()

        call_log = []

        def mock_run(cmd, cwd, env=None, timeout=None):
            call_log.append(cmd)
            return ShellResult(returncode=0, stdout="", stderr="")

        mock_shell = MagicMock(spec=ShellRunner)
        mock_shell.run.side_effect = mock_run
        mock_shell.build_command.side_effect = (
            lambda command, env_runner=None: f"{env_runner} {command}" if env_runner else command
        )
        container.shell.override(mock_shell)

        res = runner.invoke(app, ["item", "phase", item_id, "done"])
        assert res.exit_code == 0, res.output
        assert any("notify-done" in c for c in call_log)

        from mship.core.workitem_store import WorkItemStore
        items = WorkItemStore(tmp_path / ".mothership" / "workitems")
        assert items.get(item_id).phase_override == "done"
    finally:
        container.shell.reset_override()
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()


def test_item_phase_required_hook_failure_blocks_override(tmp_path):
    from unittest.mock import MagicMock
    from mship.util.shell import ShellResult, ShellRunner

    (tmp_path / "mothership.yaml").write_text(
        "workspace: testws\nrepos: {}\n"
        "lifecycle_hooks:\n"
        "  - on: workitem.phase.done\n"
        "    run: notify-done-fails\n"
        "    required: true\n"
    )
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(tmp_path / ".mothership")
    container.config.reset()
    container.state_manager.reset()
    container.log_manager.reset()
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()

        mock_shell = MagicMock(spec=ShellRunner)
        mock_shell.run.return_value = ShellResult(returncode=1, stdout="", stderr="boom")
        mock_shell.build_command.side_effect = (
            lambda command, env_runner=None: f"{env_runner} {command}" if env_runner else command
        )
        container.shell.override(mock_shell)

        res = runner.invoke(app, ["item", "phase", item_id, "done"])
        assert res.exit_code != 0

        from mship.core.workitem_store import WorkItemStore
        items = WorkItemStore(tmp_path / ".mothership" / "workitems")
        assert items.get(item_id).phase_override is None
    finally:
        container.shell.reset_override()
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
        container.state_manager.reset()
        container.log_manager.reset()


def test_item_unattended_cli(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()

        res = runner.invoke(app, ["item", "unattended", item_id, "--on"])
        assert res.exit_code == 0, res.output
        assert "unattended=True" in res.output

        res = runner.invoke(app, ["item", "show", item_id])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["unattended"] is True

        res = runner.invoke(app, ["item", "unattended", item_id, "--off"])
        assert res.exit_code == 0, res.output
        assert "unattended=False" in res.output

        res = runner.invoke(app, ["item", "show", item_id])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["unattended"] is False
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_link_plan_sets_plan_path(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()

        res = runner.invoke(app, ["item", "link-plan", item_id, "docs/plans/x.md"])
        assert res.exit_code == 0, res.output
        assert "docs/plans/x.md" in res.output

        res = runner.invoke(app, ["item", "show", item_id])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["plan_path"] == "docs/plans/x.md"
    finally:
        _reset()


def test_link_plan_missing_item_errors(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "link-plan", "nope", "docs/plans/x.md"])
        assert res.exit_code == 1
        assert "no work item" in res.output
    finally:
        _reset()


def test_link_url_with_invalid_provider_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()
        res = runner.invoke(app, ["item", "link-url", item_id, "https://x", "--provider", "slack"])
        assert res.exit_code == 1
        assert "invalid provider" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_item_archive_hides_from_list_but_all_shows_it(tmp_path):
    # No task references the item, so archive proceeds without --force.
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()

        res = runner.invoke(app, ["item", "archive", item_id])
        assert res.exit_code == 0, res.output

        res = runner.invoke(app, ["--json", "item", "list"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.output) == []

        res = runner.invoke(app, ["--json", "item", "list", "--all"])
        assert res.exit_code == 0, res.output
        rows = json.loads(res.output)
        assert [r["id"] for r in rows] == [item_id]
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_item_archive_refused_when_live_task_linked_unless_forced(tmp_path):
    # A task still present in state.tasks (close removes it entirely) is "live" by
    # definition — archiving must be refused unless --force.
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()

        sm = StateManager(tmp_path / ".mothership")
        sm.mutate(lambda s: s.tasks.__setitem__("t-1", Task(
            slug="t-1", description="d", phase="dev", created_at=_NOW,
            affected_repos=["mothership"], branch="feat/t-1")))
        assert runner.invoke(app, ["item", "link-task", item_id, "t-1"]).exit_code == 0

        res = runner.invoke(app, ["item", "archive", item_id])
        assert res.exit_code != 0
        assert "t-1" in res.output
        assert "--force" in res.output

        # The item must still be live (not archived) after the refusal.
        res = runner.invoke(app, ["--json", "item", "list"])
        assert [r["id"] for r in json.loads(res.output)] == [item_id]

        res = runner.invoke(app, ["item", "archive", item_id, "--force"])
        assert res.exit_code == 0, res.output

        res = runner.invoke(app, ["--json", "item", "list"])
        assert json.loads(res.output) == []
    finally:
        _reset()


def test_item_archive_refused_for_forward_linked_task_with_stale_reverse_link(tmp_path):
    """MOS-228 fix: the guard must also catch a live task attached via the item's
    OWN forward link (task_slugs) even when the task's reverse link
    (task.work_item_id) is missing/stale — the two links can drift apart, and
    checking only the reverse link let a still-live task slip through."""
    from mship.core.workitem_store import WorkItemStore

    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()

        sm = StateManager(tmp_path / ".mothership")
        sm.mutate(lambda s: s.tasks.__setitem__("t-1", Task(
            slug="t-1", description="d", phase="dev", created_at=_NOW,
            affected_repos=["mothership"], branch="feat/t-1")))

        # Forward-link only: add_task with no `state` arg leaves the task's own
        # work_item_id untouched, simulating a reverse link that drifted stale.
        items = WorkItemStore(tmp_path / ".mothership" / "workitems")
        items.add_task(item_id, "t-1", now=_NOW)
        assert sm.load().tasks["t-1"].work_item_id is None

        res = runner.invoke(app, ["item", "archive", item_id])
        assert res.exit_code != 0
        assert "t-1" in res.output
        assert "--force" in res.output

        # The item must still be live (not archived) after the refusal.
        res = runner.invoke(app, ["--json", "item", "list"])
        assert [r["id"] for r in json.loads(res.output)] == [item_id]

        res = runner.invoke(app, ["item", "archive", item_id, "--force"])
        assert res.exit_code == 0, res.output

        res = runner.invoke(app, ["--json", "item", "list"])
        assert json.loads(res.output) == []
    finally:
        _reset()


def test_item_unarchive_restores_to_default_list(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "new", "Title", "--kind", "feature"])
        assert res.exit_code == 0, res.output
        item_id = res.output.strip()

        assert runner.invoke(app, ["item", "archive", item_id]).exit_code == 0
        res = runner.invoke(app, ["--json", "item", "list"])
        assert json.loads(res.output) == []

        res = runner.invoke(app, ["item", "unarchive", item_id])
        assert res.exit_code == 0, res.output

        res = runner.invoke(app, ["--json", "item", "list"])
        assert [r["id"] for r in json.loads(res.output)] == [item_id]
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_item_archive_unknown_id_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "archive", "wi-does-not-exist"])
        assert res.exit_code != 0
        assert "wi-does-not-exist" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()


def test_item_unarchive_unknown_id_errors_cleanly(tmp_path):
    _isolate(tmp_path)
    try:
        res = runner.invoke(app, ["item", "unarchive", "wi-does-not-exist"])
        assert res.exit_code != 0
        assert "wi-does-not-exist" in res.output
    finally:
        container.config_path.reset_override()
        container.state_dir.reset_override()
        container.config.reset()
