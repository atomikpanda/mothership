from datetime import datetime, timezone

from mship.core.state import Task


def test_task_work_item_id_defaults_none_and_legacy_loads():
    legacy = ('{"slug":"s1","description":"d","phase":"dev",'
              '"created_at":"2026-06-30T12:00:00+00:00","affected_repos":["mothership"],'
              '"branch":"b"}')
    task = Task.model_validate_json(legacy)
    assert task.work_item_id is None
