import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional


TestState = Literal["pass", "fail", "mixed"]


@dataclass
class LogEntry:
    timestamp: datetime
    message: str
    repo: Optional[str] = None
    iteration: Optional[int] = None
    test_state: Optional[TestState] = None
    action: Optional[str] = None
    open_question: Optional[str] = None
    id: Optional[str] = None
    parent: Optional[str] = None
    evidence: Optional[str] = None
    category: Optional[str] = None


_HEADER_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)(?P<kv>(?:  [^\n]*)?)\n(.*?)(?=\n## \d{4}-\d{2}-\d{2}T|\Z)",
    re.MULTILINE | re.DOTALL,
)

_KV_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')


def _parse_kv(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _KV_RE.finditer(raw):
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else m.group(3)
        out[key] = val
    return out


def _format_kv(entry: LogEntry) -> str:
    parts: list[str] = []
    if entry.repo is not None:
        parts.append(f"repo={entry.repo}")
    if entry.iteration is not None:
        parts.append(f"iter={entry.iteration}")
    if entry.test_state is not None:
        parts.append(f"test={entry.test_state}")
    if entry.action is not None:
        if ' ' in entry.action:
            a = entry.action.replace('"', '\\"')
            parts.append(f'action="{a}"')
        else:
            parts.append(f"action={entry.action}")
    if entry.open_question is not None:
        q = entry.open_question.replace('"', '\\"')
        parts.append(f'open="{q}"')
    if entry.id is not None:
        parts.append(f"id={entry.id}")
    if entry.parent is not None:
        parts.append(f"parent={entry.parent}")
    if entry.evidence is not None:
        ev = entry.evidence.replace('"', '\\"')
        parts.append(f'evidence="{ev}"')
    if entry.category is not None:
        if ' ' in entry.category:
            c = entry.category.replace('"', '\\"')
            parts.append(f'category="{c}"')
        else:
            parts.append(f"category={entry.category}")
    return "  " + "  ".join(parts) if parts else ""


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

    def append(
        self,
        task_slug: str,
        message: str,
        *,
        repo: Optional[str] = None,
        iteration: Optional[int] = None,
        test_state: Optional[TestState] = None,
        action: Optional[str] = None,
        open_question: Optional[str] = None,
        id: Optional[str] = None,
        parent: Optional[str] = None,
        evidence: Optional[str] = None,
        category: Optional[str] = None,
    ) -> None:
        path = self._log_path(task_slug)
        if not path.exists():
            self.create(task_slug)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc),
            message=message,
            repo=repo,
            iteration=iteration,
            test_state=test_state,
            action=action,
            open_question=open_question,
            id=id,
            parent=parent,
            evidence=evidence,
            category=category,
        )
        kv = _format_kv(entry)
        with open(path, "a") as f:
            f.write(f"\n## {timestamp}{kv}\n{message}\n")

    def read(self, task_slug: str, last: Optional[int] = None) -> list[LogEntry]:
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
        for match in _HEADER_RE.finditer(content):
            timestamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            kv_raw = match.group("kv") or ""
            message = match.group(3).strip()
            if not message:
                continue
            kv = _parse_kv(kv_raw)
            iteration = int(kv["iter"]) if "iter" in kv and kv["iter"].isdigit() else None
            entries.append(LogEntry(
                timestamp=timestamp,
                message=message,
                repo=kv.get("repo"),
                iteration=iteration,
                test_state=kv.get("test"),
                action=kv.get("action"),
                open_question=kv.get("open"),
                id=kv.get("id"),
                parent=kv.get("parent"),
                evidence=kv.get("evidence"),
                category=kv.get("category"),
            ))
        return entries
