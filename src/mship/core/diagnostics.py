"""Forensics snapshot library for mship's self-diagnosing commands.

Writes JSON blobs to <state_dir>/diagnostics/<ts>-<command>-<reason>.json
when commands observe anomalous state. Best-effort — write failures are
caught and returned as None so callers are never interrupted.

Filename format: <ISO-8601 UTC, colons replaced with `-`>-<command>-<reason>.json.

See `docs/superpowers/specs/2026-04-21-stale-main-index-diagnostics-and-recovery-design.md`.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _git_state(repo_path: Path) -> dict:
    """Per-repo git state captured for the snapshot. Best-effort."""
    info: dict = {
        "git_status_porcelain": None,
        "head_sha": None,
        "head_branch": None,
        "upstream_tracking": None,
        "reflog_tail": None,
        "stash_count": None,
    }
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["git_status_porcelain"] = r.stdout
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["head_sha"] = r.stdout.strip() or None
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["head_branch"] = r.stdout.strip() or None
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            info["upstream_tracking"] = r.stdout.strip()
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "reflog", "-n", "10"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["reflog_tail"] = r.stdout.splitlines()
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["git", "stash", "list"],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        info["stash_count"] = len([l for l in r.stdout.splitlines() if l])
    except OSError:
        pass
    return info


def _mship_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("mothership")
    except Exception:
        return None


def _safe_timestamp() -> str:
    """ISO-8601 UTC timestamp with filesystem-safe separators.

    Colons (invalid on Windows, awkward on macOS) are replaced with hyphens.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # ends with +00:00, which includes a colon — strip tz and append Z
    if ts.endswith("+00:00"):
        ts = ts[:-len("+00:00")] + "Z"
    return ts.replace(":", "-")


def capture_snapshot(
    command: str,
    reason: str,
    state_dir: Path,
    *,
    repos: dict[str, Path] | None = None,
    extra: dict | None = None,
) -> Path | None:
    """Write a JSON forensics snapshot. Returns the path, or None on failure.

    command: invoking mship command name (e.g. "sync", "finish").
    reason:  short tag identifying what triggered capture (e.g. "dirty-main-pre-recovery").
    state_dir: workspace's .mothership directory.
    repos: optional {name: path} to capture per-repo git state.
    extra: caller-supplied free-form data.
    """
    try:
        diag_dir = Path(state_dir) / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)

        payload: dict = {
            "captured_at": _safe_timestamp(),
            "command": command,
            "reason": reason,
            "cwd": str(Path.cwd()),
            "mship_version": _mship_version(),
            "python_version": sys.version,
            "path_env": os.environ.get("PATH", ""),
        }
        if repos:
            payload["repos"] = {name: _git_state(Path(p)) for name, p in repos.items()}
        if extra:
            payload["extra"] = extra

        filename = f"{_safe_timestamp()}-{command}-{reason}.json"
        target = diag_dir / filename
        target.write_text(json.dumps(payload, indent=2))
        return target
    except OSError as e:
        log.debug("capture_snapshot write failed: %s", e)
        return None
