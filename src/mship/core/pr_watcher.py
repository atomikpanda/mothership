"""Background poll: notice a task's PR merge/close and post a once-only
mailbox event so the owning agent picks it up without nagging the phone.

Posted messages use `Message.kind == "event"` (see mship.core.message —
`Thread.awaiting_agent_event` is the computed flag an idle-session wake/inbox
wait can key off). This module only detects the transition and posts the
event; it does not itself wake or steer anything.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# States that mean "this PR is done, the task's agent should notice."
_TERMINAL_STATES = ("merged", "closed")


class PrWatcher:
    """Polls each task's `pr_urls` for a merge/close transition and posts a
    single agent `event` message to the task's mailbox thread the first time
    it's observed.

    Idempotency is two-layered:
    - in-process: `self.notified` — a `(slug, repo, url, state)` set, cheap
      short-circuit across repeated `check_once()` calls in one run.
    - cross-process/restart: before posting, the target thread is scanned for
      an existing event message mentioning the same url+state; if found, no
      duplicate is posted (and the tuple is recorded so this process also
      short-circuits next time).

    `check_state` and `now_fn` are injected for testability. Real callers
    pass `lambda url: pr_manager.check_pr_state(url).state` and
    `lambda: datetime.now(timezone.utc)`.
    """

    def __init__(
        self,
        msgs: Any,
        workitems: Any,
        state_manager: Any,
        check_state: Callable[[str], str],
        now_fn: Callable[[], datetime],
        lock: Any | None = None,
        config: Any | None = None,
        workspace_root: Path | None = None,
        shell: Any | None = None,
    ) -> None:
        self.msgs = msgs
        self.workitems = workitems
        self.state_manager = state_manager
        self.check_state = check_state
        self.now_fn = now_fn
        self.lock = lock
        self.notified: set[tuple[str, str, str, str]] = set()
        # Lifecycle hooks (MOS-220): `pr.merged`/`pr.closed`. `config` is the
        # workspace's WorkspaceConfig — when None (default), hooks are simply
        # never evaluated, so existing callers/tests are unaffected.
        self.config = config
        self.workspace_root = workspace_root
        self.shell = shell

    def check_once(self) -> None:
        """One sweep over all tasks' PR urls. Never raises — a failure while
        checking one PR is logged and skipped so it can't abort the sweep for
        every other task."""
        tasks = self.state_manager.load().tasks
        for slug, task in tasks.items():
            if not task.pr_urls:
                continue
            for repo, url in task.pr_urls.items():
                try:
                    self._check_one(slug, task, repo, url)
                except Exception:
                    log.exception(
                        "pr_watcher: failed checking PR (task=%s repo=%s url=%s)",
                        slug, repo, url,
                    )

    def _check_one(self, slug: str, task: Any, repo: str, url: str) -> None:
        st = self.check_state(url)
        if st not in _TERMINAL_STATES:
            return
        key = (slug, repo, url, st)
        if key in self.notified:
            return
        if self.lock is not None:
            with self.lock:
                self._resolve_and_post(slug, task, repo, url, st, key)
        else:
            self._resolve_and_post(slug, task, repo, url, st, key)

    def _resolve_and_post(
        self, slug: str, task: Any, repo: str, url: str, st: str,
        key: tuple[str, str, str, str],
    ) -> None:
        now = self.now_fn()
        text = f"\U0001f500 PR {st}: {url} (task {slug}) — ready to close out."

        tid, wi = self._resolve_thread(slug, task, now)

        thread = self.msgs.get(tid)
        marker = f"PR {st}: {url}"
        if thread is not None and any(
            m.kind == "event" and marker in m.text
            for m in thread.messages
        ):
            # Already posted (prior process) — record it so this process's
            # in-memory set short-circuits future checks too.
            self.notified.add(key)
            return

        self.msgs.append(tid, "agent", text, now, kind="event")
        self.notified.add(key)
        self._fire_lifecycle_hook(st, slug, repo)

    def _fire_lifecycle_hook(self, st: str, slug: str, repo: str) -> None:
        """Fire the `pr.merged`/`pr.closed` lifecycle hook (MOS-220), once,
        the same time the mailbox event is posted above. Polling-derived —
        the merge/close already happened by the time this sweep observes it,
        so `required: true` is rejected for these events at config-load time
        (nothing left to block). Never lets a hook failure escape: this
        mirrors check_once()'s own never-abort-the-sweep-on-one-failure
        pattern, so a broken hook config can't take down PR notifications
        for every other task."""
        if self.config is None or self.workspace_root is None:
            return
        from mship.core.lifecycle_hooks import HookContext, run_hooks
        try:
            run_hooks(
                f"pr.{st}",
                HookContext(task_slug=slug, repo=repo),
                config=self.config,
                workspace_root=self.workspace_root,
                shell=self.shell,
                state_manager=self.state_manager,
            )
        except Exception:
            log.exception(
                "pr_watcher: lifecycle hook failed for pr.%s (task=%s repo=%s)",
                st, slug, repo,
            )

    def _resolve_thread(self, slug: str, task: Any, now: datetime) -> tuple[str, Any]:
        """Mirror POST /items/{id}/messages' thread resolution: prefer the
        task's WorkItem's first thread, else an existing thread scoped to
        this task_slug, else create one (linking it back to the WorkItem when
        one exists). Returns (thread_id, work_item_or_None)."""
        work_item_id = getattr(task, "work_item_id", None)
        wi = self.workitems.get(work_item_id) if work_item_id else None
        if wi and wi.thread_ids:
            return wi.thread_ids[0], wi

        existing = next(
            (t for t in self.msgs.list() if t.task_slug == slug), None
        )
        if existing is not None:
            return existing.id, wi

        # create_thread seeds a human message — the event itself is appended
        # separately (below, in _resolve_and_post) so it lands as an agent
        # message, matching every other event/needs_you/decision message.
        thread = self.msgs.create_thread(
            subject=slug, text=f"PR watcher: tracking {slug}", now=now, task_slug=slug,
        )
        if wi is not None:
            self.workitems.add_thread(wi.id, thread.id, now)
        return thread.id, wi
