"""Exact-host source-transfer receipt regressions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from mship.core import run_transfer

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.remote_dispatch import (
    RemoteDispatchError,
    SourceSnapshot,
    _RunRefSource,
    prepare_remote_source,
)
from mship.core.run_host import HostRegistration, RunHostConnection, RunHostResolver, RunHostStore
from mship.core.run_ref import run_ref
from mship.core.run_transfer import (
    RunTransferError,
    cleanup_recorded_run_refs,
    cleanup_remote_task_worktrees,
    cleanup_run_refs,
    push_run_ref,
    record_remote_worktree_receipt,
    record_run_ref_receipt,
)
from mship.util.shell import ShellResult, ShellRunner


class _Task:
    slug = "task-1"
    affected_repos = ("api",)


class _Output:
    def breadcrumb(self, _message: str) -> None:
        pass


class _Shell:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def run(self, command: str, *, cwd: Path, env=None):
        _ = env
        self.calls.append((command, cwd))
        return ShellResult(returncode=0, stdout="", stderr="")


def _host(name: str, url: str) -> HostRegistration:
    return HostRegistration(
        name=name,
        roles=("ios",),
        tags=(),
        preference=0,
        connection=RunHostConnection(url, "never-persist-this-token"),
        scope="project",
    )


def _config(tmp_path: Path) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace="workspace",
        run_hosts=["ios"],
        repos={
            "api": RepoConfig(path=tmp_path / "api", type="service", run_host="ios")
        },
    )


def _receipts(state_dir: Path) -> list[dict[str, str]]:
    return json.loads((state_dir / "run-ref-receipts.json").read_text())["receipts"]


def test_only_successful_transfers_are_recorded_when_later_host_source_fails(
    tmp_path, monkeypatch
):
    state_dir = tmp_path / ".mothership"
    host = _host("studio", "https://studio.invalid")
    first = _RunRefSource("api", tmp_path / "api", "task-1", "a" * 40)
    second = _RunRefSource("web", tmp_path / "web", "task-1", "b" * 40)
    snapshot = SourceSnapshot(
        {"api": first.sha, "web": second.sha},
        SimpleNamespace(to_push=[]),
        (first, second),
    )

    def push(_shell, _path, *, conn, workspace_id, repo, task, sha):
        if repo == "web":
            raise RunTransferError("host transfer failed")
        return run_ref(task, repo)

    monkeypatch.setattr("mship.core.run_transfer.push_run_ref", push)
    with pytest.raises(RemoteDispatchError, match="host transfer failed"):
        prepare_remote_source(
            task_obj=_Task(),
            target_repos=["api", "web"],
            config=None,
            shell=_Shell(),
            host=host,
            resolver=RunHostResolver(),
            output=_Output(),
            snapshot=snapshot,
            on_transfer=lambda repo, ref, sha: record_run_ref_receipt(
                state_dir, task=_Task(), host=host, repo=repo, ref=ref, sha=sha
            ),
        )

    assert _receipts(state_dir) == [
        {
            "task": "task-1",
            "host_name": "studio",
            "host_scope": "project",
            "host_endpoint_fingerprint": _receipts(state_dir)[0][
                "host_endpoint_fingerprint"
            ],
            "repo": "api",
            "ref": "refs/mship/run/task-1/api",
            "sha": "a" * 40,
        }
    ]
    assert (
        "never-persist-this-token"
        not in (state_dir / "run-ref-receipts.json").read_text()
    )


def test_cleanup_refuses_changed_endpoint_and_retains_the_receipt(tmp_path):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    original = _host("studio", "https://studio.invalid")
    store.set_host(original, scope="project")
    record_run_ref_receipt(
        state_dir,
        task=_Task(),
        host=original,
        repo="api",
        ref=run_ref("task-1", "api"),
        sha="a" * 40,
    )
    store.set_host(_host("studio", "https://replacement.invalid"), scope="project")
    shell = _Shell()
    warnings: list[str] = []

    assert (
        cleanup_recorded_run_refs(
            _Task(),
            config=_config(tmp_path),
            store=store,
            shell=shell,
            warn=warnings.append,
        )
        == []
    )
    assert shell.calls == []
    assert _receipts(state_dir)[0]["host_name"] == "studio"
    assert warnings and "identity changed" in warnings[0]


def test_cleanup_never_retargets_a_recorded_host_through_a_pooled_role(tmp_path):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    original = _host("studio", "https://studio.invalid")
    store.set_host(original, scope="project")
    record_run_ref_receipt(
        state_dir,
        task=_Task(),
        host=original,
        repo="api",
        ref=run_ref("task-1", "api"),
        sha="a" * 40,
    )
    store.remove_host("studio", scope="project")
    store.set_host(_host("portable", "https://portable.invalid"), scope="project")
    store.set_role_hosts("ios", None)
    shell = _Shell()
    warnings: list[str] = []

    assert (
        cleanup_recorded_run_refs(
            _Task(),
            config=_config(tmp_path),
            store=store,
            shell=shell,
            warn=warnings.append,
        )
        == []
    )
    assert shell.calls == []
    assert _receipts(state_dir)[0]["host_name"] == "studio"
    assert warnings and "studio" in warnings[0]


def test_cleanup_deletes_only_the_recorded_ref_then_removes_its_receipt(tmp_path):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    host = _host("studio", "https://studio.invalid")
    store.set_host(host, scope="project")
    record_run_ref_receipt(
        state_dir,
        task=_Task(),
        host=host,
        repo="api",
        ref=run_ref("task-1", "api"),
        sha="a" * 40,
    )
    shell = _Shell()

    assert cleanup_recorded_run_refs(
        _Task(), config=_config(tmp_path), store=store, shell=shell, warn=pytest.fail
    ) == ["api"]
    assert shell.calls and shell.calls[0][0].endswith(":refs/mship/run/task-1/api")
    assert _receipts(state_dir) == []


def test_task_close_cleans_a_receipt_on_its_recorded_host_after_role_changes(
    tmp_path,
):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    recorded_host = _host("studio", "https://studio.invalid")
    store.set_host(recorded_host, scope="project")
    record_run_ref_receipt(
        state_dir,
        task=_Task(),
        host=recorded_host,
        repo="api",
        ref=run_ref("task-1", "api"),
        sha="a" * 40,
    )
    replacement = _host("portable", "https://portable.invalid")
    store.set_host(replacement, scope="project")
    store.set_role_hosts("ios", ("portable",))
    shell = _Shell()

    assert cleanup_run_refs(
        _Task(), config=_config(tmp_path), store=store, shell=shell, warn=pytest.fail
    ) == ["api"]
    assert len(shell.calls) == 1
    assert "https://studio.invalid/git/api" in shell.calls[0][0]
    assert "https://portable.invalid" not in shell.calls[0][0]


def test_task_close_does_not_fall_back_to_role_lookup_after_receipt_failure(
    tmp_path,
):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    original = _host("studio", "https://studio.invalid")
    store.set_host(original, scope="project")
    record_run_ref_receipt(
        state_dir,
        task=_Task(),
        host=original,
        repo="api",
        ref=run_ref("task-1", "api"),
        sha="a" * 40,
    )
    store.remove_host("studio", scope="project")
    store.set_host(_host("portable", "https://portable.invalid"), scope="project")
    store.set_role_hosts("ios", ("portable",))
    shell = _Shell()
    warnings: list[str] = []

    assert (
        cleanup_run_refs(
            _Task(),
            config=_config(tmp_path),
            store=store,
            shell=shell,
            warn=warnings.append,
        )
        == []
    )
    assert shell.calls == []
    assert _receipts(state_dir)[0]["host_name"] == "studio"
    assert warnings and "identity changed" in warnings[0]


def _git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def test_stale_receipt_cleanup_cannot_delete_a_newer_run_ref(tmp_path):
    state_dir = tmp_path / ".mothership"
    host_root = tmp_path / "host"
    host_repo = host_root / "git" / "api"
    host_repo.parent.mkdir(parents=True)
    _git(host_repo.parent, "init", "--bare", "api")
    operator = tmp_path / "operator"
    operator.mkdir()
    _git(operator, "init", "-q")
    _git(operator, "config", "user.name", "test")
    _git(operator, "config", "user.email", "test@example.invalid")
    (operator / "source.txt").write_text("first\n")
    _git(operator, "add", "source.txt")
    _git(operator, "commit", "-qm", "first")
    first = _git(operator, "rev-parse", "HEAD")
    host = _host("studio", f"file://{host_root}")
    push_run_ref(
        ShellRunner(),
        operator,
        conn=host.connection,
        repo="api",
        task="task-1",
        sha=first,
    )
    store = RunHostStore(state_dir)
    store.set_host(host, scope="project")
    record_run_ref_receipt(
        state_dir,
        task=_Task(),
        host=host,
        repo="api",
        ref=run_ref("task-1", "api"),
        sha=first,
    )
    (operator / "source.txt").write_text("newer\n")
    _git(operator, "commit", "-am", "newer")
    newer = _git(operator, "rev-parse", "HEAD")
    push_run_ref(
        ShellRunner(),
        operator,
        conn=host.connection,
        repo="api",
        task="task-1",
        sha=newer,
    )
    warnings: list[str] = []

    assert (
        cleanup_recorded_run_refs(
            _Task(),
            config=WorkspaceConfig(
                workspace="workspace",
                run_hosts=["ios"],
                repos={
                    "api": RepoConfig(path=operator, type="service", run_host="ios")
                },
            ),
            store=store,
            shell=ShellRunner(),
            warn=warnings.append,
        )
        == []
    )
    assert _git(host_repo, "rev-parse", "refs/mship/run/task-1/api") == newer
    assert _receipts(state_dir)[0]["sha"] == first
    assert warnings


class _RetryAfterReceiptWriteFailureShell:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.delete_attempts = 0

    def run(self, command: str, *, cwd: Path, env=None):
        _ = env
        self.calls.append((command, cwd))
        if command.startswith("git push "):
            self.delete_attempts += 1
            if self.delete_attempts == 1:
                return ShellResult(returncode=0, stdout="", stderr="")
            return ShellResult(returncode=1, stdout="", stderr="stale lease")
        pytest.fail(f"unexpected command: {command}")


def test_task_close_retains_lease_after_receipt_write_failure_and_uncertain_retry(
    tmp_path, monkeypatch
):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    host = _host("studio", "https://studio.invalid")
    store.set_host(host, scope="project")
    record_run_ref_receipt(
        state_dir,
        task=_Task(),
        host=host,
        repo="api",
        ref=run_ref("task-1", "api"),
        sha="a" * 40,
    )
    shell = _RetryAfterReceiptWriteFailureShell()
    warnings: list[str] = []
    original_write = run_transfer._write_receipts

    def fail_write(*_args):
        raise OSError("private receipt write detail")

    monkeypatch.setattr(run_transfer, "_write_receipts", fail_write)
    assert cleanup_run_refs(
        _Task(),
        config=_config(tmp_path),
        store=store,
        shell=shell,
        warn=warnings.append,
    ) == ["api"]
    assert _receipts(state_dir)
    assert shell.delete_attempts == 1
    assert all("private receipt write detail" not in warning for warning in warnings)

    monkeypatch.setattr(run_transfer, "_write_receipts", original_write)
    assert cleanup_run_refs(
        _Task(),
        config=_config(tmp_path),
        store=store,
        shell=shell,
        warn=warnings.append,
    ) == []
    assert shell.delete_attempts == 2
    assert _receipts(state_dir)[0]["sha"] == "a" * 40



def _worktree_receipts(state_dir: Path) -> list[dict[str, str]]:
    return json.loads(
        (state_dir / "remote-worktree-receipts.json").read_text()
    )["receipts"]


def test_source_update_transfer_advances_the_recorded_compare_and_delete_lease(
    tmp_path,
):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    host = _host("flutter", "https://flutter.invalid")
    store.set_host(host, scope="project")
    ref = run_ref("task-1", "api")
    record_run_ref_receipt(
        state_dir, task=_Task(), host=host, repo="api", ref=ref, sha="a" * 40
    )

    # This is the successful run_ref_only delivery made by source update.
    record_run_ref_receipt(
        state_dir, task=_Task(), host=host, repo="api", ref=ref, sha="b" * 40
    )

    assert _receipts(state_dir)[0]["sha"] == "b" * 40
    shell = _Shell()
    assert cleanup_recorded_run_refs(
        _Task(), config=_config(tmp_path), store=store, shell=shell, warn=pytest.fail
    ) == ["api"]
    assert f"--force-with-lease={ref}:{'b' * 40}" in shell.calls[0][0]


def test_close_removes_only_recorded_clean_ios_dirty_flutter_and_android_worktrees(
    tmp_path,
):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    hosts = (
        _host("native-ios", "https://ios.invalid"),
        _host("flutter", "https://flutter.invalid"),
        _host("android-build", "https://android.invalid"),
    )
    for host in hosts:
        store.set_host(host, scope="project")
        record_remote_worktree_receipt(
            state_dir, task=_Task(), host=host, repo="api", sha="b" * 40
        )
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def cleanup(**request):
        calls.append(
            (
                request["host"].name,
                request["task"],
                tuple(request["repos"]),
            )
        )
        return "removed"

    cleared = cleanup_remote_task_worktrees(
        _Task(),
        config=_config(tmp_path),
        store=store,
        warn=pytest.fail,
        cleanup_worktrees=cleanup,
    )

    assert {host.name for host in hosts} == {identity[0] for identity in cleared}
    assert sorted(calls) == [
        ("android-build", "task-1", ("api",)),
        ("flutter", "task-1", ("api",)),
        ("native-ios", "task-1", ("api",)),
    ]
    assert _worktree_receipts(state_dir) == []


def test_close_retains_ref_and_worktree_receipts_when_remote_teardown_is_unknown(
    tmp_path,
):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    host = _host("flutter", "https://flutter.invalid")
    store.set_host(host, scope="project")
    ref = run_ref("task-1", "api")
    record_run_ref_receipt(
        state_dir, task=_Task(), host=host, repo="api", ref=ref, sha="b" * 40
    )
    record_remote_worktree_receipt(
        state_dir, task=_Task(), host=host, repo="api", sha="b" * 40
    )
    warnings: list[str] = []

    cleared = cleanup_remote_task_worktrees(
        _Task(),
        config=_config(tmp_path),
        store=store,
        warn=warnings.append,
        cleanup_worktrees=lambda **_request: "unknown",
    )
    shell = _Shell()

    assert cleared == set()
    assert cleanup_run_refs(
        _Task(),
        config=_config(tmp_path),
        store=store,
        shell=shell,
        warn=warnings.append,
        cleared_worktree_hosts=cleared,
    ) == []
    assert shell.calls == []
    assert _receipts(state_dir)[0]["sha"] == "b" * 40
    assert _worktree_receipts(state_dir)[0]["host_name"] == "flutter"
    assert any("cleanup is not proven" in warning for warning in warnings)


def test_close_never_targets_a_host_without_a_delivery_receipt(tmp_path):
    state_dir = tmp_path / ".mothership"
    store = RunHostStore(state_dir)
    host = _host("android-build", "https://android.invalid")
    store.set_host(host, scope="project")

    assert cleanup_remote_task_worktrees(
        _Task(),
        config=_config(tmp_path),
        store=store,
        warn=pytest.fail,
        cleanup_worktrees=lambda **_request: pytest.fail("unexpected remote cleanup"),
    ) == set()
    shell = _Shell()
    assert cleanup_run_refs(
        _Task(),
        config=_config(tmp_path),
        store=store,
        shell=shell,
        warn=pytest.fail,
        cleared_worktree_hosts=set(),
    ) == []
    assert shell.calls == []
