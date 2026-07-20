"""Pure assembly of a single-WorkItem cockpit model for `mship view workitem` (AC3).

Sourced from already-resolved canonical objects: a `WorkItemSummary` (for the
derived phase + id/title/kind/links), the linked `Spec` (status + acceptance
criteria with evidence), the item's `Task`s (worktrees + recorded PR urls), and its
`Thread`s. No Textual, no container, no store I/O — the whole cockpit shape is
unit-testable directly. The Textual `WorkItemCockpitView` and the CLI command wire
thin on top; the per-entity formatters below are shared by both the flat text
renderer (non-TTY) and the TUI row builder.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from mship.core.message import Thread
from mship.core.spec import AcceptanceEvidence, Spec
from mship.core.state import Task
from mship.core.view.workitem_index import WorkItemSummary


@dataclass(frozen=True)
class CriterionView:
    id: str
    text: str
    verdict: str
    evidence: list[AcceptanceEvidence] = field(default_factory=list)


@dataclass(frozen=True)
class TaskView:
    slug: str
    phase: str
    branch: str
    worktrees: dict[str, str]
    pr_urls: dict[str, str]
    blocked_reason: str | None
    finished_at: datetime | None


@dataclass(frozen=True)
class PRView:
    task_slug: str
    repo: str
    url: str


@dataclass(frozen=True)
class ThreadView:
    id: str
    subject: str
    needs_you: bool
    needs_decision: bool
    unseen: bool


@dataclass(frozen=True)
class WorkItemCockpit:
    id: str
    title: str
    kind: str
    phase: str
    spec_id: str | None
    spec_title: str | None
    spec_status: str | None
    criteria: list[CriterionView] = field(default_factory=list)
    tasks: list[TaskView] = field(default_factory=list)
    prs: list[PRView] = field(default_factory=list)
    threads: list[ThreadView] = field(default_factory=list)


def assemble_cockpit(
    summary: WorkItemSummary,
    spec: Spec | None,
    tasks: list[Task],
    threads: list[Thread],
) -> WorkItemCockpit:
    """Fold a WorkItem's canonical parts into one flat, render-ready model (AC3).

    `summary` supplies the derived phase + id/title/kind; `spec` its acceptance
    criteria (with evidence) + status; `tasks` their worktrees + PR urls; `threads`
    the linked conversations. All inputs are already resolved by the caller from the
    canonical stores. PRs are aggregated from each task's recorded `pr_urls` (where
    `mship finish` records the opened PR), preserving task then repo order.
    """
    criteria = [
        CriterionView(id=c.id, text=c.text, verdict=c.verdict, evidence=list(c.evidence))
        for c in (spec.acceptance_criteria if spec is not None else [])
    ]
    task_views = [
        TaskView(
            slug=t.slug, phase=t.phase, branch=t.branch,
            worktrees={repo: str(p) for repo, p in t.worktrees.items()},
            pr_urls=dict(t.pr_urls),
            blocked_reason=t.blocked_reason, finished_at=t.finished_at,
        )
        for t in tasks
    ]
    prs = [
        PRView(task_slug=t.slug, repo=repo, url=url)
        for t in tasks
        for repo, url in t.pr_urls.items()
    ]
    thread_views = [
        ThreadView(id=th.id, subject=th.subject, needs_you=th.needs_you,
                   needs_decision=th.needs_decision, unseen=th.unseen)
        for th in threads
    ]
    return WorkItemCockpit(
        id=summary.id, title=summary.title, kind=summary.kind, phase=summary.phase,
        spec_id=summary.spec_id,
        spec_title=spec.title if spec is not None else None,
        spec_status=spec.status if spec is not None else None,
        criteria=criteria, tasks=task_views, prs=prs, threads=thread_views,
    )


# --- per-entity formatters (shared by render_text + the TUI row builder) ---

def _evidence_line(e: AcceptanceEvidence) -> str:
    note = f" — {e.note}" if e.note else ""
    return f"    [{e.kind}] {e.ref}{note}"


def spec_detail(cockpit: WorkItemCockpit) -> str:
    if cockpit.spec_id is None:
        return "No spec linked."
    return "\n".join([
        f"spec {cockpit.spec_id}  [{cockpit.spec_status}]",
        f"  {cockpit.spec_title or ''}",
        f"  WorkItem phase: {cockpit.phase}",
    ])


def criterion_detail(c: CriterionView) -> str:
    lines = [f"{c.id}  [{c.verdict}]", f"  {c.text}"]
    if c.evidence:
        lines.append("  evidence:")
        lines.extend(_evidence_line(e) for e in c.evidence)
    else:
        lines.append("  (no evidence)")
    return "\n".join(lines)


def task_detail(t: TaskView) -> str:
    lines = [f"task {t.slug}  [{t.phase}]", f"  branch: {t.branch}"]
    if t.blocked_reason:
        lines.append(f"  BLOCKED: {t.blocked_reason}")
    if t.finished_at is not None:
        lines.append(f"  finished: {t.finished_at:%Y-%m-%d %H:%M}")
    if t.worktrees:
        lines.append("  worktrees:")
        for repo, path in t.worktrees.items():
            lines.append(f"    {repo}: {path}")
    if t.pr_urls:
        lines.append("  PRs:")
        for repo, url in t.pr_urls.items():
            lines.append(f"    {repo}: {url}")
    return "\n".join(lines)


def pr_detail(p: PRView) -> str:
    return f"PR ({p.repo}, task {p.task_slug})\n  {p.url}"


def thread_detail(t: ThreadView) -> str:
    flags = [name for name, on in (("needs-you", t.needs_you),
             ("needs-decision", t.needs_decision), ("unseen", t.unseen)) if on]
    suffix = f"  [{', '.join(flags)}]" if flags else ""
    return f"thread {t.id}{suffix}\n  {t.subject}"


def render_text(cockpit: WorkItemCockpit) -> str:
    """Flat text dump of the whole cockpit — the non-TTY short-circuit output
    (agent pipes / CI), mirroring `mship view spec`'s non-TTY behavior."""
    parts: list[str] = [
        f"◆ {cockpit.id}  ·  {cockpit.title}  ·  [{cockpit.phase}]",
        "",
        "SPEC",
        spec_detail(cockpit),
        "",
        "ACCEPTANCE CRITERIA",
    ]
    parts.extend(criterion_detail(c) for c in cockpit.criteria)
    if not cockpit.criteria:
        parts.append("(none)")
    parts += ["", "TASKS"]
    parts.extend(task_detail(t) for t in cockpit.tasks)
    if not cockpit.tasks:
        parts.append("(none)")
    parts += ["", "PRS"]
    parts.extend(pr_detail(p) for p in cockpit.prs)
    if not cockpit.prs:
        parts.append("(none)")
    parts += ["", "THREADS"]
    parts.extend(thread_detail(t) for t in cockpit.threads)
    if not cockpit.threads:
        parts.append("(none)")
    return "\n".join(parts)
