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
    return WorkspaceConfig(workspace="test", repos=repos or {}, hooks=hooks)


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
