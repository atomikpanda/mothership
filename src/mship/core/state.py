import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


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


class WorkspaceState(BaseModel):
    current_task: str | None = None
    tasks: dict[str, Task] = {}


class StateManager:
    """Read/write .mothership/state.yaml with atomic writes."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._state_file = state_dir / "state.yaml"

    def load(self) -> WorkspaceState:
        if not self._state_file.exists():
            return WorkspaceState()
        with open(self._state_file) as f:
            raw = yaml.safe_load(f)
        if raw is None:
            return WorkspaceState()
        return WorkspaceState(**raw)

    def save(self, state: WorkspaceState) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        data = state.model_dump(mode="json")
        # Convert Path objects to strings for YAML
        for task in data.get("tasks", {}).values():
            task["worktrees"] = {
                k: str(v) for k, v in task.get("worktrees", {}).items()
            }
        # Atomic write: write to temp, rename
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

    def get_current_task(self) -> Task | None:
        state = self.load()
        if state.current_task is None:
            return None
        return state.tasks.get(state.current_task)
