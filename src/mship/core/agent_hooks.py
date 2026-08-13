"""Private runtime-independent policy for Claude, Codex, and OMP agent hooks."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

from mship.core.edit_guard import evaluate_edit
from mship.core.gate import messaging_notice, no_task_notice
from mship.core.message_wait import stamp_agent_seen


class Runtime(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    OMP = "omp"


class DecisionKind(str, Enum):
    CONTEXT = "context"
    ALLOW = "allow"
    DENY = "deny"
    CONTINUE = "continue"
    STOP = "stop"


@dataclass(frozen=True)
class AgentHookDecision:
    runtime: Runtime
    kind: DecisionKind
    message: str = ""


def session_start(runtime: Runtime, cwd: Path) -> AgentHookDecision:
    """Return the shared Mothership context for a runtime session start."""
    context = "\n".join(
        text
        for text in (no_task_notice(cwd), messaging_notice(cwd))
        if text
    )
    return AgentHookDecision(runtime, DecisionKind.CONTEXT, context)


def _normalize_targets(cwd: Path, targets: Iterable[str | os.PathLike[str]]) -> tuple[Path, ...]:
    normalized: list[Path] = []
    seen: set[str] = set()
    for target in targets:
        if not str(target):
            continue
        path = Path(target)
        if not path.is_absolute():
            path = cwd / path
        path = Path(os.path.normpath(path))
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            normalized.append(path)
    if not normalized:
        raise ValueError("guarded edit did not contain a target path")
    return tuple(normalized)


def pre_tool_use(
    *,
    runtime: Runtime,
    cwd: Path,
    targets: Iterable[str | os.PathLike[str]],
    state,
    config,
    workspace_root: Path,
    bypass_main_edit: bool,
    bypass_workitem_gate: bool,
) -> AgentHookDecision:
    """Evaluate every target in one normalized guarded edit operation."""
    normalized = _normalize_targets(cwd, targets)
    if bypass_main_edit:
        return AgentHookDecision(runtime, DecisionKind.ALLOW)

    for target in normalized:
        decision = evaluate_edit(
            target,
            state,
            config,
            workspace_root=workspace_root,
            bypass_workitem_gate=bypass_workitem_gate,
        )
        if not decision.allowed:
            return AgentHookDecision(runtime, DecisionKind.DENY, decision.reason)
    return AgentHookDecision(runtime, DecisionKind.ALLOW)


def _format_drain_reason(threads: list) -> str:
    """Render actionable inbox threads as a continuation instruction."""
    replies = [thread for thread in threads if thread.awaiting_reply]
    events = [
        thread for thread in threads
        if thread.awaiting_agent_event and not thread.awaiting_reply
    ]

    def pending(thread) -> str:
        return thread.messages[-1].text if thread.messages else ""

    lines: list[str] = []
    if replies:
        count = len(replies)
        lines += [
            f"{count} message{'s' if count != 1 else ''} waiting in your inbox. Answer each, "
            f"then post your answer with `mship reply <thread-id> \"<text>\"` "
            f"(replying clears the thread):",
            "",
        ]
        lines += [
            f"- thread {thread.id} ({thread.subject}): {pending(thread)}"
            for thread in replies
        ]
    if events:
        if lines:
            lines.append("")
        count = len(events)
        lines += [
            f"{count} agent signal{'s' if count != 1 else ''} for you to act on "
            f"(a dispatched spec handoff, or a PR merged/closed). These are FOR "
            f"THE AGENT, not the operator — take the action each one calls for. "
            f"The signal clears once you post a follow-up on the thread; a human "
            f"reply does not clear it:",
            "",
        ]
        lines += [
            f"- thread {thread.id} ({thread.subject}): {pending(thread)}"
            for thread in events
        ]
    return "\n".join(lines)


def stop(
    *,
    runtime: Runtime,
    store,
    continuation_active: bool,
    now: datetime | None = None,
) -> AgentHookDecision:
    """Continue once for actionable inbox work; stop on continuation re-entry."""
    if continuation_active:
        return AgentHookDecision(runtime, DecisionKind.STOP)

    awaiting = [
        thread for thread in store.list()
        if thread.awaiting_reply or thread.awaiting_agent_event
    ]
    stamp_agent_seen(store, awaiting, now or datetime.now(timezone.utc))
    if not awaiting:
        return AgentHookDecision(runtime, DecisionKind.STOP)
    return AgentHookDecision(runtime, DecisionKind.CONTINUE, _format_drain_reason(awaiting))
