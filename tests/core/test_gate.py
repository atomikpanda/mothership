import json
from pathlib import Path

from mship.core.gate import resolve_bypass, record_bypass, no_task_notice, NO_TASK_NOTICE


def test_resolve_bypass_unset(monkeypatch):
    monkeypatch.delenv("MSHIP_BYPASS_GATE", raising=False)
    assert resolve_bypass() == (False, "")


def test_resolve_bypass_bare(monkeypatch):
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "1")
    assert resolve_bypass() == (True, "")


def test_resolve_bypass_with_reason(monkeypatch):
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "quick hotfix")
    assert resolve_bypass() == (True, "quick hotfix")


def test_record_bypass_appends_jsonl(tmp_path):
    ws = tmp_path
    (ws / ".mothership").mkdir()
    record_bypass(ws, op="commit", branch="feat/x", reason="r")
    log = ws / ".mothership" / "bypass-log.jsonl"
    line = json.loads(log.read_text().splitlines()[-1])
    assert line["op"] == "commit" and line["branch"] == "feat/x" and line["reason"] == "r"
    assert "ts" in line and "cwd" in line
    record_bypass(ws, op="push", branch="feat/y", reason="r2")
    assert len((ws / ".mothership" / "bypass-log.jsonl").read_text().splitlines()) == 2


def _ws_with(tmp_path, tasks: bool):
    ws = tmp_path / "ws"; ws.mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
    )
    (ws / "lib").mkdir(); (ws / "lib" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    sd = ws / ".mothership"; sd.mkdir()
    if tasks:
        (sd / "state.yaml").write_text(
            "tasks:\n  t:\n    slug: t\n    description: d\n    phase: dev\n"
            "    created_at: 2026-01-01T00:00:00+00:00\n    affected_repos: [lib]\n"
            "    branch: feat/t\n"
        )
    return ws


def test_no_task_notice_workspace_no_task(tmp_path):
    ws = _ws_with(tmp_path, tasks=False)
    assert no_task_notice(ws) == NO_TASK_NOTICE


def test_no_task_notice_with_task_is_none(tmp_path):
    ws = _ws_with(tmp_path, tasks=True)
    assert no_task_notice(ws) is None


def test_no_task_notice_outside_workspace_is_none(tmp_path):
    assert no_task_notice(tmp_path) is None


def test_resolve_bypass_zero_is_off(monkeypatch):
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "0")
    assert resolve_bypass() == (False, "")
    monkeypatch.setenv("MSHIP_BYPASS_GATE", "false")
    assert resolve_bypass() == (False, "")


def test_no_task_notice_finished_task_still_notifies(tmp_path):
    ws = tmp_path / "wsf"; ws.mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
    )
    (ws / "lib").mkdir(); (ws / "lib" / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    sd = ws / ".mothership"; sd.mkdir()
    (sd / "state.yaml").write_text(
        "tasks:\n  t:\n    slug: t\n    description: d\n    phase: dev\n"
        "    created_at: 2026-01-01T00:00:00+00:00\n    affected_repos: [lib]\n"
        "    branch: feat/t\n    finished_at: 2026-01-02T00:00:00+00:00\n"
    )
    assert no_task_notice(ws) == NO_TASK_NOTICE
