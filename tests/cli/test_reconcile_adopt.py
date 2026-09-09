"""Behavioral regression coverage for explicit merged-PR metadata adoption."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.persistence.lifecycle_repository import LifecycleRepository
from mship.core.reconcile.cache import CachePayload, ReconcileCache
from mship.core.state import StateManager, Task, WorkspaceState
from mship.core.workitem_store import WorkItemStore
from mship.util.shell import ShellResult, ShellRunner
from tests.persistence_helpers import corrupt_workitem


_BRANCH = "feat/adopt"
_MERGED_AT = "2026-09-08T12:34:56Z"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


@dataclass(frozen=True)
class RepoFixture:
    name: str
    worktree: Path
    head: str
    merge: str


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_GIT_ENV,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _merged_repo(tmp_path: Path, name: str) -> RepoFixture:
    """Create a clean, pushed feature branch merged into a local bare origin."""
    bare = tmp_path / "origins" / f"{name}.git"
    bare.parent.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(bare)],
        env=_GIT_ENV,
        text=True,
        capture_output=True,
        check=True,
    )
    worktree = tmp_path / name
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(worktree)],
        env=_GIT_ENV,
        text=True,
        capture_output=True,
        check=True,
    )
    _git(worktree, "config", "user.name", "Test User")
    _git(worktree, "config", "user.email", "test@example.invalid")
    (worktree / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (worktree / "README.md").write_text(f"{name}\n")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", "base")
    _git(worktree, "push", "-q", "origin", "main")
    _git(worktree, "checkout", "-q", "-b", _BRANCH)
    (worktree / "README.md").write_text(f"{name} feature\n")
    _git(worktree, "commit", "-qam", "feature")
    head = _git(worktree, "rev-parse", "HEAD")
    _git(worktree, "push", "-q", "-u", "origin", _BRANCH)
    _git(worktree, "checkout", "-q", "main")
    _git(worktree, "merge", "--no-ff", "-qm", "merge", _BRANCH)
    merge = _git(worktree, "rev-parse", "HEAD")
    _git(worktree, "push", "-q", "origin", "main")
    _git(worktree, "checkout", "-q", _BRANCH)
    return RepoFixture(name=name, worktree=worktree, head=head, merge=merge)


def _pr(repo: RepoFixture, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "url": f"https://github.com/acme/{repo.name}/pull/7",
        "state": "MERGED",
        "mergedAt": _MERGED_AT,
        "mergeCommit": {"oid": repo.merge},
        "headRefName": _BRANCH,
        "headRefOid": repo.head,
        "baseRefName": "main",
    }
    value.update(changes)
    return value


class _GitHubFixtureShell(ShellRunner):
    """Runs fixture git commands for real and intercepts only GitHub-facing I/O."""

    def __init__(
        self,
        prs: dict[str, list[dict[str, object]]],
        *,
        after_reachability=None,
    ) -> None:
        super().__init__()
        self.prs = prs
        self.commands: list[str] = []
        self._after_reachability = after_reachability
        self._reached = False

    def run(self, command, cwd, env=None, timeout=None) -> ShellResult:
        self.commands.append(command)
        if command == "gh auth status":
            return ShellResult(returncode=0, stdout="", stderr="")
        if command == "git remote get-url origin":
            return ShellResult(
                returncode=0,
                stdout=f"https://github.com/acme/{Path(cwd).name}.git\n",
                stderr="",
            )
        if command.startswith("gh pr list "):
            return ShellResult(
                returncode=0,
                stdout=json.dumps(self.prs[Path(cwd).name]),
                stderr="",
            )
        result = super().run(command, cwd=cwd, env=env, timeout=timeout)
        if (
            command.startswith("git merge-base --is-ancestor")
            and self._after_reachability is not None
            and not self._reached
        ):
            self._reached = True
            self._after_reachability()
        return result


def _task(repos: list[RepoFixture], *, pr_urls: dict[str, str] | None = None) -> Task:
    return Task(
        slug="adopt",
        description="recover merged metadata",
        phase="dev",
        created_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
        affected_repos=[repo.name for repo in repos],
        worktrees={repo.name: repo.worktree for repo in repos},
        branch=_BRANCH,
        base_branch="main",
        blocked_reason="waiting for operator",
        active_repo=repos[0].name,
        spec_id="spec-adopt",
        work_item_id="wi-adopt",
        test_iteration=4,
        pr_urls=pr_urls or {},
    )


def _workspace(
    tmp_path: Path, names: tuple[str, ...] = ("api",)
) -> tuple[Path, Path, list[RepoFixture], StateManager]:
    repos = [_merged_repo(tmp_path, name) for name in names]
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: reconcile-adopt\nrepos:\n"
        + "".join(
            f"  {repo.name}:\n    path: ./{repo.name}\n    type: service\n    base_branch: main\n"
            for repo in repos
        )
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    manager = StateManager(state_dir)
    manager.save(
        WorkspaceState(
            tasks={
                "adopt": _task(repos),
                "untouched": Task(
                    slug="untouched",
                    description="unrelated",
                    phase="review",
                    created_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
                    affected_repos=[],
                    worktrees={},
                    branch="feat/untouched",
                    spec_id="other-spec",
                    work_item_id="wi-other",
                ),
            }
        )
    )
    return cfg, state_dir, repos, manager


def _configure(cfg: Path, state_dir: Path, shell: ShellRunner) -> None:
    container.config.reset()
    container.log_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    container.shell.override(shell)


def _reset_container() -> None:
    container.shell.reset_override()
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.log_manager.reset()
    container.state_manager.reset()


def _invoke_adoption() -> tuple[object, dict[str, object]]:
    result = CliRunner().invoke(app, ["reconcile", "--adopt-merged", "adopt", "--json"])
    return result, json.loads(result.output) if result.exit_code == 0 else {}


def test_adopt_merged_recovers_only_lifecycle_metadata_and_invalidates_cache(
    tmp_path: Path,
):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    cache = ReconcileCache(state_dir)
    cache.write(
        CachePayload(
            fetched_at=time.time(),
            ttl_seconds=300,
            results={"adopt": {"state": "missing"}},
        )
    )
    shell = _GitHubFixtureShell({repo.name: [_pr(repo)] for repo in repos})
    before = manager.load().tasks["adopt"].model_dump(mode="json")
    _configure(cfg, state_dir, shell)
    try:
        result, payload = _invoke_adoption()
        assert result.exit_code == 0, result.output
        assert payload["adopted"] is True
        assert payload["prs"] == [
            {
                "repo": repos[0].name,
                "url": _pr(repos[0])["url"],
                "merge_commit": repos[0].merge,
                "merged_at": "2026-09-08T12:34:56+00:00",
                "base": "main",
            }
        ]
        task = manager.load().tasks["adopt"]
        assert task.pr_urls == {repos[0].name: _pr(repos[0])["url"]}
        assert task.finished_at == datetime(2026, 9, 8, 12, 34, 56, tzinfo=timezone.utc)
        after = task.model_dump(mode="json")
        for key in (
            "branch",
            "worktrees",
            "phase",
            "blocked_reason",
            "active_repo",
            "spec_id",
            "work_item_id",
            "test_iteration",
        ):
            assert after[key] == before[key]
        assert _git(repos[0].worktree, "branch", "--show-current") == _BRANCH
        assert repos[0].worktree.is_dir()
        assert cache.read() is not None and cache.read().fetched_at == 0.0
        assert any(
            command.startswith("gh pr list --repo acme/api")
            for command in shell.commands
        )
        assert [entry.action for entry in container.log_manager().read("adopt")] == [
            "adopt_merged"
        ]
    finally:
        _reset_container()


def test_adopt_merged_is_idempotent_without_timestamp_churn(tmp_path: Path):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    shell = _GitHubFixtureShell({repo.name: [_pr(repo)] for repo in repos})
    _configure(cfg, state_dir, shell)
    try:
        first, first_payload = _invoke_adoption()
        first_state = manager.load().tasks["adopt"].model_dump(mode="json")
        first_journal_entries = container.log_manager().read("adopt")
        second, second_payload = _invoke_adoption()
        assert first.exit_code == second.exit_code == 0
        assert first_payload["adopted"] is True
        assert second_payload["adopted"] is False
        assert manager.load().tasks["adopt"].model_dump(mode="json") == first_state
        assert [entry.action for entry in first_journal_entries] == ["adopt_merged"]
        assert container.log_manager().read("adopt") == first_journal_entries
    finally:
        _reset_container()


def test_adopt_merged_succeeds_after_origin_deletes_head(tmp_path: Path):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    repo = repos[0]
    _git(repo.worktree, "push", "origin", "--delete", _BRANCH)
    shell = _GitHubFixtureShell({repo.name: [_pr(repo)]})
    _configure(cfg, state_dir, shell)
    try:
        result, payload = _invoke_adoption()
        assert result.exit_code == 0, result.output
        assert payload["adopted"] is True
        assert manager.load().tasks["adopt"].pr_urls == {
            "api": "https://github.com/acme/api/pull/7"
        }
    finally:
        _reset_container()


@pytest.mark.parametrize("tracking_ref", ["stale", "missing"])
def test_adopt_uses_actual_base_with_narrowed_fetch_mapping(tmp_path: Path, tracking_ref):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    repo = repos[0]
    _git(repo.worktree, "config", "remote.origin.fetch",
         "+refs/heads/feat/adopt:refs/remotes/origin/feat/adopt")
    if tracking_ref == "stale":
        # Change the bare remote directly: the local tracking ref must retain
        # the old merge while the actual remote base no longer contains it.
        bare = tmp_path / "origins" / "api.git"
        old_base = _git(repo.worktree, "rev-parse", f"{repo.merge}^1")
        _git(bare, "update-ref", "refs/heads/main", old_base)
        assert _git(repo.worktree, "rev-parse", "origin/main") == repo.merge
    else:
        _git(repo.worktree, "update-ref", "-d", "refs/remotes/origin/main")
    before = manager.load()
    shell = _GitHubFixtureShell({repo.name: [_pr(repo)]})
    _configure(cfg, state_dir, shell)
    try:
        result, payload = _invoke_adoption()
        if tracking_ref == "stale":
            assert result.exit_code != 0, result.output
            assert "not reachable" in result.output
            assert manager.load() == before
        else:
            assert result.exit_code == 0, result.output
            assert payload["adopted"] is True
            assert manager.load().tasks["adopt"].pr_urls == {
                "api": "https://github.com/acme/api/pull/7"
            }
        assert _git(repo.worktree, "for-each-ref", "refs/mship/adopt-merged") == ""
    finally:
        _reset_container()


@pytest.mark.parametrize("names", [("api",), ("api", "worker")])
@pytest.mark.parametrize("recorded_base", [None, ""])
def test_adopt_discovers_actual_default_base_and_persists_it(tmp_path: Path, names, recorded_base):
    cfg, state_dir, repos, manager = _workspace(tmp_path, names)
    cfg.write_text(cfg.read_text().replace("    base_branch: main\n", ""))
    manager.mutate(lambda state: setattr(state.tasks["adopt"], "base_branch", recorded_base))
    for repo in repos:
        _git(repo.worktree, "push", "origin", "main:refs/heads/release")
        _git(repo.worktree, "update-ref", "-d", "refs/remotes/origin/main")
    shell = _GitHubFixtureShell({repo.name: [_pr(repo, baseRefName="release")] for repo in repos})
    _configure(cfg, state_dir, shell)
    try:
        result, payload = _invoke_adoption()
        assert result.exit_code == 0, result.output
        assert manager.load().tasks["adopt"].base_branch == "release"
        assert all(pr["base"] == "release" for pr in payload["prs"])
        for repo in repos:
            assert f"git merge-base --is-ancestor {repo.merge} {repo.merge}" in shell.commands
    finally:
        _reset_container()


@pytest.mark.parametrize("bases", [("main", "release"), ("", ""), (None, None)])
def test_adopt_refuses_unrepresentable_or_missing_discovered_bases(tmp_path: Path, bases):
    cfg, state_dir, repos, manager = _workspace(tmp_path, ("api", "worker"))
    cfg.write_text(cfg.read_text().replace("    base_branch: main\n", ""))
    manager.mutate(lambda state: setattr(state.tasks["adopt"], "base_branch", None))
    _git(repos[1].worktree, "push", "origin", "main:refs/heads/release")
    before = manager.load()
    shell = _GitHubFixtureShell({repo.name: [_pr(repo, baseRefName=base)] for repo, base in zip(repos, bases)})
    _configure(cfg, state_dir, shell)
    try:
        result, _ = _invoke_adoption()
        assert result.exit_code != 0
        assert manager.load() == before
    finally:
        _reset_container()


def _owner(state_dir, manager):
    items = WorkItemStore(state_dir / "workitems", workspace_store=manager.workspace_store)
    item = items.create("Owner", "bug", "test", datetime(2026, 9, 8, tzinfo=timezone.utc))
    item.task_slugs = ["adopt"]
    item.affected_repos = ["historical"]
    item.pr_urls = ["https://github.com/acme/historical/pull/1"]
    items.save(item)
    manager.mutate(lambda state: setattr(state.tasks["adopt"], "work_item_id", item.id))
    return items, item


@pytest.mark.parametrize("task_complete", [False, True])
def test_adopt_mirrors_and_repairs_workitem_metadata(tmp_path: Path, task_complete):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    items, owner = _owner(state_dir, manager)
    if task_complete:
        manager.mutate(lambda state: (
            setattr(state.tasks["adopt"], "pr_urls", {"api": "https://github.com/acme/api/pull/7"}),
            setattr(state.tasks["adopt"], "finished_at", datetime(2026, 9, 8, tzinfo=timezone.utc)),
        ))
    shell = _GitHubFixtureShell({repo.name: [_pr(repo)] for repo in repos})
    _configure(cfg, state_dir, shell)
    try:
        result, payload = _invoke_adoption()
        assert result.exit_code == 0, result.output
        assert payload["adopted"] is True
        saved = items.get(owner.id)
        assert saved.affected_repos == ["historical", "api"]
        assert saved.pr_urls == ["https://github.com/acme/historical/pull/1", "https://github.com/acme/api/pull/7"]
        assert saved.task_slugs == ["adopt"]
        before_task = manager.load().tasks["adopt"]
        cache = ReconcileCache(state_dir)
        cache.write(CachePayload(fetched_at=12345, ttl_seconds=300, results={"sentinel": {}}))
        second, second_payload = _invoke_adoption()
        assert second.exit_code == 0, second.output
        assert second_payload["adopted"] is False
        assert items.get(owner.id) == saved
        assert manager.load().tasks["adopt"] == before_task
        assert cache.read().fetched_at == 12345
    finally:
        _reset_container()


@pytest.mark.parametrize("stage", ["after_task_replace", "after_adoption_retention"])
def test_adopt_rolls_back_task_and_workitem_on_write_failure(tmp_path: Path, monkeypatch, stage):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    items, owner = _owner(state_dir, manager)
    before = manager.load()
    def fail(self, checkpoint):
        if checkpoint == stage:
            raise RuntimeError("injected adoption failure")
    monkeypatch.setattr(LifecycleRepository, "_checkpoint", fail)
    shell = _GitHubFixtureShell({repo.name: [_pr(repo)] for repo in repos})
    _configure(cfg, state_dir, shell)
    try:
        result, _ = _invoke_adoption()
        assert result.exit_code != 0
        assert "injected adoption failure" in result.output
        assert manager.load() == before
        assert items.get(owner.id) == owner
    finally:
        _reset_container()


@pytest.mark.parametrize("case", ["corrupt", "ambiguous"])
def test_adopt_refuses_corrupt_or_ambiguous_owner_atomically(tmp_path: Path, monkeypatch, case):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    items, owner = _owner(state_dir, manager)
    if case == "corrupt":
        corrupt_workitem(state_dir, owner.id)
    else:
        # SQLite forbids duplicate forward links; inject the malformed lookup
        # at the ownership boundary to exercise adoption's fail-closed path.
        monkeypatch.setattr(LifecycleRepository, "_owner_ids", lambda *args: [owner.id, "wi-duplicate"])
    before = manager.load()
    shell = _GitHubFixtureShell({repo.name: [_pr(repo)] for repo in repos})
    _configure(cfg, state_dir, shell)
    try:
        result, _ = _invoke_adoption()
        assert result.exit_code != 0
        assert manager.load() == before
        if case == "ambiguous":
            assert items.get(owner.id) == owner
    finally:
        _reset_container()


@pytest.mark.parametrize(
    "change",
    [
        {"state": "OPEN", "mergeCommit": None},
        {"baseRefName": "release"},
        {"headRefName": "feat/someone-else"},
        {"url": "https://github.com/acme/other/pull/7"},
        {"mergeCommit": {"oid": "a" * 40}},
    ],
    ids=["open", "wrong-base", "wrong-head", "wrong-repository", "unreachable-merge"],
)
def test_adopt_merged_refuses_unverified_pr_proofs_without_mutation(
    tmp_path: Path, change: dict[str, object]
):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    shell = _GitHubFixtureShell({repos[0].name: [_pr(repos[0], **change)]})
    before = manager.load().model_dump(mode="json")
    _configure(cfg, state_dir, shell)
    try:
        result, _ = _invoke_adoption()
        assert result.exit_code != 0
        assert manager.load().model_dump(mode="json") == before
    finally:
        _reset_container()


def test_adopt_merged_rejects_mismatched_recorded_url_without_mutation(tmp_path: Path):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    manager.mutate(
        lambda state: setattr(
            state.tasks["adopt"],
            "pr_urls",
            {repos[0].name: "https://github.com/acme/api/pull/99"},
        )
    )
    shell = _GitHubFixtureShell({repos[0].name: [_pr(repos[0])]})
    before = manager.load().model_dump(mode="json")
    _configure(cfg, state_dir, shell)
    try:
        result, _ = _invoke_adoption()
        assert result.exit_code != 0
        assert manager.load().model_dump(mode="json") == before
    finally:
        _reset_container()


def test_adopt_merged_verifies_every_repo_before_multi_repo_mutation(tmp_path: Path):
    cfg, state_dir, repos, manager = _workspace(tmp_path, ("api", "worker"))
    shell = _GitHubFixtureShell(
        {
            "api": [_pr(repos[0])],
            "worker": [_pr(repos[1], state="CLOSED", mergeCommit=None)],
        }
    )
    before = manager.load().model_dump(mode="json")
    _configure(cfg, state_dir, shell)
    try:
        result, _ = _invoke_adoption()
        assert result.exit_code != 0
        assert manager.load().model_dump(mode="json") == before
    finally:
        _reset_container()


def test_adopt_merged_aborts_if_task_changes_during_verification(tmp_path: Path):
    cfg, state_dir, repos, manager = _workspace(tmp_path)

    def concurrent_change() -> None:
        manager.mutate(
            lambda state: setattr(
                state.tasks["adopt"], "blocked_reason", "concurrent update"
            )
        )

    shell = _GitHubFixtureShell(
        {repos[0].name: [_pr(repos[0])]},
        after_reachability=concurrent_change,
    )
    _configure(cfg, state_dir, shell)
    try:
        result, _ = _invoke_adoption()
        assert result.exit_code != 0
        task = manager.load().tasks["adopt"]
        assert task.blocked_reason == "concurrent update"
        assert task.pr_urls == {}
        assert task.finished_at is None
    finally:
        _reset_container()


@pytest.mark.parametrize("case", ["dirty", "unpushed", "head-mismatch"])
def test_adopt_merged_refuses_worktree_or_head_proof_failures(
    tmp_path: Path,
    case: str,
):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    repo = repos[0]
    pr = _pr(repo)
    if case == "dirty":
        (repo.worktree / "pending.txt").write_text("uncommitted\n")
    elif case == "unpushed":
        (repo.worktree / "README.md").write_text("local-only\n")
        _git(repo.worktree, "commit", "-qam", "local-only")
    else:
        pr["headRefOid"] = "b" * 40

    shell = _GitHubFixtureShell({repo.name: [pr]})
    before = manager.load().model_dump(mode="json")
    _configure(cfg, state_dir, shell)
    try:
        result, _ = _invoke_adoption()
        assert result.exit_code != 0
        assert manager.load().model_dump(mode="json") == before
    finally:
        _reset_container()


def test_adopt_merged_rejects_unknown_tasks_and_conflicting_cache_actions(
    tmp_path: Path,
):
    cfg, state_dir, repos, manager = _workspace(tmp_path)
    shell = _GitHubFixtureShell({repos[0].name: [_pr(repos[0])]})
    before = manager.load().model_dump(mode="json")
    _configure(cfg, state_dir, shell)
    try:
        unknown = CliRunner().invoke(
            app,
            ["reconcile", "--adopt-merged", "missing", "--json"],
        )
        conflicting = CliRunner().invoke(
            app,
            ["reconcile", "--adopt-merged", "adopt", "--ignore", "adopt", "--json"],
        )
        assert unknown.exit_code != 0
        assert conflicting.exit_code != 0
        assert manager.load().model_dump(mode="json") == before
    finally:
        _reset_container()
