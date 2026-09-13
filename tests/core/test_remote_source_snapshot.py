"""Regression coverage for frozen multi-host source snapshots."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mship.core import remote_preflight, run_transfer
from mship.core.remote_dispatch import (
    RemoteDispatchError,
    SourceSnapshot,
    _DirtySource,
    prepare_remote_source,
    snapshot_remote_source,
)
from mship.core.run_host import RunHostConnection
from mship.util.shell import ShellRunner


class _Task:
    slug = "task-1"
    branch = "task-1"


class _Output:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def breadcrumb(self, message: str) -> None:
        self.messages.append(message)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.txt").write_text("base\n")
    _git(repo, "add", "app.txt")
    _git(
        repo,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    return repo, _git(repo, "rev-parse", "HEAD")


def _dirty_preflight(repo: Path, head: str, *, aliases: tuple[str, ...] = ("app",)):
    states = [
        remote_preflight.RepoState(
            repo=alias,
            path=repo if alias == aliases[0] else repo / alias,
            branch="task-1",
            blocked_reason=None,
            detail=None,
            dirty=True,
            needs_push=False,
            push_reason=None,
            head_sha=head,
            git_repo="app",
        )
        for alias in aliases
    ]
    return remote_preflight.Preflight(
        states=states, blocked=[], to_push=[], dirty=[states[0]]
    )


def test_frozen_dirty_snapshot_reaches_every_host_after_local_edits(
    tmp_path, monkeypatch
):
    repo, head = _repo(tmp_path)
    (repo / "app.txt").write_text("first snapshot\n")
    preflight = _dirty_preflight(repo, head)
    monkeypatch.setattr(remote_preflight, "inspect", lambda *args, **kwargs: preflight)

    snapshot = snapshot_remote_source(
        task_obj=_Task(), target_repos=["app"], config=None, shell=ShellRunner()
    )
    frozen_sha = snapshot.source_revisions["app"]
    assert _git(repo, "show", f"{frozen_sha}:app.txt") == "first snapshot"

    # A later local edit must not change the already-certified object delivered
    # to the next eligible host.
    (repo / "app.txt").write_text("second local edit\n")
    deliveries: list[tuple[str, str]] = []

    def push(shell, path, *, conn, repo, task, sha):
        deliveries.append((conn.url, sha))
        return f"refs/mship/run/{task}/{repo}"

    monkeypatch.setattr(run_transfer, "push_run_ref", push)
    output = _Output()
    for host in ("https://one.invalid", "https://two.invalid"):
        prepare_remote_source(
            task_obj=_Task(),
            target_repos=["app"],
            config=None,
            shell=ShellRunner(),
            conn=RunHostConnection(host, "private"),
            output=output,
            snapshot=snapshot,
        )

    assert deliveries == [
        ("https://one.invalid", frozen_sha),
        ("https://two.invalid", frozen_sha),
    ]


def test_snapshot_pins_all_selected_git_root_aliases_to_one_dirty_tree(
    tmp_path, monkeypatch
):
    repo, head = _repo(tmp_path)
    (repo / "api").mkdir()
    (repo / "api" / "child.txt").write_text("child\n")
    (repo / "app.txt").write_text("root edit\n")
    preflight = _dirty_preflight(repo, head, aliases=("app", "api"))
    monkeypatch.setattr(remote_preflight, "inspect", lambda *args, **kwargs: preflight)

    snapshot = snapshot_remote_source(
        task_obj=_Task(), target_repos=["app", "api"], config=None, shell=ShellRunner()
    )

    assert snapshot.source_revisions["app"] == snapshot.source_revisions["api"]
    assert (
        _git(repo, "show", f"{snapshot.source_revisions['api']}:app.txt") == "root edit"
    )


def _single_dirty_snapshot(tmp_path: Path) -> tuple[SourceSnapshot, _DirtySource]:
    source = _DirtySource("app", tmp_path / "app", "task-1", "a" * 40)
    return SourceSnapshot(
        {"app": source.sha}, SimpleNamespace(to_push=[]), (source,)
    ), source


def test_receipt_failure_rolls_back_only_the_transferred_ref_with_its_lease(
    tmp_path, monkeypatch
):
    snapshot, source = _single_dirty_snapshot(tmp_path)
    deleted: list[dict[str, object]] = []
    monkeypatch.setattr(
        run_transfer,
        "push_run_ref",
        lambda _shell, _path, **_kwargs: "refs/mship/run/task-1/app",
    )
    monkeypatch.setattr(
        run_transfer,
        "delete_run_ref",
        lambda _shell, path, **kwargs: deleted.append({"path": path, **kwargs}),
    )
    monkeypatch.setattr(remote_preflight, "push", lambda _preflight, _shell: ([], None))

    with pytest.raises(RemoteDispatchError, match="rolled back") as error:
        prepare_remote_source(
            task_obj=_Task(),
            target_repos=["app"],
            config=None,
            shell=ShellRunner(),
            conn=RunHostConnection("https://host.invalid", "private-token"),
            output=_Output(),
            snapshot=snapshot,
            on_transfer=lambda *_args: (_ for _ in ()).throw(
                OSError("receipt disk failure private-token")
            ),
        )

    assert "private-token" not in str(error.value)
    assert deleted == [
        {
            "path": source.path,
            "conn": RunHostConnection("https://host.invalid", "private-token"),
            "repo": "app",
            "task": "task-1",
            "expected_sha": source.sha,
        }
    ]


def test_receipt_failure_reports_unresolved_cleanup_when_leased_rollback_fails(
    tmp_path, monkeypatch
):
    snapshot, _source = _single_dirty_snapshot(tmp_path)
    monkeypatch.setattr(
        run_transfer,
        "push_run_ref",
        lambda _shell, _path, **_kwargs: "refs/mship/run/task-1/app",
    )

    def fail_rollback(*_args, **_kwargs):
        raise run_transfer.RunTransferError("Bearer private-token")

    monkeypatch.setattr(run_transfer, "delete_run_ref", fail_rollback)
    monkeypatch.setattr(remote_preflight, "push", lambda _preflight, _shell: ([], None))

    with pytest.raises(RemoteDispatchError, match="unresolved") as error:
        prepare_remote_source(
            task_obj=_Task(),
            target_repos=["app"],
            config=None,
            shell=ShellRunner(),
            conn=RunHostConnection("https://host.invalid", "private-token"),
            output=_Output(),
            snapshot=snapshot,
            on_transfer=lambda *_args: (_ for _ in ()).throw(
                OSError("receipt disk failure private-token")
            ),
        )

    assert "private-token" not in str(error.value)
