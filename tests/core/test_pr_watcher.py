from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mship.core.message import Message, Thread
from mship.core.pr_watcher import PrWatcher

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)


def now_fn() -> datetime:
    return NOW


# --- small in-memory fakes mimicking the real store surfaces ---


@dataclass
class FakeTask:
    pr_urls: dict[str, str] = field(default_factory=dict)
    work_item_id: str | None = None
    worktrees: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeWorkItem:
    id: str
    thread_ids: list[str] = field(default_factory=list)


class FakeWorkItemStore:
    def __init__(self) -> None:
        self.items: dict[str, FakeWorkItem] = {}
        self.add_thread_calls: list[tuple[str, str]] = []

    def get(self, item_id):
        return self.items.get(item_id)

    def add_thread(self, item_id, thread_id, now=None):
        item = self.items[item_id]
        if thread_id not in item.thread_ids:
            item.thread_ids.append(thread_id)
        self.add_thread_calls.append((item_id, thread_id))


class FakeMessageStore:
    def __init__(self) -> None:
        self.threads: dict[str, Thread] = {}
        self.append_calls: list[dict] = []
        self._n = 0

    def _new_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    def create_thread(self, subject, text, now, task_slug=None) -> Thread:
        tid = self._new_id("th")
        thread = Thread(id=tid, subject=subject, created_at=now, updated_at=now, task_slug=task_slug)
        thread.messages.append(
            Message(id=self._new_id("msg"), thread_id=tid, role="human", text=text, created_at=now)
        )
        self.threads[tid] = thread
        return thread

    def append(self, thread_id, role, text, now, kind="note", decision=None) -> Message:
        thread = self.threads[thread_id]
        msg = Message(id=self._new_id("msg"), thread_id=thread_id, role=role, text=text,
                      created_at=now, kind=kind, decision=decision)
        thread.messages.append(msg)
        thread.updated_at = now
        self.append_calls.append({"thread_id": thread_id, "role": role, "text": text, "kind": kind})
        return msg

    def get(self, thread_id):
        return self.threads.get(thread_id)

    def list(self):
        return list(self.threads.values())


class FakeStateManager:
    def __init__(self, tasks: dict) -> None:
        self._tasks = tasks

    def load(self):
        return SimpleNamespace(tasks=self._tasks)


# --- tests ---


def test_open_to_merged_transition_posts_one_event():
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    watcher = PrWatcher(msgs, workitems, state, lambda url: "merged", now_fn)
    watcher.check_once()

    assert len(msgs.append_calls) == 1
    call = msgs.append_calls[0]
    assert call["kind"] == "event"
    assert call["thread_id"] == thread.id
    assert call["role"] == "agent"
    assert "https://github.com/org/repo1/pull/1" in call["text"]
    assert "merged" in call["text"]


def test_same_watcher_second_check_once_no_duplicate():
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})
    watcher = PrWatcher(msgs, workitems, state, lambda url: "merged", now_fn)

    watcher.check_once()
    watcher.check_once()

    assert len(msgs.append_calls) == 1


def test_fresh_watcher_thread_scan_idempotency():
    """A brand-new watcher (empty `notified` set) must not double-post if the
    thread already carries the event from a prior process (restart safety)."""
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    watcher1 = PrWatcher(msgs, workitems, state, lambda url: "merged", now_fn)
    watcher1.check_once()
    assert len(msgs.append_calls) == 1

    watcher2 = PrWatcher(msgs, workitems, state, lambda url: "merged", now_fn)
    watcher2.check_once()

    assert len(msgs.append_calls) == 1  # no duplicate


def test_thread_scan_does_not_false_dedup_on_state_word_in_url():
    """A repo/url containing a state word (e.g. `merged-pr-archive`) must not
    make the substring scan confuse a `closed` event for a `merged` one.
    A thread already carrying a `closed` event for such a url must still get
    a fresh `merged` event posted when that transition is observed."""
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    url = "https://github.com/org/merged-pr-archive/pull/1"
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    # Simulate a prior process having already posted the `closed` event for
    # this url — note the url itself contains the substring "merged".
    msgs.append(
        thread.id, "agent",
        f"\U0001f500 PR closed: {url} (task task-1) — ready to close out.",
        NOW, kind="event",
    )
    task = FakeTask(pr_urls={"repo1": url}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    watcher = PrWatcher(msgs, workitems, state, lambda u: "merged", now_fn)
    watcher.check_once()

    # The closed-event append plus a new merged-event append.
    assert len(msgs.append_calls) == 2
    merged_call = msgs.append_calls[-1]
    assert merged_call["kind"] == "event"
    assert merged_call["thread_id"] == thread.id
    assert f"PR merged: {url}" in merged_call["text"]


@pytest.mark.parametrize("pr_state", ["open", "unknown"])
def test_open_or_unknown_state_no_event(pr_state):
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    watcher = PrWatcher(msgs, workitems, state, lambda url: pr_state, now_fn)
    watcher.check_once()

    assert msgs.append_calls == []


def test_workitem_with_no_thread_creates_and_links():
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    workitems.items["wi-2"] = FakeWorkItem(id="wi-2", thread_ids=[])
    task = FakeTask(pr_urls={"repoB": "https://github.com/org/repoB/pull/9"}, work_item_id="wi-2")
    state = FakeStateManager({"task-2": task})

    watcher = PrWatcher(msgs, workitems, state, lambda url: "closed", now_fn)
    watcher.check_once()

    assert workitems.add_thread_calls, "expected add_thread to be called"
    item_id, new_tid = workitems.add_thread_calls[0]
    assert item_id == "wi-2"
    assert workitems.items["wi-2"].thread_ids == [new_tid]

    assert len(msgs.append_calls) == 1
    call = msgs.append_calls[0]
    assert call["thread_id"] == new_tid
    assert call["kind"] == "event"
    assert "closed" in call["text"]

    new_thread = msgs.get(new_tid)
    assert new_thread is not None
    # create_thread seeds a human message; the event itself must be an agent message.
    assert new_thread.messages[0].role == "human"
    assert new_thread.messages[-1].role == "agent"
    assert new_thread.messages[-1].kind == "event"


def test_reuses_existing_thread_that_contains_pr_url():
    # A CLI-spawned task's WorkItem has no thread, but the PR url was already posted in
    # some thread (a dispatch/chat message). The merge event must land THERE — not in a
    # fresh thread (the clutter the operator reported) — and link that thread to the
    # WorkItem so later events for the item consolidate there too.
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    workitems.items["wi-5"] = FakeWorkItem(id="wi-5", thread_ids=[])
    url = "https://github.com/org/repoE/pull/45"
    chat = msgs.create_thread(subject="chat", text="seed", now=NOW, task_slug=None)
    msgs.append(chat.id, "agent", f"PR is up: {url}", NOW, kind="note")
    task = FakeTask(pr_urls={"repoE": url}, work_item_id="wi-5")
    state = FakeStateManager({"task-5": task})

    watcher = PrWatcher(msgs, workitems, state, lambda u: "merged", now_fn)
    before = len(msgs.threads)
    watcher.check_once()

    assert len(msgs.threads) == before  # no new thread spawned
    events = [c for c in msgs.append_calls if c["kind"] == "event"]
    assert len(events) == 1
    assert events[0]["thread_id"] == chat.id  # event posted into the url thread
    assert "merged" in events[0]["text"]
    assert workitems.items["wi-5"].thread_ids == [chat.id]  # linked to the WorkItem


def test_workitem_thread_wins_over_pr_url_thread():
    # When the WorkItem already has its own canonical thread, that wins even if another
    # thread also mentions the url — the item's events stay consolidated on its thread.
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    canonical = msgs.create_thread(subject="task-6", text="seed", now=NOW, task_slug="task-6")
    workitems.items["wi-6"] = FakeWorkItem(id="wi-6", thread_ids=[canonical.id])
    url = "https://github.com/org/repoF/pull/6"
    other = msgs.create_thread(subject="chat", text="seed", now=NOW, task_slug=None)
    msgs.append(other.id, "agent", f"see {url}", NOW, kind="note")
    task = FakeTask(pr_urls={"repoF": url}, work_item_id="wi-6")
    state = FakeStateManager({"task-6": task})

    watcher = PrWatcher(msgs, workitems, state, lambda u: "merged", now_fn)
    watcher.check_once()

    events = [c for c in msgs.append_calls if c["kind"] == "event"]
    assert len(events) == 1
    assert events[0]["thread_id"] == canonical.id  # WorkItem's own thread, not the url thread


def test_pr_url_match_is_not_a_numeric_prefix():
    # Resolving .../pull/4 must NOT reuse a thread that only mentions .../pull/42 (a numeric
    # prefix). With no thread carrying pull/4, a fresh thread is created + linked instead.
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    workitems.items["wi-7"] = FakeWorkItem(id="wi-7", thread_ids=[])
    other = msgs.create_thread(subject="chat", text="seed", now=NOW, task_slug=None)
    msgs.append(other.id, "agent", "see https://github.com/org/repoG/pull/42", NOW, kind="note")
    task = FakeTask(pr_urls={"repoG": "https://github.com/org/repoG/pull/4"}, work_item_id="wi-7")
    state = FakeStateManager({"task-7": task})

    watcher = PrWatcher(msgs, workitems, state, lambda u: "merged", now_fn)
    watcher.check_once()

    events = [c for c in msgs.append_calls if c["kind"] == "event"]
    assert len(events) == 1
    assert events[0]["thread_id"] != other.id  # did not post into the pull/42 thread
    assert workitems.items["wi-7"].thread_ids == [events[0]["thread_id"]]  # fresh thread, linked


def test_no_work_item_uses_existing_thread_by_task_slug():
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-3", text="seed", now=NOW, task_slug="task-3")
    task = FakeTask(pr_urls={"repoC": "https://github.com/org/repoC/pull/3"}, work_item_id=None)
    state = FakeStateManager({"task-3": task})

    watcher = PrWatcher(msgs, workitems, state, lambda url: "merged", now_fn)
    watcher.check_once()

    assert len(msgs.threads) == 1  # no new thread created
    assert len(msgs.append_calls) == 1
    assert msgs.append_calls[0]["thread_id"] == thread.id


def test_no_work_item_and_no_existing_thread_creates_new():
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    task = FakeTask(pr_urls={"repoD": "https://github.com/org/repoD/pull/4"}, work_item_id=None)
    state = FakeStateManager({"task-4": task})

    watcher = PrWatcher(msgs, workitems, state, lambda url: "merged", now_fn)
    watcher.check_once()

    assert len(msgs.threads) == 1
    assert workitems.add_thread_calls == []
    new_thread = next(iter(msgs.threads.values()))
    assert new_thread.task_slug == "task-4"
    assert len(msgs.append_calls) == 1


def test_one_bad_pr_does_not_abort_sweep():
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread_good = msgs.create_thread(subject="task-good", text="seed", now=NOW, task_slug="task-good")
    bad_task = FakeTask(pr_urls={"repoBad": "https://github.com/org/bad/pull/1"})
    good_task = FakeTask(pr_urls={"repoGood": "https://github.com/org/good/pull/2"})
    state = FakeStateManager({"task-bad": bad_task, "task-good": good_task})

    def check_state(url):
        if "bad" in url:
            raise RuntimeError("boom")
        return "merged"

    watcher = PrWatcher(msgs, workitems, state, check_state, now_fn)
    watcher.check_once()  # must not raise

    assert len(msgs.append_calls) == 1
    assert msgs.append_calls[0]["thread_id"] == thread_good.id


def test_no_pr_urls_skipped():
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    task = FakeTask(pr_urls={})
    state = FakeStateManager({"task-5": task})
    watcher = PrWatcher(msgs, workitems, state, lambda url: "merged", now_fn)
    watcher.check_once()
    assert msgs.append_calls == []


# ---------------------------------------------------------------------------
# Lifecycle hooks (MOS-220, spec mship-lifecycle-hooks): `pr.merged`/`pr.closed`
# ---------------------------------------------------------------------------


class _RecordingShell:
    """Minimal ShellRunner-shaped fake for asserting lifecycle-hook execution."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_substrings: set[str] = set()

    def build_command(self, command, env_runner=None):
        return f"{env_runner} {command}" if env_runner else command

    def run(self, command, cwd, env=None, timeout=None):
        from pathlib import Path as _Path
        from mship.util.shell import ShellResult
        self.calls.append({"command": command, "cwd": _Path(cwd)})
        if any(s in command for s in self.fail_substrings):
            return ShellResult(returncode=1, stdout="", stderr="boom")
        return ShellResult(returncode=0, stdout="", stderr="")


def _hooks_config(hooks, repos=None):
    from mship.core.config import WorkspaceConfig
    return WorkspaceConfig(workspace="test", repos=repos or {}, lifecycle_hooks=hooks)


def test_pr_merged_fires_matching_hook():
    from mship.core.config import HookConfig

    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    shell = _RecordingShell()
    config = _hooks_config([HookConfig(on="pr.merged", run="task notify-merge")])

    watcher = PrWatcher(
        msgs, workitems, state, lambda url: "merged", now_fn,
        config=config, workspace_root=Path("/ws"), shell=shell,
    )
    watcher.check_once()

    assert any("notify-merge" in c["command"] for c in shell.calls)


def test_pr_closed_fires_only_the_closed_hook_not_merged():
    from mship.core.config import HookConfig

    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    shell = _RecordingShell()
    config = _hooks_config([
        HookConfig(on="pr.merged", run="task notify-merge"),
        HookConfig(on="pr.closed", run="task notify-closed"),
    ])

    watcher = PrWatcher(
        msgs, workitems, state, lambda url: "closed", now_fn,
        config=config, workspace_root=Path("/ws"), shell=shell,
    )
    watcher.check_once()

    commands = [c["command"] for c in shell.calls]
    assert any("notify-closed" in c for c in commands)
    assert not any("notify-merge" in c for c in commands)


def test_pr_hook_runs_from_the_prs_own_repo(tmp_path: Path):
    from mship.core.config import HookConfig, RepoConfig

    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    shell = _RecordingShell()
    config = _hooks_config(
        [HookConfig(on="pr.merged", run="task notify-merge")],
        repos={"repo1": RepoConfig(path=tmp_path / "repo1", type="service", env_runner="direnv exec --")},
    )

    watcher = PrWatcher(
        msgs, workitems, state, lambda url: "merged", now_fn,
        config=config, workspace_root=tmp_path, shell=shell,
    )
    watcher.check_once()

    call = shell.calls[0]
    assert call["cwd"] == tmp_path / "repo1"
    assert call["command"] == "direnv exec -- task notify-merge"


def test_pr_hook_failure_does_not_abort_sweep_or_block_message():
    from mship.core.config import HookConfig

    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    shell = _RecordingShell()
    shell.fail_substrings.add("notify-merge")
    config = _hooks_config([HookConfig(on="pr.merged", run="task notify-merge")])

    watcher = PrWatcher(
        msgs, workitems, state, lambda url: "merged", now_fn,
        config=config, workspace_root=Path("/ws"), shell=shell,
    )
    watcher.check_once()  # must not raise

    assert len(msgs.append_calls) == 1  # message still posted


def test_pr_watcher_without_config_is_unaffected():
    """Default construction (no config/workspace_root/shell) — existing
    behavior, no lifecycle hooks evaluated at all."""
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    watcher = PrWatcher(msgs, workitems, state, lambda url: "merged", now_fn)
    watcher.check_once()  # must not raise
    assert len(msgs.append_calls) == 1


def test_pr_hook_not_refired_on_second_sweep():
    from mship.core.config import HookConfig

    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    shell = _RecordingShell()
    config = _hooks_config([HookConfig(on="pr.merged", run="task notify-merge")])

    watcher = PrWatcher(
        msgs, workitems, state, lambda url: "merged", now_fn,
        config=config, workspace_root=Path("/ws"), shell=shell,
    )
    watcher.check_once()
    watcher.check_once()

    assert len(shell.calls) == 1


def test_pr_hook_not_refired_on_restart_dedup():
    """A brand-new watcher (empty `notified` set) whose target thread already
    carries the event from a prior process must not refire the hook — only a
    genuinely NEW post (not a restart-dedup skip) may trigger it. Guards the
    `_check_one`/`_check_posted`/`_post_event` refactor that moved the hook
    call outside the lock: the dedup signal must still gate it correctly."""
    from mship.core.config import HookConfig

    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    url = "https://github.com/org/repo1/pull/1"
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    # Simulate a prior process having already posted the event.
    msgs.append(
        thread.id, "agent",
        f"\U0001f500 PR merged: {url} (task task-1) — ready to close out.",
        NOW, kind="event",
    )
    task = FakeTask(pr_urls={"repo1": url}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    shell = _RecordingShell()
    config = _hooks_config([HookConfig(on="pr.merged", run="task notify-merge")])

    watcher = PrWatcher(
        msgs, workitems, state, lambda u: "merged", now_fn,
        config=config, workspace_root=Path("/ws"), shell=shell,
    )
    watcher.check_once()

    assert shell.calls == []  # dedup-skipped post must not fire the hook


def test_pr_hook_fires_outside_the_lock():
    """MOS-220 Greptile fix ("PR Hook Holds Item Lock"): the lifecycle hook
    must run AFTER the watcher releases its lock. In `serve` this lock is the
    same `_item_msg_lock` guarding POST /items/{id}/messages, /unattended, and
    /phase — a slow hook holding it would stall unrelated item/message HTTP
    requests for up to the hook's timeout."""
    import threading

    from mship.core.config import HookConfig

    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    lock = threading.Lock()
    observed_locked: list[bool] = []

    class _AssertingShell(_RecordingShell):
        def run(self, command, cwd, env=None, timeout=None):
            observed_locked.append(lock.locked())
            return super().run(command, cwd, env=env, timeout=timeout)

    shell = _AssertingShell()
    config = _hooks_config([HookConfig(on="pr.merged", run="task notify-merge")])

    watcher = PrWatcher(
        msgs, workitems, state, lambda url: "merged", now_fn,
        lock=lock, config=config, workspace_root=Path("/ws"), shell=shell,
    )
    watcher.check_once()

    assert len(shell.calls) == 1
    assert observed_locked == [False]  # the lock was free while the hook ran


def test_pr_hook_fires_before_dedup_marker_is_durably_recorded():
    """MOS-220 Greptile fix ("Dedup Drops Hooks"): the hook must fire BEFORE
    the durable dedup record (the mailbox event message) is written, not
    after. If the message were written first, a crash between the write and
    the hook call would leave the marker in place forever with the hook
    having never run, and a restarted watcher would see the marker and skip
    the hook — silently, permanently. Proven by recording the relative order
    of the hook call and the message append."""
    from mship.core.config import HookConfig

    order: list[str] = []

    class _OrderTrackingShell(_RecordingShell):
        def run(self, command, cwd, env=None, timeout=None):
            order.append("hook")
            return super().run(command, cwd, env=env, timeout=timeout)

    class _OrderTrackingMessageStore(FakeMessageStore):
        def append(self, *a, **kw):
            order.append("append")
            return super().append(*a, **kw)

    msgs = _OrderTrackingMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    shell = _OrderTrackingShell()
    config = _hooks_config([HookConfig(on="pr.merged", run="task notify-merge")])

    watcher = PrWatcher(
        msgs, workitems, state, lambda url: "merged", now_fn,
        config=config, workspace_root=Path("/ws"), shell=shell,
    )
    watcher.check_once()

    assert order == ["hook", "append"]
    assert len(msgs.append_calls) == 1


def test_pr_hook_refires_after_crash_before_dedup_marker_recorded():
    """MOS-220 Greptile fix ("Dedup Drops Hooks"): simulates the crash window
    directly — the durable append (the dedup marker) fails the first time,
    as if the process died before it could land, even though the hook had
    already fired. A subsequent sweep (the "restart") must see that no
    marker was durably recorded and refire the hook rather than treating it
    as already handled — at-least-once beats at-most-zero for hooks. Once
    the append succeeds, the event is recorded exactly once (happy-path
    dedup is preserved)."""
    from mship.core.config import HookConfig

    class _FlakyMessageStore(FakeMessageStore):
        def __init__(self) -> None:
            super().__init__()
            self.append_attempts = 0

        def append(self, *a, **kw):
            self.append_attempts += 1
            if self.append_attempts == 1:
                raise RuntimeError("simulated crash before durable write lands")
            return super().append(*a, **kw)

    msgs = _FlakyMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    task = FakeTask(pr_urls={"repo1": "https://github.com/org/repo1/pull/1"}, work_item_id="wi-1")
    state = FakeStateManager({"task-1": task})

    shell = _RecordingShell()
    config = _hooks_config([HookConfig(on="pr.merged", run="task notify-merge")])

    watcher = PrWatcher(
        msgs, workitems, state, lambda url: "merged", now_fn,
        config=config, workspace_root=Path("/ws"), shell=shell,
    )

    watcher.check_once()  # hook fires; the durable append then "crashes"
    assert len(shell.calls) == 1
    assert msgs.append_calls == []  # nothing durably recorded yet

    watcher.check_once()  # re-check after the "crash": must refire, not skip
    assert len(shell.calls) == 2
    assert len(msgs.append_calls) == 1  # now durably recorded, exactly once


def test_announce_prs_on_thread_posts_to_workitem_thread():
    from mship.core.pr_watcher import announce_prs_on_thread
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    thread = msgs.create_thread(subject="task-1", text="seed", now=NOW, task_slug="task-1")
    workitems.items["wi-1"] = FakeWorkItem(id="wi-1", thread_ids=[thread.id])
    pr_list = [{"repo": "r1", "url": "https://github.com/org/r1/pull/1"},
               {"repo": "r2", "url": "https://github.com/org/r2/pull/2"}]
    task = FakeTask(pr_urls={}, work_item_id="wi-1")

    announce_prs_on_thread(msgs, workitems, "task-1", task, pr_list, NOW)

    events = [c for c in msgs.append_calls if c["kind"] == "event"]
    assert len(events) == 1
    assert events[0]["thread_id"] == thread.id
    assert "PR opened:" in events[0]["text"]
    assert "https://github.com/org/r1/pull/1" in events[0]["text"]
    assert "https://github.com/org/r2/pull/2" in events[0]["text"]
    # idempotent: a second announce (e.g. --force re-finish) does not double-post
    announce_prs_on_thread(msgs, workitems, "task-1", task, pr_list, NOW)
    assert len([c for c in msgs.append_calls if c["kind"] == "event"]) == 1


def test_announce_creates_and_links_thread_when_workitem_has_none():
    from mship.core.pr_watcher import announce_prs_on_thread
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    workitems.items["wi-9"] = FakeWorkItem(id="wi-9", thread_ids=[])
    task = FakeTask(pr_urls={}, work_item_id="wi-9")

    announce_prs_on_thread(msgs, workitems, "task-9", task,
                           [{"repo": "r", "url": "https://github.com/org/r/pull/7"}], NOW)

    events = [c for c in msgs.append_calls if c["kind"] == "event"]
    assert len(events) == 1
    new_tid = events[0]["thread_id"]
    assert workitems.items["wi-9"].thread_ids == [new_tid]  # linked, so later events reuse it


def test_finish_announce_then_watcher_merge_share_one_thread():
    # The end-to-end win: finish announces the PR url (creates+links the thread), and the later
    # merge event lands in that SAME thread — no second thread spawned per task.
    from mship.core.pr_watcher import announce_prs_on_thread
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    workitems.items["wi-5"] = FakeWorkItem(id="wi-5", thread_ids=[])
    url = "https://github.com/org/r/pull/12"
    task = FakeTask(pr_urls={"r": url}, work_item_id="wi-5")
    state = FakeStateManager({"task-5": task})

    announce_prs_on_thread(msgs, workitems, "task-5", task, [{"repo": "r", "url": url}], NOW)
    threads_after_open = len(msgs.threads)

    PrWatcher(msgs, workitems, state, lambda u: "merged", now_fn).check_once()

    assert len(msgs.threads) == threads_after_open  # merge spawned no new thread
    events = [c for c in msgs.append_calls if c["kind"] == "event"]
    assert len({e["thread_id"] for e in events}) == 1  # opened + merged on one thread
    assert any("opened" in e["text"] for e in events) and any("merged" in e["text"] for e in events)


def test_resolve_links_task_slug_thread_to_workitem():
    # A WorkItem with no thread + an existing task_slug thread (no url match): resolve must reuse
    # AND link it, so later WorkItem events don't fall through and spawn a divergent thread.
    from mship.core.pr_watcher import resolve_task_thread
    msgs = FakeMessageStore()
    workitems = FakeWorkItemStore()
    workitems.items["wi-8"] = FakeWorkItem(id="wi-8", thread_ids=[])
    thread = msgs.create_thread(subject="task-8", text="seed", now=NOW, task_slug="task-8")
    task = FakeTask(pr_urls={}, work_item_id="wi-8")

    tid, _wi = resolve_task_thread(msgs, workitems, "task-8", task, "https://github.com/o/r/pull/8", NOW)

    assert tid == thread.id
    assert workitems.items["wi-8"].thread_ids == [thread.id]  # linked
