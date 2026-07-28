"""Emit derives everything live: plan slice from the plan file, acceptance
text from the spec store. Editing either changes the next emit; the record
never contains them (spec ac2/ac3/ac8)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core.dispatch_emit import PlanDriftWarning, build_emitted_prompt
from mship.core.spec import AcceptanceCriterion, Spec
from mship.core.spec_body import render_body
from tests.core.test_sdd_store import _record

PLAN = (
    "# Plan\n\n"
    "<!-- mship:" "task id=1 acs=ac1 -->\n"
    "### Task 1\n\n"
    "the anchored body\n"
    "<!-- /mship:" "task -->\n"
)


def _spec() -> Spec:
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    return Spec(
        id="sp1", title="Emit spec", status="approved",
        created_at=now, updated_at=now, affected_repos=["mothership"],
        body=render_body("the problem", "as a user", "the approach"),
        acceptance_criteria=[
            AcceptanceCriterion(id="ac1", text="prompts render live plan text"),
        ],
    )


@dataclass
class _Ws:
    root: Path
    plan_path: Path
    spec: Spec | None
    record: object


@pytest.fixture
def ws(tmp_path: Path) -> _Ws:
    plan_path = tmp_path / "docs" / "plans" / "plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(PLAN)
    record = _record(
        plan_path="docs/plans/plan.md", plan_task_id="1", acs=["ac1"],
        worktree=str(tmp_path / "wt"),
        # Exactly the plan's mtime: no spurious drift (file timestamps come
        # from the kernel's coarse clock, which lags datetime.now()).
        created_at=datetime.fromtimestamp(plan_path.stat().st_mtime, tz=timezone.utc),
    )
    return _Ws(root=tmp_path, plan_path=plan_path, spec=_spec(), record=record)


@pytest.fixture
def ws_adhoc(tmp_path: Path) -> _Ws:
    record = _record(
        plan_path=None, plan_task_id=None, acs=[],
        instruction="wire the ad-hoc thing",
        worktree=str(tmp_path / "wt"),
        created_at=datetime.now(timezone.utc),
    )
    return _Ws(root=tmp_path, plan_path=tmp_path / "unused.md", spec=None, record=record)


def test_emit_contains_plan_body_and_ac_text(ws):
    prompt, warnings = build_emitted_prompt(ws.record, workspace_root=ws.root, spec=ws.spec)
    assert "the anchored body" in prompt          # from the plan file
    assert "[ac1]" in prompt and ws.spec.acceptance_criteria[0].text in prompt
    assert f"**Model:** {ws.record.model}" in prompt


def test_emit_reflects_plan_edit_without_touching_store(ws):
    ws.plan_path.write_text(ws.plan_path.read_text().replace("the anchored body", "EDITED BODY"))
    prompt, _ = build_emitted_prompt(ws.record, workspace_root=ws.root, spec=ws.spec)
    assert "EDITED BODY" in prompt


def test_emit_warns_when_plan_newer_than_record(ws):
    t = time.time() + 60  # deterministic mtime > record.created_at (touch()
    os.utime(ws.plan_path, (t, t))  # can lag on the coarse kernel clock)
    _, warnings = build_emitted_prompt(ws.record, workspace_root=ws.root, spec=ws.spec)
    assert any(isinstance(w, PlanDriftWarning) for w in warnings)


def test_emit_ad_hoc_instruction_record(ws_adhoc):
    prompt, _ = build_emitted_prompt(ws_adhoc.record, workspace_root=ws_adhoc.root, spec=None)
    assert ws_adhoc.record.instruction in prompt


def test_emit_unknown_ac_id_warns_not_errors(ws):
    # The plan anchor's acs= is authoritative at emit time; put the unknown
    # id there, not on the record.
    ws.plan_path.write_text(ws.plan_path.read_text().replace("acs=ac1", "acs=ac1,ac9"))
    prompt, warnings = build_emitted_prompt(ws.record, workspace_root=ws.root, spec=ws.spec)
    assert "[ac1]" in prompt
    assert any("ac9" in str(w) for w in warnings)


def test_emit_acs_without_spec_warns(ws):
    _, warnings = build_emitted_prompt(ws.record, workspace_root=ws.root, spec=None)
    assert any("ac1" in str(w) for w in warnings)
