import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class LogEntry:
    timestamp: datetime
    message: str


class LogManager:
    """Per-task append-only markdown logs for agent context recovery."""

    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir

    def _log_path(self, task_slug: str) -> Path:
        return self._logs_dir / f"{task_slug}.md"

    def create(self, task_slug: str) -> None:
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_path(task_slug)
        path.write_text(f"# Task Log: {task_slug}\n")

    def append(self, task_slug: str, message: str) -> None:
        path = self._log_path(task_slug)
        if not path.exists():
            self.create(task_slug)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a") as f:
            f.write(f"\n## {timestamp}\n{message}\n")

    def read(self, task_slug: str, last: int | None = None) -> list[LogEntry]:
        path = self._log_path(task_slug)
        if not path.exists():
            return []
        content = path.read_text()
        entries = self._parse(content)
        if last is not None:
            entries = entries[-last:]
        return entries

    def _parse(self, content: str) -> list[LogEntry]:
        entries: list[LogEntry] = []
        pattern = re.compile(
            r"^## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\n(.*?)(?=\n## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        for match in pattern.finditer(content):
            timestamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            message = match.group(2).strip()
            if message:
                entries.append(LogEntry(timestamp=timestamp, message=message))
        return entries
