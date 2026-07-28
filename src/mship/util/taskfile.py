"""Read a repo's go-task file without running go-task.

Moved verbatim out of `cli/exec.py` (where `mship logs` used it to turn a
missing target into an actionable error instead of go-task's general help text,
issue #125) so `core/remote_exec.py` can ask the same question on a run host —
`core` must not import from `cli`.
"""
from pathlib import Path

import yaml


def taskfile_has_target(repo_path, target: str) -> bool:
    """True if `<repo_path>/Taskfile.yml` (or .yaml) defines `target`.

    Reads the local Taskfile only; `includes:` are not recursed into. That
    covers the common case. False on a missing file or a parse error, which is
    the correct, fail-loud signal for the caller.
    """
    p = Path(repo_path)
    candidates = [p / "Taskfile.yml", p / "Taskfile.yaml"]
    taskfile = next((c for c in candidates if c.exists()), None)
    if taskfile is None:
        return False
    try:
        data = yaml.safe_load(taskfile.read_text())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    tasks = data.get("tasks", {})
    if not isinstance(tasks, dict):
        return False
    return target in tasks
