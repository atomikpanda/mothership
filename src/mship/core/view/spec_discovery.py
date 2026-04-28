from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mship.core.state import WorkspaceState


class SpecNotFoundError(Exception):
    pass


SPEC_SUBDIR = Path("docs") / "superpowers" / "specs"


def find_spec(
    workspace_root: Path,
    name_or_path: str | None,
    *,
    task: str | None = None,
    state: Optional["WorkspaceState"] = None,
    spec_paths: list[str] | None = None,
) -> Path:
    """Resolve a spec file.

    - `name_or_path=None, task=None`: newest in `workspace_root` specs dir.
    - `name_or_path=None, task=<slug>`: newest across that task's worktrees.
    - `name_or_path=<name>, task=<slug>`: that name, searching only the task's worktrees.
    - `name_or_path=<name>, task=None, state=<state>`: that name, searching main + every worktree.
    - `name_or_path=<absolute path>`: that literal file.

    `spec_paths` (workspace-relative) overrides the default `docs/superpowers/specs`
    search root. None = default. See #113.
    """
    if name_or_path is not None:
        candidate = Path(name_or_path)
        if candidate.is_absolute():
            if candidate.is_file():
                return candidate
            raise SpecNotFoundError(f"Spec not found: {name_or_path}")

    search_roots = _resolve_search_roots(workspace_root, task, state, spec_paths)

    if name_or_path is None:
        return _newest_across(search_roots, task)

    for root in search_roots:
        for candidate_name in (name_or_path, f"{name_or_path}.md"):
            p = root / candidate_name
            if p.is_file():
                return p

    available_msg = _available_msg(search_roots)
    where = f"task {task!r}" if task else "any known location"
    raise SpecNotFoundError(f"Spec not found: {name_or_path!r} (searched {where}).{available_msg}")


def _resolve_search_roots(
    workspace_root: Path,
    task: str | None,
    state: "WorkspaceState | None",
    spec_paths: list[str] | None = None,
) -> list[Path]:
    subdirs = [Path(p) for p in spec_paths] if spec_paths else [SPEC_SUBDIR]
    if task is not None:
        if state is None or task not in state.tasks:
            raise SpecNotFoundError(f"Unknown task: {task!r}")
        worktrees = state.tasks[task].worktrees
        roots = [Path(p) / sub for p in worktrees.values() for sub in subdirs]
        return [r for r in roots if r.is_dir()] or roots
    roots: list[Path] = [workspace_root / sub for sub in subdirs]
    if state is not None:
        for t in state.tasks.values():
            for wt in t.worktrees.values():
                for sub in subdirs:
                    roots.append(Path(wt) / sub)
    return roots


def _newest_across(roots: list[Path], task: str | None) -> Path:
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(p for p in root.iterdir() if p.is_file() and p.suffix == ".md")
    if not candidates:
        where = f"task {task!r}" if task else f"{roots[0] if roots else '?'}"
        raise SpecNotFoundError(f"No specs found in {where}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _available_msg(roots: list[Path]) -> str:
    names: set[str] = set()
    for r in roots:
        if r.is_dir():
            names.update(p.name for p in r.iterdir() if p.is_file() and p.suffix == ".md")
    if not names:
        return ""
    shown = sorted(names)[:5]
    rest = len(names) - len(shown)
    suffix = f" ({rest} more)" if rest > 0 else ""
    return f" Available: {', '.join(shown)}{suffix}."
