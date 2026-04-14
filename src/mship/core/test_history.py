"""Per-iteration test run storage + diffing."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _run_dir(state_dir: Path, task_slug: str) -> Path:
    return state_dir / "test-runs" / task_slug


def _run_path(state_dir: Path, task_slug: str, iteration: int) -> Path:
    return _run_dir(state_dir, task_slug) / f"{iteration}.json"


def _latest_path(state_dir: Path, task_slug: str) -> Path:
    return _run_dir(state_dir, task_slug) / "latest.json"


def write_run(
    state_dir: Path,
    task_slug: str,
    iteration: int,
    started_at: datetime,
    duration_ms: int,
    results: dict[str, dict[str, Any]],
    streams: "dict[str, tuple[str, str]] | None" = None,
) -> Path:
    """Write iteration JSON + update latest pointer. Returns the iteration file path.

    streams: optional dict mapping repo name -> (stdout, stderr) strings. When
    provided, writes <iteration>.<repo>.stdout and <iteration>.<repo>.stderr
    files alongside the iteration JSON and sets stdout_path/stderr_path on each
    repo entry in the results dict in-place before serialising.
    """
    run_dir = _run_dir(state_dir, task_slug)
    run_dir.mkdir(parents=True, exist_ok=True)

    if streams:
        for repo, (stdout_str, stderr_str) in streams.items():
            if repo not in results:
                continue
            stdout_file = run_dir / f"{iteration}.{repo}.stdout"
            stderr_file = run_dir / f"{iteration}.{repo}.stderr"
            stdout_file.write_text(stdout_str or "")
            stderr_file.write_text(stderr_str or "")
            results[repo]["stdout_path"] = str(stdout_file.resolve())
            results[repo]["stderr_path"] = str(stderr_file.resolve())

    payload = {
        "iteration": iteration,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "duration_ms": duration_ms,
        "repos": results,
    }
    path = _run_path(state_dir, task_slug, iteration)
    path.write_text(json.dumps(payload, indent=2))
    _latest_path(state_dir, task_slug).write_text(json.dumps(payload, indent=2))
    return path


def read_run(state_dir: Path, task_slug: str, iteration: int) -> dict | None:
    path = _run_path(state_dir, task_slug, iteration)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def latest_iteration(state_dir: Path, task_slug: str) -> int | None:
    d = _run_dir(state_dir, task_slug)
    if not d.exists():
        return None
    numbers = [int(p.stem) for p in d.iterdir() if p.stem.isdigit()]
    return max(numbers) if numbers else None


def compute_diff(
    current: dict,
    previous: dict | None,
    pre_previous: dict | None,
) -> dict:
    """Label each repo based on pass/fail transitions."""
    tags: dict[str, str] = {}
    summary = {
        "new_failures": [],
        "fixes": [],
        "regressions": [],
        "new_passes": [],
    }
    prev_repos = (previous or {}).get("repos", {})
    pre_prev_repos = (pre_previous or {}).get("repos", {})

    for name, r in current.get("repos", {}).items():
        cur_status = r.get("status")
        if previous is None or name not in prev_repos:
            tags[name] = "first run"
            continue
        prev_status = prev_repos[name].get("status")
        if prev_status == "pass" and cur_status == "pass":
            tags[name] = "still passing"
        elif prev_status == "fail" and cur_status == "pass":
            tags[name] = "fix"
            summary["fixes"].append(name)
        elif prev_status == "pass" and cur_status == "fail":
            pre_status = pre_prev_repos.get(name, {}).get("status")
            if pre_status == "pass":
                tags[name] = "regression"
                summary["regressions"].append(name)
            else:
                tags[name] = "new failure"
                summary["new_failures"].append(name)
        elif prev_status == "fail" and cur_status == "fail":
            tags[name] = "still failing"
        else:
            tags[name] = "changed"

    return {
        "previous_iteration": (previous or {}).get("iteration"),
        "tags": tags,
        "summary": summary,
    }


def prune(state_dir: Path, task_slug: str, keep: int = 20) -> None:
    d = _run_dir(state_dir, task_slug)
    if not d.exists():
        return
    numbered = sorted(
        (int(p.stem), p) for p in d.iterdir() if p.stem.isdigit()
    )
    if len(numbered) <= keep:
        return
    for iter_num, path in numbered[:-keep]:
        path.unlink(missing_ok=True)
        # Remove any stdout/stderr artifact files for this iteration.
        for artifact in d.glob(f"{iter_num}.*.stdout"):
            artifact.unlink(missing_ok=True)
        for artifact in d.glob(f"{iter_num}.*.stderr"):
            artifact.unlink(missing_ok=True)
