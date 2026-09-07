import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from mship.core.persistence.export import export_state
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem import WorkItem
from mship.core.workitem_store import WorkItemStore


NOW = datetime(2026, 9, 7, 18, 0, tzinfo=timezone.utc)


def _seed_sqlite_state(state_dir: Path) -> None:
    tasks = {
        slug: Task(
            slug=slug,
            description=f"Task {slug}",
            phase="dev",
            created_at=NOW,
            affected_repos=["mothership"],
            branch=f"feat/{slug}",
        )
        for slug in ("zeta", "alpha")
    }
    StateManager(state_dir).save(WorkspaceState(tasks=tasks))
    store = WorkItemStore(state_dir / "workitems")
    store.save(
        WorkItem(
            id="wi-zeta",
            title="Zeta",
            workspace="test",
            kind="chore",
            created_at=NOW,
            updated_at=NOW,
            archived=True,
        )
    )
    store.save(
        WorkItem(
            id="wi-alpha",
            title="Alpha",
            workspace="test",
            kind="feature",
            created_at=NOW,
            updated_at=NOW,
        )
    )


def test_export_json_is_deterministic_sorted_and_scoped(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    _seed_sqlite_state(state_dir)
    (state_dir / "messages").mkdir()
    (state_dir / "messages" / "private.json").write_text("MESSAGE_SENTINEL")
    (state_dir / "artifacts").mkdir()
    (state_dir / "artifacts" / "secret.bin").write_bytes(b"ARTIFACT_SENTINEL")

    first = export_state(state_dir, format="json")
    second = export_state(state_dir, format="json")
    payload = json.loads(first)

    assert first == second
    assert [task["slug"] for task in payload["tasks"]] == ["alpha", "zeta"]
    assert [item["id"] for item in payload["work_items"]] == [
        "wi-alpha",
        "wi-zeta",
    ]
    assert payload["work_items"][1]["archived"] is True
    assert "MESSAGE_SENTINEL" not in first
    assert "ARTIFACT_SENTINEL" not in first


def test_export_yaml_has_same_payload_as_json(tmp_path: Path) -> None:
    state_dir = tmp_path / ".mothership"
    _seed_sqlite_state(state_dir)

    json_payload = json.loads(export_state(state_dir, format="json"))
    yaml_payload = yaml.safe_load(export_state(state_dir, format="yaml"))

    assert yaml_payload == json_payload


def test_export_rejects_unsupported_format(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="json or yaml"):
        export_state(tmp_path / ".mothership", format="toml")
