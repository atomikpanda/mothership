from datetime import datetime, timezone

from mship.core.focus import FocusState, focus_path, read_focus, write_focus, FOCUS_FILENAME


def test_focus_path_is_under_state_dir(tmp_path):
    assert focus_path(tmp_path) == tmp_path / FOCUS_FILENAME


def test_write_then_read_roundtrips(tmp_path):
    p = focus_path(tmp_path)
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    written = write_focus(p, "wi-1", now=now)
    assert written == FocusState(work_item_id="wi-1", updated_at=now)
    assert read_focus(p) == FocusState(work_item_id="wi-1", updated_at=now)


def test_write_creates_parent_dirs(tmp_path):
    p = tmp_path / "nested" / FOCUS_FILENAME
    write_focus(p, "wi-2")
    assert read_focus(p).work_item_id == "wi-2"


def test_read_missing_file_is_none(tmp_path):
    assert read_focus(focus_path(tmp_path)) is None


def test_read_corrupt_json_is_none(tmp_path):
    p = focus_path(tmp_path)
    p.write_text("{ not json")
    assert read_focus(p) is None


def test_read_missing_key_is_none(tmp_path):
    p = focus_path(tmp_path)
    p.write_text('{"updated_at": "2026-07-21T00:00:00+00:00"}')
    assert read_focus(p) is None


def test_write_defaults_now_to_utc(tmp_path):
    before = datetime.now(timezone.utc)
    s = write_focus(focus_path(tmp_path), "wi-3")
    assert s.updated_at >= before
    assert s.updated_at.tzinfo is not None
