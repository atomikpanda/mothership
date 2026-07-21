from datetime import datetime, timezone

from mship.cli import container
from mship.cli.view._follow import follow_hint, read_focused_id
from mship.core.focus import focus_path, write_focus


def _bind_state_dir(tmp_path):
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    container.state_dir.override(state_dir)
    return state_dir


def _unbind():
    container.state_dir.reset_override()


def test_read_focused_id_none_when_no_file(tmp_path):
    _bind_state_dir(tmp_path)
    try:
        assert read_focused_id(container) is None
    finally:
        _unbind()


def test_read_focused_id_returns_written_id(tmp_path):
    state_dir = _bind_state_dir(tmp_path)
    try:
        write_focus(focus_path(state_dir), "wi-42",
                    now=datetime(2026, 7, 21, tzinfo=timezone.utc))
        assert read_focused_id(container) == "wi-42"
    finally:
        _unbind()


def test_follow_hint_mentions_focus_and_is_not_empty():
    hint = follow_hint()
    assert hint.strip()
    assert "focus" in hint.lower()
