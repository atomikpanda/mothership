"""Review-package builder (spec mship-dispatch-v2, ac4).

The ONE stored content in the sdd store: raw `git diff` output written as
files (generated artifacts, not duplicated prose) plus a metadata manifest.
Reading diffs from disk instead of pasting them is upstream superpowers'
measured token win (~2x faster, ~50% fewer review tokens in their evals).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mship.core.dispatch import _closing_section, _conventions_recap
from mship.core.sdd_store import DispatchRecord, SddStore


@dataclass
class ReviewPackage:
    manifest_path: Path
    diff_paths: list[Path]


def review_dir(rec: DispatchRecord, state_dir: Path) -> Path:
    """The review-package dir beside the record: `<record-dir>/review/`."""
    return SddStore(state_dir)._dir(rec.work_item_id, rec.task_slug) / "review"


def load_review_package(rec: DispatchRecord, state_dir: Path) -> ReviewPackage:
    """Reload a previously built package from its manifest (the emit path).

    Raises OSError when no package was built for this record, ValueError
    (incl. json.JSONDecodeError) when the manifest is corrupt.
    """
    manifest_path = review_dir(rec, state_dir) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or "diff_files" not in manifest:
        raise ValueError(
            "review package manifest is corrupt — re-run "
            "`mship dispatch --mode reviewer`"
        )
    return ReviewPackage(
        manifest_path=manifest_path,
        diff_paths=[Path(p) for p in manifest["diff_files"]],
    )


def build_review_package(
    rec: DispatchRecord,
    *,
    targets: list[tuple[str, str, str | None]],
    git_runner,
    state_dir: Path,
) -> ReviewPackage:
    """Write `<record-dir>/review/{manifest.json, <repo>.diff per target}`.

    `targets` is (repo, worktree, base_sha) for EVERY affected repo — a
    multi-repo task reviewed from only the dispatched repo's diff would
    present an incomplete review as complete. Diff range per target:
    `<base_sha>..HEAD` of that worktree (HEAD at dispatch time may have
    moved past the recorded head_sha; base_sha anchors the review range).
    An empty diff is still written — the CLI warns per empty file.
    `git_runner(cmd, cwd=...)` is the injected shell (same contract as
    container.shell().run) so tests use fixture repos.
    """
    if not targets:
        raise ValueError(
            f"no reviewable targets for task {rec.task_slug!r} — "
            f"cannot build a review package"
        )
    d = review_dir(rec, state_dir)
    d.mkdir(parents=True, exist_ok=True)
    diff_paths: list[Path] = []
    targets_meta: dict[str, dict] = {}
    for repo, worktree, base_sha in targets:
        if not base_sha:
            raise ValueError(
                f"no base_sha for repo {repo!r} — cannot compute its review "
                f"diff range"
            )
        res = git_runner(f"git diff {base_sha}..HEAD", cwd=Path(worktree))
        if res.returncode != 0:
            raise ValueError(
                f"git diff {base_sha}..HEAD failed in {worktree}: "
                f"{res.stderr.strip() or 'unknown error'}"
            )
        diff_file = d / f"{repo}.diff"
        diff_file.write_text(res.stdout)
        diff_paths.append(diff_file)
        # Each diff runs to that worktree's LIVE HEAD — the manifest must
        # describe the diff it sits beside, not the record's snapshot.
        head = git_runner("git rev-parse HEAD", cwd=Path(worktree))
        targets_meta[repo] = {
            "base_sha": base_sha,
            "head_sha": head.stdout.strip() if head.returncode == 0 else None,
        }
    manifest = {
        "task_slug": rec.task_slug, "work_item_id": rec.work_item_id,
        "plan_path": rec.plan_path, "plan_task_id": rec.plan_task_id,
        "acs": rec.acs, "base_sha": rec.base_sha,
        # Dispatched repo's live head (record snapshot when it wasn't diffed).
        "head_sha": targets_meta.get(rec.repo, {}).get("head_sha") or rec.head_sha,
        "targets": targets_meta,
        "diff_files": [str(p) for p in diff_paths],
    }
    manifest_path = d / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return ReviewPackage(manifest_path=manifest_path, diff_paths=diff_paths)


def build_reviewer_prompt(
    rec: DispatchRecord, pkg: ReviewPackage, *, acceptance: list
) -> str:
    """The full reviewer prompt: package paths + live ACs + the read-only
    dual-verdict contract (composed from the shared closing-section flow —
    the contract text lives once, in core.dispatch)."""
    ac_block = "\n".join(f"- [{ac_id}] {text}" for ac_id, text in acceptance) or "(none mapped)"
    files_block = "\n".join(f"- `{p}`" for p in pkg.diff_paths)
    closing_heading, closing_body = _closing_section("reviewer")
    return f"""\
# Review: task {rec.task_slug} (plan task {rec.plan_task_id or 'ad-hoc'})

**Model:** {rec.model}

## Diff files to read (on disk — read, don't paste)

{files_block}

Manifest: `{pkg.manifest_path}`

## Acceptance criteria to check (live from the spec store)

{ac_block}

## Conventions (recap)

{_conventions_recap("reviewer")}

## {closing_heading}

{closing_body}"""
