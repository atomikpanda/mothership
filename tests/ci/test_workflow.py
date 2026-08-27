# tests/ci/test_workflow.py
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "version-bump.yml"
TEST_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "test.yml"


def _load(workflow=WORKFLOW):
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))


def test_workflow_exists():
    assert WORKFLOW.is_file()


def test_triggers_on_pull_request_target_closed_not_pull_request():
    wf = _load()
    # PyYAML parses the bare `on:` key as the boolean True.
    on = wf.get("on", wf.get(True))

    assert on["pull_request_target"]["types"] == ["closed"]
    assert "pull_request" not in on


def test_job_guarded_to_merged_into_main():
    wf = _load()
    job = next(iter(wf["jobs"].values()))
    guard = job["if"]
    assert "merged == true" in guard
    assert "base.ref == 'main'" in guard


def test_has_required_permissions_and_concurrency():
    wf = _load()
    assert wf["permissions"]["contents"] == "write"
    assert wf["permissions"]["actions"] == "write"
    assert "concurrency" in wf

def test_bump_job_checks_out_only_trusted_main():
    steps = next(iter(_load()["jobs"].values()))["steps"]
    checkout = next(step for step in steps if step["uses"] == "actions/checkout@v4")

    assert checkout["with"]["ref"] == "main"
    assert "github.event.pull_request.head" not in WORKFLOW.read_text(encoding="utf-8")


def test_bump_commit_runs_ci_and_tags():
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "[skip ci]" not in raw
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


def test_version_bump_regenerates_and_commits_lockfile():
    steps = next(iter(_load()["jobs"].values()))["steps"]
    bump_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Compute and apply version bump"
    )
    lock_index, lock_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Regenerate lockfile"
    )
    commit_step = next(step for step in steps if step.get("name") == "Commit, tag, and push")

    assert bump_index < lock_index
    assert lock_step["run"].strip() == "uv lock"
    assert "git add pyproject.toml src/mship/__init__.py uv.lock" in commit_step["run"]

def test_version_bump_runs_canonical_suite_before_publishing():
    steps = next(iter(_load()["jobs"].values()))["steps"]
    lock_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Regenerate lockfile"
    )
    task_setup_index, task_setup = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("uses") == "arduino/setup-task@v2"
    )
    identity_index, identity_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Give git an identity"
    )
    suite_index, suite_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Run bumped tree suite"
    )
    commit_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Commit, tag, and push"
    )

    assert task_setup["with"]["repo-token"] == "${{ secrets.GITHUB_TOKEN }}"
    assert 'git config --global user.name "mship ci"' in identity_step["run"]
    assert 'git config --global user.email "ci@example.invalid"' in identity_step["run"]
    assert suite_step["run"].strip() == "task test"
    assert lock_index < task_setup_index < identity_index < suite_index < commit_index


def test_pr_workflow_checks_lockfile_before_running_suite():
    steps = next(iter(_load(TEST_WORKFLOW)["jobs"].values()))["steps"]
    lock_index, lock_step = next(
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Verify lockfile is current"
    )
    suite_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Run the suite"
    )

    assert lock_index < suite_index
    assert lock_step["run"].strip() == "uv lock --check"


def test_test_workflow_supports_explicit_dispatch():
    wf = _load(TEST_WORKFLOW)
    on = wf.get("on", wf.get(True))

    assert "workflow_dispatch" in on


def test_version_bump_dispatches_tests_for_new_version_tag_after_pushing():
    steps = next(iter(_load()["jobs"].values()))["steps"]
    commit_step = next(step for step in steps if step.get("name") == "Commit, tag, and push")
    run = commit_step["run"]
    tag = 'git tag -a "v${{ steps.bump.outputs.new_version }}"'
    commit_sha = 'BUMP_SHA="$(git rev-parse HEAD)"'
    dispatch = 'gh workflow run test.yml --ref "$TAG"'

    assert commit_step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert (
        run.index('git commit -m "chore: bump version')
        < run.index(commit_sha)
        < run.index(tag)
        < run.index("git push origin main --follow-tags")
        < run.index(dispatch)
    )
    assert "gh workflow run test.yml --ref main" not in run


def test_version_bump_waits_for_the_dispatched_tag_ci_run():
    steps = next(iter(_load()["jobs"].values()))["steps"]
    commit_step = next(step for step in steps if step.get("name") == "Commit, tag, and push")
    run = commit_step["run"]
    dispatch = 'gh workflow run test.yml --ref "$TAG"'
    lookup = (
        'gh run list --workflow test.yml --commit "$BUMP_SHA" --event workflow_dispatch '
        "--limit 1 --json databaseId --jq '.[0].databaseId // empty'"
    )
    watch = 'gh run watch "$RUN_ID" --exit-status'

    assert 'TAG="v${{ steps.bump.outputs.new_version }}"' in run
    assert dispatch in run
    assert lookup in run
    assert "for attempt in 1 2 3 4 5; do" in run
    assert 'sleep "$attempt"' in run
    assert '[ -z "$RUN_ID" ]' in run
    assert "exit 1" in run
    assert watch in run
    assert '--branch "$TAG"' not in run
    assert run.index(dispatch) < run.index(lookup) < run.index('[ -z "$RUN_ID" ]') < run.index(watch)
