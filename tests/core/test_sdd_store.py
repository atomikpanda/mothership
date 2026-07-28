from datetime import datetime, timezone
from pathlib import Path
from mship.core.sdd_store import DispatchRecord, SddStore

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

def _record(**over):
    base = dict(
        task_slug="my-task", work_item_id="wi-1", mode="implementer",
        model="sonnet", repo="api", worktree="/ws/.worktrees/my-task/api",
        base_branch="main", base_sha="a" * 7, head_sha="b" * 7,
        plan_path="docs/plans/2026-07-28-my-task.md", plan_task_id="3",
        acs=["ac2", "ac5"], instruction=None, created_at=NOW,
    )
    base.update(over)
    return DispatchRecord(**base)

def test_write_then_read_roundtrip(tmp_path):
    store = SddStore(tmp_path / ".mothership")
    store.write(_record())
    rec = store.read(work_item_id="wi-1", task_slug="my-task")
    assert rec.plan_task_id == "3" and rec.model == "sonnet" and rec.acs == ["ac2", "ac5"]

def test_record_dir_is_keyed_by_workitem_and_slug(tmp_path):
    store = SddStore(tmp_path / ".mothership")
    p = store.write(_record())
    assert p == tmp_path / ".mothership" / "sdd" / "wi-1" / "my-task" / "record.json"

def test_no_workitem_uses_no_item_key(tmp_path):
    store = SddStore(tmp_path / ".mothership")
    p = store.write(_record(work_item_id=None))
    assert p.parent.parent.name == "no-item"

def test_record_never_contains_plan_body(tmp_path):
    """The record is a pointer: plan prose must not be persisted (spec ac2)."""
    store = SddStore(tmp_path / ".mothership")
    p = store.write(_record())
    raw = p.read_text()
    assert "plan_path" in raw and "docs/plans" in raw
    for field in ("body", "task_text", "prompt", "template"):
        assert f'"{field}"' not in raw

def test_write_supersedes_record_under_other_workitem_key(tmp_path):
    """A slug has at most one live record: gaining a WorkItem after a no-item
    dispatch must not leave a stale no-item record shadowing the new one
    (find_for_slug returns the first sorted glob — "no-item" sorts before
    "wi-*")."""
    store = SddStore(tmp_path / ".mothership")
    store.write(_record(work_item_id=None, plan_task_id="1"))
    store.write(_record(work_item_id="wi-1", plan_task_id="2"))
    rec = store.find_for_slug("my-task")
    assert rec is not None and rec.work_item_id == "wi-1" and rec.plan_task_id == "2"
    assert not (tmp_path / ".mothership" / "sdd" / "no-item" / "my-task").exists()


def test_remove_task_removes_all_records_for_slug(tmp_path):
    store = SddStore(tmp_path / ".mothership")
    store.write(_record())
    store.write(_record(work_item_id=None))
    store.remove_task("my-task")
    assert not list((tmp_path / ".mothership" / "sdd").rglob("record.json"))
