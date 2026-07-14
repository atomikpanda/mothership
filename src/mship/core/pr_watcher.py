"""Background poll: notice a task's PR merge/close and post a once-only
mailbox event so the owning agent picks it up without nagging the phone.

Posted messages use `Message.kind == "event"` (see mship.core.message —
`Thread.awaiting_agent_event` is the computed flag an idle-session wake/inbox
wait can key off). This module only detects the transition and posts the
event; it does not itself wake or steer anything.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# States that mean "this PR is done, the task's agent should notice."
_TERMINAL_STATES = ("merged", "closed")


def _refs_pr(text: str, needle: str) -> bool:
    """True if `text` contains `needle` NOT immediately followed by another digit — so a
    reference to `.../pull/4` doesn't spuriously match `.../pull/42`. Used for both the PR
    url (thread lookup) and the `PR <state>: <url>` dedup marker; both end in the PR number,
    so a trailing-digit boundary is exactly the right test."""
    return re.search(re.escape(needle) + r"(?!\d)", text) is not None


def resolve_task_thread(msgs, workitems, slug, task, url, now):
    """Resolve the thread a PR event for `task`/`url` belongs to, strongly preferring an EXISTING
    thread over a fresh one (PR events were cluttering the app with a thread per task). In order:

      1. the task's WorkItem's first thread (its canonical thread), else
      2. any existing thread that already references this PR `url` — link it to the WorkItem so
         later events consolidate there too, else
      3. an existing thread scoped to this task_slug, else
      4. create one (linked to the WorkItem when present).

    Only step 4 spawns a new thread. Returns (thread_id, work_item_or_None). Shared by the
    pr_watcher (merge/close events) and `mship finish` ([announce_prs_on_thread], posting the PR
    url at open time so there's always a thread to reuse)."""
    work_item_id = getattr(task, "work_item_id", None)
    wi = workitems.get(work_item_id) if work_item_id else None
    if wi and wi.thread_ids:
        return wi.thread_ids[0], wi

    url_thread = next(
        (t for t in msgs.list() if any(_refs_pr(m.text, url) for m in t.messages)),
        None,
    )
    if url_thread is not None:
        if wi is not None:
            workitems.add_thread(wi.id, url_thread.id, now)
        return url_thread.id, wi

    existing = next((t for t in msgs.list() if t.task_slug == slug), None)
    if existing is not None:
        if wi is not None:
            workitems.add_thread(wi.id, existing.id, now)  # link so later WorkItem events reuse it
        return existing.id, wi

    # create_thread seeds a human message; the event itself is appended separately so it lands as
    # an agent message, matching every other event/needs_you/decision message.
    thread = msgs.create_thread(subject=slug, text=f"Tracking {slug}", now=now, task_slug=slug)
    if wi is not None:
        workitems.add_thread(wi.id, thread.id, now)
    return thread.id, wi


def announce_prs_on_thread(msgs, workitems, slug, task, pr_list, now):
    """Post a one-time `PR opened: <url>…` NOTE into the task's WorkItem/tracking thread (resolved
    via [resolve_task_thread]) when `mship finish` opens PR(s), so the pr_watcher reuses that thread
    on merge/close instead of spawning a new one. Posted as kind='note' (informational) — NOT
    kind='event' — so it does not trip `Thread.awaiting_agent_event` and nag the agent on every
    finish; the url still carries for reuse. Idempotent for `--force` re-finish: skips when an
    equivalent opened-note is already present. No-op when there are no urls."""
    urls = list(dict.fromkeys(p["url"] for p in pr_list if p.get("url")))
    if not urls:
        return
    tid, _wi = resolve_task_thread(msgs, workitems, slug, task, urls[0], now)
    thread = msgs.get(tid)
    if thread is not None and any(
        m.kind == "note" and "PR opened:" in m.text and all(u in m.text for u in urls)
        for m in thread.messages
    ):
        return
    msgs.append(tid, "agent", "\U0001f500 PR opened: " + ", ".join(urls), now, kind="note")


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
        # `_check_posted` resolves the thread and checks the dedup marker
        # under the lock (when one is given) — cheap, no subprocess. It does
        # NOT write the marker message: that write is deliberately deferred
        # to `_post_event`, called only AFTER the lifecycle hook below has
        # been attempted (see `_post_event`'s docstring for why — MOS-220
        # Greptile "Dedup Drops Hooks"). A restart-dedup skip (thread already
        # carried the event from a prior process) must not refire the hook.
        if self.lock is not None:
            with self.lock:
                already_posted, tid, wi = self._check_posted(slug, task, key)
        else:
            already_posted, tid, wi = self._check_posted(slug, task, key)
        if already_posted:
            return

        # Fire the hook BEFORE the durable dedup record (the mailbox event
        # message) is written — deliberately outside the lock (see
        # `_fire_lifecycle_hook`'s docstring) — a slow hook must not hold up
        # unrelated item/message operations (serve's `_item_msg_lock` also
        # guards POST /items/{id}/messages, /unattended, and /phase) — AND
        # deliberately before the durable write (see `_post_event`'s
        # docstring): if the process dies between the hook call and the
        # write, a restarted watcher sees no marker yet and refires the hook
        # next sweep rather than silently dropping it forever (at-least-once,
        # not at-most-zero). The happy path still posts — and dedups —
        # exactly once.
        self._fire_lifecycle_hook(st, slug, repo)

        if self.lock is not None:
            with self.lock:
                self._post_event(tid, slug, repo, url, st, key)
        else:
            self._post_event(tid, slug, repo, url, st, key)

    def _check_posted(
        self, slug: str, task: Any, key: tuple[str, str, str, str],
    ) -> tuple[bool, str, Any]:
        """Resolve the target thread and report whether its dedup marker
        (the mailbox event message) is already present — a prior, COMPLETE
        post+hook for this key (see `_post_event`: the marker is only ever
        written after the hook has been attempted, so its presence proves
        the hook already ran). Runs under the lock — cheap, no subprocess.
        Returns (already_posted, thread_id, work_item_or_None)."""
        _, _repo, url, st = key
        now = self.now_fn()
        tid, wi = self._resolve_thread(slug, task, url, now)

        thread = self.msgs.get(tid)
        marker = f"PR {st}: {url}"
        if thread is not None and any(
            m.kind == "event" and _refs_pr(m.text, marker)
            for m in thread.messages
        ):
            # Already posted (prior process) — record it so this process's
            # in-memory set short-circuits future checks too.
            self.notified.add(key)
            return True, tid, wi

        return False, tid, wi

    def _post_event(
        self, tid: str, slug: str, repo: str, url: str, st: str,
        key: tuple[str, str, str, str],
    ) -> None:
        """Write the durable dedup record (the mailbox event message) —
        called only AFTER the lifecycle hook has been attempted (see
        `_check_one`). This ordering IS the fix for "Dedup Drops Hooks": were
        the record written first (as it used to be), a crash between the
        write and the hook call would leave the marker in place with the
        hook having never run, and a restarted watcher would see the marker
        and skip the hook — silently, forever. Runs under the lock — must
        stay cheap."""
        now = self.now_fn()
        text = f"\U0001f500 PR {st}: {url} (task {slug}) — ready to close out."
        self.msgs.append(tid, "agent", text, now, kind="event")
        self.notified.add(key)

    def _fire_lifecycle_hook(self, st: str, slug: str, repo: str) -> None:
        """Fire the `pr.merged`/`pr.closed` lifecycle hook (MOS-220), once,
        BEFORE the mailbox event is durably posted (see `_post_event`'s
        docstring for why) — and, deliberately, OUTSIDE `self.lock`. A hook
        shells out and is bounded by its own (possibly generous) timeout;
        running it while holding `self.lock` (in `serve`, the same
        `_item_msg_lock` guarding POST /items/{id}/messages, /unattended,
        and /phase) would let one slow PR hook stall unrelated item/message
        HTTP requests for up to that timeout. Polling-derived — the
        merge/close already happened by the time this sweep observes it,
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

    def _resolve_thread(self, slug: str, task: Any, url: str, now: datetime) -> tuple[str, Any]:
        """Resolve the thread this PR merge/close event belongs to — the shared
        [resolve_task_thread] (WorkItem thread → url thread → task_slug thread → create),
        also used by `mship finish` at PR-open time."""
        return resolve_task_thread(self.msgs, self.workitems, slug, task, url, now)
