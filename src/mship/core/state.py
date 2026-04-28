import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

import fcntl
from contextlib import contextmanager
from typing import Callable


class TestResult(BaseModel):
    status: Literal["pass", "fail", "skip"]
    at: datetime


class Task(BaseModel):
    slug: str
    description: str
    phase: Literal["plan", "dev", "review", "run"]
    created_at: datetime
    affected_repos: list[str]
    worktrees: dict[str, Path] = {}
    branch: str
    test_results: dict[str, TestResult] = {}
    blocked_reason: str | None = None
    blocked_at: datetime | None = None
    pr_urls: dict[str, str] = {}
    finished_at: datetime | None = None
    phase_entered_at: datetime | None = None
    active_repo: str | None = None
    last_switched_at_sha: dict[str, dict[str, str]] = {}
    test_iteration: int = 0
    base_branch: str | None = None
    passive_repos: set[str] = set()


class WorkspaceState(BaseModel):
    # extra="ignore" lets legacy state.yaml with `current_task:` load cleanly
    # (the field is silently dropped during the multi-task migration).
    model_config = ConfigDict(extra="ignore")
    tasks: dict[str, Task] = {}


@contextmanager
def _locked(state_dir: Path, mode: int):
    """Advisory lock on `<state_dir>/state.lock`.

    mode: fcntl.LOCK_SH (shared read) or fcntl.LOCK_EX (exclusive write).
    Released when the context exits.
    """
    lock_path = state_dir / "state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as lf:
        fcntl.flock(lf, mode)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


class StateManager:
    """Read/write .mothership/state.yaml with atomic writes + flock."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._state_file = state_dir / "state.yaml"

    def _load_nolock(self) -> WorkspaceState:
        if not self._state_file.exists():
            return WorkspaceState()
        with open(self._state_file) as f:
            raw = yaml.safe_load(f)
        if raw is None:
            return WorkspaceState()
        return WorkspaceState(**raw)

    def _save_nolock(self, state: WorkspaceState) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        data = state.model_dump(mode="json")
        for task in data.get("tasks", {}).values():
            task["worktrees"] = {
                k: str(v) for k, v in task.get("worktrees", {}).items()
            }
            if "passive_repos" in task:
                task["passive_repos"] = sorted(task["passive_repos"])
        fd, tmp_path = tempfile.mkstemp(
            dir=self._state_dir, suffix=".yaml.tmp"
        )
        try:
            with open(fd, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            Path(tmp_path).replace(self._state_file)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def load(self) -> WorkspaceState:
        with _locked(self._state_dir, fcntl.LOCK_SH):
            return self._load_nolock()

    def save(self, state: WorkspaceState) -> None:
        with _locked(self._state_dir, fcntl.LOCK_EX):
            self._save_nolock(state)

    def mutate(self, fn: "Callable[[WorkspaceState], None]") -> WorkspaceState:
        """Read-modify-write under one exclusive lock. No lost updates."""
        with _locked(self._state_dir, fcntl.LOCK_EX):
            state = self._load_nolock()
            fn(state)
            self._save_nolock(state)
            return state
