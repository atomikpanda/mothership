"""Workspace discovery (#472): scan configured roots for `mothership.yaml`.

Rules (issue #472's discovery model):
- Bounded: only the configured roots are walked, to `max_depth`, symlinks not
  followed. Empty config scans nothing.
- Prune-on-hit: a directory containing `mothership.yaml` is a candidate and is
  NOT descended into — the outermost marker wins, so a repo inside a metarepo
  never registers as a second workspace.
- Mandatory exclusions: `.worktrees`, `.mothership`, `.git`, and dot-dirs.
  Spawn materializes a full tracked `mothership.yaml` inside
  `.worktrees/<slug>/<repo>/` — the primary false-positive this prune exists
  for (worktree.py:714 marker).
- Linked-worktree detection: a candidate whose `.git` is a FILE (gitdir:
  pointer) or that sits under a `.mship-workspace` marker ancestor
  (workspace_marker.read_marker_from_ancestor) is a task worktree, never an
  independent workspace — even outside `.worktrees/`.
- Walk-up: a scan root that is itself INSIDE a workspace resolves to that
  enclosing workspace (nearest ancestor with `mothership.yaml`).
- Dedupe: resolved-real-path identity, then cross-candidate ancestor/descendant
  collapse (outermost wins globally, across roots).
- Validation: `ConfigLoader.load(require_paths=False)` (unmaterialized child
  paths must not degrade a metarepo/monorepo entry) + a materialization guard —
  a parseable yaml NONE of whose repo paths exist is a template/example
  (mothership's own `examples/mothership.yaml`), degraded, never healthy.
- Degraded, never fatal: every per-candidate error becomes a degraded
  Candidate; the scan never aborts.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mship.core.daemon.registry import DaemonConfig, RepoInfo, RuntimeInfo

MARKER = "mothership.yaml"
MANDATORY_EXCLUDES = {".worktrees", ".mothership", ".git"}


class ScanRootError(ValueError):
    """A configured discovery boundary is unavailable; never an empty scan."""


@dataclass
class Candidate:
    path: Path  # workspace root (resolved)
    config_path: Path
    name: str = ""
    healthy: bool = True
    detail: str = ""
    repos: list[RepoInfo] = field(default_factory=list)
    runtime: RuntimeInfo = field(default_factory=RuntimeInfo)
    runner: Optional[dict] = None


def _is_task_worktree(ws_dir: Path) -> bool:
    from mship.core.workspace_marker import find_marker_owner

    git = ws_dir / ".git"
    if git.is_file():  # linked worktree: .git is a gitdir: pointer file
        return True
    return find_marker_owner(ws_dir) is not None


def _walk_up_enclosing(root: Path) -> Path | None:
    """Nearest ancestor (or root itself) carrying a mothership.yaml."""
    cur = root
    while True:
        if (cur / MARKER).is_file():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def _find_marker_dirs(root: Path, cfg: DaemonConfig) -> list[Path]:
    found: list[Path] = []
    enclosing = _walk_up_enclosing(root)
    if enclosing is not None:
        return [enclosing]
    root_depth = len(root.parts)

    def fail_walk(error: OSError) -> None:
        failed_path = error.filename or str(root)
        raise ScanRootError(
            f"configured scan root {root} is inaccessible at {failed_path}: "
            f"{error.strerror or error}"
        ) from error

    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=fail_walk
    ):
        cur = Path(dirpath)
        if MARKER in filenames:
            found.append(cur)
            dirnames[:] = []  # prune-on-hit: outermost wins
            continue
        if len(cur.parts) - root_depth >= cfg.max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if d not in MANDATORY_EXCLUDES
            and not d.startswith(".")
            and not any(fnmatch.fnmatch(d, g) for g in cfg.ignore_globs)
            and not (cur / d).is_symlink()
        ]
    return found


def _materialize(ws_dir: Path) -> Candidate:
    from mship.core.config import ConfigLoader

    config_path = ws_dir / MARKER
    cand = Candidate(path=ws_dir, config_path=config_path, name=ws_dir.name)
    try:
        raw_config = ConfigLoader.load(config_path, require_paths=False)
    except Exception as e:
        cand.healthy = False
        cand.detail = f"invalid mothership.yaml: {e}"
        return cand
    cand.name = raw_config.workspace or ws_dir.name
    repos: list[RepoInfo] = []
    any_exists = False
    for name, repo in raw_config.repos.items():
        if repo.git_root is not None:
            parent = raw_config.repos.get(repo.git_root)
            effective = (Path(parent.path) / repo.path) if parent else Path(repo.path)
        else:
            effective = Path(repo.path)
        if effective.is_dir():
            any_exists = True
        repos.append(RepoInfo(name=name, path=str(effective), git_root=repo.git_root))
    cand.repos = repos
    if repos and not any_exists:
        cand.healthy = False
        cand.detail = "no repo paths exist — template/unmaterialized workspace?"
        return cand
    venv = ws_dir / ".venv"
    if venv.is_dir():
        cand.runtime = RuntimeInfo(
            venv_path=str(venv),
            interpreter=str(venv / "bin" / "python") if (venv / "bin" / "python").exists() else None,
        )
    # Never the daemon's own sys.executable: an inherited runtime is exactly
    # the ambient-state failure #472 forbids. Absent venv → interpreter=None.
    runner_raw = getattr(raw_config, "runner", None)
    if runner_raw is None:
        try:
            import yaml

            runner_raw = (yaml.safe_load(config_path.read_text()) or {}).get("runner")
        except Exception:
            runner_raw = None
    if runner_raw is not None and not isinstance(runner_raw, dict):
        cand.healthy = False
        cand.detail = "invalid runner: expected a mapping"
        return cand
    cand.runner = runner_raw
    return cand

def scan_roots(cfg: DaemonConfig) -> list[Candidate]:
    marker_dirs: list[Path] = []
    for root_str in cfg.scan_roots:
        root = Path(root_str)
        if root.is_symlink():
            if not root.exists() or not root.is_dir():
                raise ScanRootError(
                    f"configured scan root is missing or inaccessible: {root}"
                )
            continue
        if not root.is_dir():
            raise ScanRootError(
                f"configured scan root is missing or inaccessible: {root}"
            )
        marker_dirs.extend(_find_marker_dirs(root, cfg))

    # Resolved-path dedupe.
    seen: dict[Path, Path] = {}
    for d in marker_dirs:
        try:
            seen.setdefault(d.resolve(), d)
        except OSError:
            continue
    resolved = sorted(seen.keys(), key=lambda p: len(p.parts))

    # Cross-candidate ancestor/descendant collapse: outermost wins globally.
    kept: list[Path] = []
    for p in resolved:
        if any(p.is_relative_to(k) for k in kept):
            continue
        kept.append(p)

    out: list[Candidate] = []
    for ws_dir in kept:
        try:
            if _is_task_worktree(ws_dir):
                continue
            out.append(_materialize(ws_dir))
        except Exception as e:  # degraded, never fatal — the scan must not abort
            out.append(Candidate(path=ws_dir, config_path=ws_dir / MARKER, name=ws_dir.name,
                                 healthy=False, detail=f"scan error: {e}"))
    return out
