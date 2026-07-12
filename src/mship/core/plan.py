"""Resolve + validate a task's implementation plan doc (see MOS-235).

Centralizes plan resolution so both `mship export` (bundle assembly) and the
work-item plan gate share one convention: a `docs/plans/*.md` file whose stem
precisely matches the task slug, or an explicit `plan_path`. Validity is
structural — a plan is "real" if it carries at least one `<!-- mship:task -->`
anchor — not a content/approval check.
"""
from __future__ import annotations

import re
from pathlib import Path

_TASK_ANCHOR_RE = re.compile(r"<!--\s*mship:task\s+id=([^\s>]+)\s*-->")


def _plan_stem_matches_slug(stem: str, task_slug: str) -> bool:
    """Precise match, not substring: either the stem IS the slug, or it's the
    canonical `<YYYY-MM-DD>-<slug>.md` form. A bare `-<slug>` suffix (e.g.
    'my-add.md' for slug 'add') deliberately does NOT match — that's still a
    substring-style match in disguise, just anchored to the end instead of
    anywhere, and would wrongly pick up unrelated docs prefixed with
    another word (MOS-102 Greptile fix)."""
    if stem == task_slug:
        return True
    escaped_slug = re.escape(task_slug)
    return re.fullmatch(rf"\d{{4}}-\d{{2}}-\d{{2}}-{escaped_slug}", stem) is not None


def discover_plan_path(workspace_root: Path, task_slug: str, docs_dir: str = "docs") -> Path | None:
    """Best-effort plan-doc discovery: a `docs/plans/*.md` file whose stem
    precisely matches the task slug (exact stem, or the canonical
    `<date>-<slug>.md` form) — not merely containing the slug as a
    substring, which would false-positive on a short slug like "add"
    matching "add-labels.md". v1 has no fuzzy/similarity scoring and no
    explicit plan_path reference on Task — picks the most-recently-modified
    match when more than one file matches."""
    plans_dir = workspace_root / docs_dir / "plans"
    if not plans_dir.is_dir():
        return None
    matches = [p for p in sorted(plans_dir.glob("*.md")) if _plan_stem_matches_slug(p.stem, task_slug)]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def plan_has_tasks(text: str) -> bool:
    """A plan is 'real' if it has at least one <!-- mship:task ... --> anchor."""
    return _TASK_ANCHOR_RE.search(text) is not None


def resolve_plan_path(
    task_slug: str,
    plan_path: str | None,
    workspace_root: Path,
    docs_dir: str = "docs",
) -> Path | None:
    """Explicit `plan_path` (workspace-relative) wins; else the
    discover_plan_path convention. Returns the Path if the file exists AND lives
    inside the workspace, else None. The plan path is later read by the gate and
    dispatch, so a path that escapes the workspace (absolute, or `..` traversal)
    is rejected — never read files outside the workspace (Greptile security)."""
    root = Path(workspace_root).resolve()
    if plan_path:
        p = Path(plan_path)
        if not p.is_absolute():
            p = root / p
        try:
            p = p.resolve()
        except OSError:
            return None
        if not p.is_relative_to(root):
            return None
        return p if p.is_file() else None
    return discover_plan_path(root, task_slug, docs_dir)
