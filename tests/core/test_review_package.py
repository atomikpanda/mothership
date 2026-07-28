"""Reviewer packages: manifest JSON + raw diff files. The reviewer READS the
diff from disk; the prompt references paths and never embeds diff content
(spec ac4). One reviewer returns both spec-compliance and quality verdicts
(upstream 6.2.0 task-reviewer contract)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from mship.core.review_package import build_review_package, build_reviewer_prompt
from mship.core.sdd_store import DispatchRecord
from mship.util.shell import ShellRunner
from tests.core.test_sdd_store import _record


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return r.stdout.strip()


@dataclass
class _Ws:
    record: DispatchRecord
    shell: object  # callable(cmd, cwd=...) -> result with .stdout
    state_dir: Path


@pytest.fixture
def ws(tmp_path: Path) -> _Ws:
    """A real git repo (base commit + 2 commits past base), a DispatchRecord
    pointing at it, and a shell runner with the container.shell().run
    contract."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("change one\n")
    _git(repo, "commit", "-q", "-am", "one")
    (repo / "b.txt").write_text("new file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    head_sha = _git(repo, "rev-parse", "HEAD")
    record = _record(worktree=str(repo), base_sha=base_sha, head_sha=head_sha)
    return _Ws(record=record, shell=ShellRunner().run, state_dir=tmp_path / ".mothership")


def test_package_writes_manifest_and_diff_files(ws):
    pkg = build_review_package(ws.record, git_runner=ws.shell, state_dir=ws.state_dir)
    assert pkg.manifest_path.name == "manifest.json"
    assert pkg.diff_paths and all(p.exists() for p in pkg.diff_paths)
    assert "diff --git" in pkg.diff_paths[0].read_text()  # the raw diff landed
    raw = pkg.manifest_path.read_text()
    assert '"diff_files"' in raw and '"acs"' in raw
    assert "diff --git" not in raw               # manifest is metadata, not content


def test_reviewer_prompt_references_paths_not_content(ws):
    pkg = build_review_package(ws.record, git_runner=ws.shell, state_dir=ws.state_dir)
    prompt = build_reviewer_prompt(ws.record, pkg, acceptance=[("ac1", "does the thing")])
    assert str(pkg.diff_paths[0]) in prompt
    assert "diff --git" not in prompt             # never embedded
    assert "spec-compliance" in prompt.lower() and "quality" in prompt.lower()
    assert "[ac1] does the thing" in prompt
    # Content-absence: the manifest carries AC IDs, never the AC prose.
    assert "does the thing" not in pkg.manifest_path.read_text()


def test_load_package_rejects_manifest_missing_diff_files(ws):
    from mship.core.review_package import load_review_package, review_dir

    d = review_dir(ws.record, ws.state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text('{"task_slug": "my-task"}\n')  # valid JSON, no diff_files
    with pytest.raises(ValueError, match="manifest is corrupt"):
        load_review_package(ws.record, ws.state_dir)


def test_manifest_head_sha_matches_the_diffed_head(ws):
    """The diff runs to live HEAD, which may have moved past the dispatch-time
    rec.head_sha — the manifest must describe the diff it sits beside."""
    import json

    live_head = _git(Path(ws.record.worktree), "rev-parse", "HEAD")
    stale = ws.record.model_copy(update={"head_sha": "deadbee"})
    pkg = build_review_package(stale, git_runner=ws.shell, state_dir=ws.state_dir)
    manifest = json.loads(pkg.manifest_path.read_text())
    assert manifest["head_sha"] == live_head


def test_reviewer_mode_is_dispatchable():
    from mship.core.dispatch import DISPATCH_MODES
    assert "reviewer" in DISPATCH_MODES
