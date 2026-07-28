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


def _targets(rec: DispatchRecord) -> list[tuple[str, str, str | None]]:
    """The single-repo target list for a record (repo, worktree, base_sha)."""
    return [(rec.repo, rec.worktree, rec.base_sha)]


def _make_repo(path: Path, marker: str) -> tuple[str, str]:
    """Init a git repo with a base commit + one commit; return (base, head) SHAs."""
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text(f"{marker} base\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    (path / "a.txt").write_text(f"{marker} changed\n")
    _git(path, "commit", "-q", "-am", "work")
    return base, _git(path, "rev-parse", "HEAD")


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
    pkg = build_review_package(ws.record, targets=_targets(ws.record), git_runner=ws.shell, state_dir=ws.state_dir)
    assert pkg.manifest_path.name == "manifest.json"
    assert pkg.diff_paths and all(p.exists() for p in pkg.diff_paths)
    assert "diff --git" in pkg.diff_paths[0].read_text()  # the raw diff landed
    raw = pkg.manifest_path.read_text()
    assert '"diff_files"' in raw and '"acs"' in raw
    assert "diff --git" not in raw               # manifest is metadata, not content


def test_reviewer_prompt_references_paths_not_content(ws):
    pkg = build_review_package(ws.record, targets=_targets(ws.record), git_runner=ws.shell, state_dir=ws.state_dir)
    prompt = build_reviewer_prompt(ws.record, pkg, acceptance=[("ac1", "does the thing")])
    assert str(pkg.diff_paths[0]) in prompt
    assert "diff --git" not in prompt             # never embedded
    assert "spec-compliance" in prompt.lower() and "quality" in prompt.lower()
    assert "[ac1] does the thing" in prompt
    assert "NOT included" not in prompt           # no skips -> no omission section


def test_skipped_repos_are_disclosed_in_manifest_and_prompt(ws):
    """An omitted affected repo must be first-class in the artifact — a
    verdict over a partial package that doesn't say so implies full
    coverage (PR #439, Greptile 3/5)."""
    import json

    pkg = build_review_package(
        ws.record, targets=_targets(ws.record),
        skipped={"web": "worktree missing (/gone/web)"},
        git_runner=ws.shell, state_dir=ws.state_dir,
    )
    manifest = json.loads(pkg.manifest_path.read_text())
    assert manifest["skipped"] == {"web": "worktree missing (/gone/web)"}
    prompt = build_reviewer_prompt(ws.record, pkg, acceptance=[])
    assert "NOT included" in prompt
    assert "web" in prompt and "worktree missing" in prompt
    assert "can't-tell" in prompt
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
    pkg = build_review_package(stale, targets=_targets(stale), git_runner=ws.shell, state_dir=ws.state_dir)
    manifest = json.loads(pkg.manifest_path.read_text())
    assert manifest["head_sha"] == live_head


def test_reviewer_mode_is_dispatchable():
    from mship.core.dispatch import DISPATCH_MODES
    assert "reviewer" in DISPATCH_MODES


def test_package_covers_every_target_repo(tmp_path: Path):
    """A task spanning multiple repos gets one .diff per repo, all listed in
    the manifest — a single-repo package would present an incomplete review
    as complete (PR #439 P1)."""
    import json

    api_base, _ = _make_repo(tmp_path / "api", "api")
    web_base, web_head = _make_repo(tmp_path / "web", "web")
    rec = _record(worktree=str(tmp_path / "api"), base_sha=api_base)
    pkg = build_review_package(
        rec,
        targets=[
            ("api", str(tmp_path / "api"), api_base),
            ("web", str(tmp_path / "web"), web_base),
        ],
        git_runner=ShellRunner().run,
        state_dir=tmp_path / ".mothership",
    )
    assert sorted(p.name for p in pkg.diff_paths) == ["api.diff", "web.diff"]
    assert all(p.exists() and "diff --git" in p.read_text() for p in pkg.diff_paths)
    manifest = json.loads(pkg.manifest_path.read_text())
    assert sorted(Path(f).name for f in manifest["diff_files"]) == ["api.diff", "web.diff"]
    assert manifest["targets"]["web"] == {"base_sha": web_base, "head_sha": web_head}


def test_git_root_child_diff_is_scoped_and_never_duplicated(tmp_path: Path):
    """A git_root child shares its parent's checkout — its diff must cover
    only its subtree (not the parent's whole diff mislabeled), and the
    parent's diff must exclude the co-targeted child so no hunk appears in
    both files (PR #439 P1 follow-up)."""
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "sub").mkdir(parents=True)
    _git(parent, "init", "-q")
    _git(parent, "config", "user.email", "t@t")
    _git(parent, "config", "user.name", "t")
    (parent / "root.txt").write_text("base\n")
    (parent / "sub" / "child.txt").write_text("base\n")
    _git(parent, "add", "-A")
    _git(parent, "commit", "-q", "-m", "base")
    base = _git(parent, "rev-parse", "HEAD")
    (parent / "root.txt").write_text("changed\n")
    (parent / "sub" / "child.txt").write_text("changed\n")
    _git(parent, "commit", "-q", "-am", "work")

    rec = _record(worktree=str(parent), base_sha=base, repo="parent")
    pkg = build_review_package(
        rec,
        targets=[
            ("parent", str(parent), base),
            ("child", str(parent / "sub"), base),
        ],
        excludes={"parent": ["sub"]},
        git_runner=ShellRunner().run,
        state_dir=tmp_path / ".mothership",
    )
    diffs = {p.name: p.read_text() for p in pkg.diff_paths}
    assert "sub/child.txt" in diffs["child.diff"]        # child subtree hunks
    assert "root.txt" not in diffs["child.diff"]         # ...and nothing else
    assert "root.txt" in diffs["parent.diff"]            # parent keeps the rest
    assert "sub/child.txt" not in diffs["parent.diff"]   # no hunk in both files


def test_package_requires_at_least_one_target(ws):
    with pytest.raises(ValueError, match="target"):
        build_review_package(
            ws.record, targets=[], git_runner=ws.shell, state_dir=ws.state_dir
        )
