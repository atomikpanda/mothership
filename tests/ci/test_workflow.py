# tests/ci/test_workflow.py
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "version-bump.yml"


def _load():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_exists():
    assert WORKFLOW.is_file()


def test_triggers_on_pr_closed():
    wf = _load()
    # PyYAML parses the bare `on:` key as the boolean True.
    on = wf.get("on", wf.get(True))
    assert on["pull_request"]["types"] == ["closed"]


def test_job_guarded_to_merged_into_main():
    wf = _load()
    job = next(iter(wf["jobs"].values()))
    guard = job["if"]
    assert "merged == true" in guard
    assert "base.ref == 'main'" in guard


def test_has_write_permission_and_concurrency():
    wf = _load()
    assert wf["permissions"]["contents"] == "write"
    assert "concurrency" in wf


def test_bump_commit_uses_skip_ci_and_tags():
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "[skip ci]" in raw
    assert "python -m mship.ci.version_bump" in raw
    assert "git tag" in raw


def test_labels_passed_via_env_not_interpolated_into_shell():
    # Guard against script injection: the labels expression must be assigned to
    # an env var and referenced as a shell variable, never interpolated straight
    # into the run command (Greptile P1 security).
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "PR_LABELS:" in raw
    assert '--labels "$PR_LABELS"' in raw
    assert '--labels "${{' not in raw
