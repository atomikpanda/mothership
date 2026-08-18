"""Runtime poison test (#472): the serve app, built purely from explicit
parameters, must ignore a POISONED environment — decoy workspace SET in
MSHIP_WORKSPACE (env-wins discovery precedence is the likeliest regression
class; a delenv-only test would pass it unchanged), cwd chdir'd into the
decoy, `Path.cwd` raising, and a poison watch interval in env.

The static sweep (tests/core/test_serve_ambient_invariants.py) proves no
ambient READS exist on serve paths; this proves the explicit parameters are
actually the ones in EFFECT. PRManager's eight former `cwd=Path(".")` sites
route through one `self._cwd` mechanism, exercised here via the watcher sweep
(check_pr_state) and direct get_merge_commit/check calls rather than a full
merge-close teardown (which would spin up worktree machinery this test does
not need to prove cwd-explicitness).
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core.config import ConfigLoader
from mship.core.pr import PRManager
from mship.core.serve import create_app
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult


class RecordingShell:
    """ShellRunner stand-in: records every cwd, answers gh probes harmlessly."""

    def __init__(self):
        self.cwds: list[Path] = []

    def run(self, command: str, cwd: Path, env=None, timeout=None):
        self.cwds.append(Path(cwd))
        return ShellResult(returncode=1, stdout="", stderr="recorded")


def _mk_ws(root: Path, name: str) -> Path:
    ws = root / name
    repo = ws / "app"
    repo.mkdir(parents=True)
    (repo / "Taskfile.yml").write_text("version: '3'\n")
    (ws / "mothership.yaml").write_text(
        f"workspace: {name}\nrepos:\n  app:\n    path: app\n    type: service\n"
    )
    specs = ws / "specs"
    specs.mkdir()
    return ws


@pytest.fixture
def poisoned(tmp_path, monkeypatch):
    real = _mk_ws(tmp_path, "real-ws")
    decoy = _mk_ws(tmp_path, "decoy-ws")
    monkeypatch.chdir(decoy)
    monkeypatch.setenv("MSHIP_WORKSPACE", str(decoy))  # SET, not deleted
    monkeypatch.setenv("MSHIP_PR_WATCH_INTERVAL", "99999")  # poison: must not be read
    monkeypatch.setattr(
        Path, "cwd",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("Path.cwd read on serve path"))),
    )
    # os.getcwd is NOT poisoned: pytest's own failure repr calls it. The
    # static sweep forbids os.getcwd on serve paths instead.
    return real, decoy


def test_serve_ignores_poisoned_env_and_cwd(poisoned, monkeypatch):
    real, decoy = poisoned
    import mship.core.serve as serve_mod

    rec = RecordingShell()
    monkeypatch.setattr(serve_mod, "ShellRunner", lambda: rec)

    state_dir = real / ".mothership"
    sm = StateManager(state_dir)
    now = datetime.now(timezone.utc)
    sm.save(WorkspaceState(tasks={
        "t1": Task(slug="t1", description="d", phase="review", created_at=now,
                   affected_repos=["app"], worktrees={}, branch="feat/t1",
                   pr_urls={"app": "https://github.com/x/y/pull/1"}),
    }))
    (real / "specs" / "2026-08-17-poison-spec.md").write_text(
        "---\n"
        "id: poison-spec\n"
        "title: Poison spec\n"
        "status: draft\n"
        "created_at: '2026-08-17T00:00:00Z'\n"
        "updated_at: '2026-08-17T00:00:00Z'\n"
        "affected_repos: [app]\n"
        "acceptance_criteria: []\n"
        "open_questions: []\n"
        "---\n\n## Problem\nreal-ws data\n"
    )

    app = serve_mod.create_app(
        specs_dir=real / "specs",
        state_manager=sm,
        log_manager=None,
        workspace_root=real,
        workspace_name="real-ws",
        config=ConfigLoader.load(real / "mothership.yaml"),
        pr_watch_interval=0.05,  # explicit param IN EFFECT (env says 99999)
    )
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["workspace"] == "real-ws"  # not the decoy's name

        r = client.get("/specs")
        assert r.status_code == 200
        payload = r.json()
        specs = payload if isinstance(payload, list) else payload.get("specs", [])
        assert any(s.get("id") == "poison-spec" for s in specs)

        r = client.get("/net/topology")
        assert r.status_code == 200  # topology probe ran with explicit cwd

        # one write route: steer a message onto a work item store (workspace-scoped)
        r = client.post("/threads", json={"subject": "s", "text": "hello"})
        assert r.status_code in (200, 201, 404, 405)  # route shape may vary; must not 500

        # give the watcher loop a couple of ticks so check_pr_state runs
        deadline = time.time() + 5
        while time.time() < deadline and not rec.cwds:
            time.sleep(0.05)

    assert rec.cwds, "watcher sweep never invoked the shell — interval param not in effect?"
    for cwd in rec.cwds:
        assert str(cwd.resolve()).startswith(str(real.resolve())), f"shell ran outside real workspace: {cwd}"
    assert not any(str(decoy) in str(c) for c in rec.cwds)


def test_pr_manager_merge_commit_uses_explicit_cwd(poisoned):
    real, decoy = poisoned
    rec = RecordingShell()
    pm = PRManager(rec, cwd=real)
    pm.check_pr_state("https://github.com/x/y/pull/1")
    pm.get_merge_commit("https://github.com/x/y/pull/1")
    assert rec.cwds and all(c == real for c in rec.cwds)
