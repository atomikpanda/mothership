"""Start history is the OS-agnostic crash-loop detector's data: identical under
systemd, launchd, and `daemon run`. Only UNCLEAN starts (not preceded by a
clean stop) count toward a loop, so routine operator restarts never cry wolf."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mship.core.daemon.history import (
    HistoryEntry,
    append_clean_stop,
    append_start,
    is_crash_looping,
    read_history,
    unclean_start_count,
)

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def _t(seconds_ago: float) -> datetime:
    return NOW - timedelta(seconds=seconds_ago)


def test_append_and_read_roundtrip(tmp_path: Path):
    path = tmp_path / "start-history.json"
    append_start(path, _t(30))
    append_clean_stop(path, _t(20))
    append_start(path, _t(10))
    entries = read_history(path)
    assert [e.kind for e in entries] == ["start", "clean_stop", "start"]
    assert entries[0].at == _t(30)


def test_history_trims_to_last_20(tmp_path: Path):
    path = tmp_path / "h.json"
    for i in range(25):
        append_start(path, _t(1000 - i))
    assert len(read_history(path)) == 20


def test_corrupt_file_starts_fresh(tmp_path: Path):
    path = tmp_path / "h.json"
    path.write_text("{not json")
    assert read_history(path) == []
    append_start(path, NOW)
    assert len(read_history(path)) == 1


def test_missing_file_reads_empty(tmp_path: Path):
    assert read_history(tmp_path / "absent.json") == []


def test_under_threshold_not_looping():
    entries = [HistoryEntry("start", _t(100)), HistoryEntry("start", _t(50))]
    assert not is_crash_looping(entries, NOW, window_s=600, threshold=3)


def test_threshold_unclean_starts_in_window_loops():
    entries = [HistoryEntry("start", _t(300)), HistoryEntry("start", _t(200)), HistoryEntry("start", _t(100))]
    assert is_crash_looping(entries, NOW, window_s=600, threshold=3)


def test_old_entries_outside_window_ignored():
    entries = [
        HistoryEntry("start", _t(5000)),
        HistoryEntry("start", _t(4000)),
        HistoryEntry("start", _t(100)),
    ]
    assert not is_crash_looping(entries, NOW, window_s=600, threshold=3)
    assert unclean_start_count(entries, NOW, window_s=600) == 1


def test_operator_restarts_do_not_cry_loop():
    """start/stop/start/stop/start inside the window: every restart is clean."""
    entries = [
        HistoryEntry("start", _t(500)),
        HistoryEntry("clean_stop", _t(400)),
        HistoryEntry("start", _t(300)),
        HistoryEntry("clean_stop", _t(200)),
        HistoryEntry("start", _t(100)),
    ]
    assert not is_crash_looping(entries, NOW, window_s=600, threshold=3)
    # Only the first start is unclean (no preceding clean stop).
    assert unclean_start_count(entries, NOW, window_s=600) == 1
