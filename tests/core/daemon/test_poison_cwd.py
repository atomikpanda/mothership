"""Runtime poison tests for explicit daemon workspace selection.

The serve and host-app paths must use the workspace carried by their explicit
parameters/registry entry even when the process cwd, workspace environment, and
workspace marker all resolve to a valid decoy workspace. Profile discovery also
proves that host-local XDG bindings remain available without becoming workspace
selection input.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mship.core import remote_exec
from mship.core.config import ConfigLoader
from mship.core.daemon.host_app import create_host_app
from mship.core.daemon.paths import registry_path
from mship.core.daemon.registry import RegistryStore, WorkspaceEntry
from mship.core.pr import PRManager
from mship.core.remote_tool import ToolContext, ToolRequest, iter_tool_events
from mship.core.run_target.host import TARGET_REQUEST_FILE
from mship.core.run_target.models import profile_revision
from mship.core.serve import create_app
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult, ShellRunner


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
        Path,
        "cwd",
        classmethod(
            lambda cls: (_ for _ in ()).throw(
                AssertionError("Path.cwd read on serve path")
            )
        ),
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
    sm.save(
        WorkspaceState(
            tasks={
                "t1": Task(
                    slug="t1",
                    description="d",
                    phase="review",
                    created_at=now,
                    affected_repos=["app"],
                    worktrees={},
                    branch="feat/t1",
                    pr_urls={"app": "https://github.com/x/y/pull/1"},
                ),
            }
        )
    )
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
        assert r.status_code in (
            200,
            201,
            404,
            405,
        )  # route shape may vary; must not 500

        # give the watcher loop a couple of ticks so check_pr_state runs
        deadline = time.time() + 5
        while time.time() < deadline and not rec.cwds:
            time.sleep(0.05)

    assert rec.cwds, (
        "watcher sweep never invoked the shell — interval param not in effect?"
    )
    for cwd in rec.cwds:
        assert str(cwd.resolve()).startswith(str(real.resolve())), (
            f"shell ran outside real workspace: {cwd}"
        )
    assert not any(str(decoy) in str(c) for c in rec.cwds)


def test_pr_manager_merge_commit_uses_explicit_cwd(poisoned):
    real, decoy = poisoned
    rec = RecordingShell()
    pm = PRManager(rec, cwd=real)
    pm.check_pr_state("https://github.com/x/y/pull/1")
    pm.get_merge_commit("https://github.com/x/y/pull/1")
    assert rec.cwds and all(c == real for c in rec.cwds)


def test_profile_discovery_uses_registered_workspace_not_ambient_one(
    tmp_path, monkeypatch
):
    target = _mk_ws(tmp_path, "target")
    decoy = _mk_ws(tmp_path, "decoy")
    target_config = target / "mothership.yaml"
    target_config.write_text(
        "workspace: target\n"
        "run_hosts: [mobile]\n"
        "repos:\n"
        "  app:\n"
        "    path: app\n"
        "    type: service\n"
        "    tasks:\n"
        "      discover: target-discover\n"
        "      launch: target-launch\n"
        "      setup: target-setup\n"
        "    run_backends:\n"
        "      native:\n"
        "        discover_task: discover\n"
        "        operations: {run: launch}\n"
        "    run_profiles:\n"
        "      target-profile:\n"
        "        backend: native\n"
        "        hosts: {roles: [mobile]}\n"
        "        options: {source: target}\n"
    )
    (decoy / "mothership.yaml").write_text(
        "workspace: decoy\n"
        "run_hosts: [mobile]\n"
        "repos:\n"
        "  app:\n"
        "    path: app\n"
        "    type: service\n"
        "    tasks: {discover: decoy-discover}\n"
        "    run_backends:\n"
        "      poison: {discover_task: discover, operations: {run: discover}}\n"
        "    run_profiles:\n"
        "      poison-profile:\n"
        "        backend: poison\n"
        "        hosts: {roles: [mobile]}\n"
        "        options: {source: decoy}\n"
    )
    (decoy / "app" / ".mship-workspace").write_text(f"{decoy}\n")

    home = tmp_path / "host-home"
    xdg_config = home / "isolated-xdg"
    bindings = xdg_config / "mothership" / "run-target-bindings.yaml"
    bindings.parent.mkdir(parents=True)
    bindings.write_text(
        "version: 1\n"
        "backends:\n"
        "  native:\n"
        "    paths: {target-device: /private/target-device}\n"
        "    aliases: {target: target-alias}\n"
        "  poison:\n"
        "    paths: {poison-device: /private/poison-device}\n"
        "    aliases: {poison: poison-alias}\n"
    )
    bindings.chmod(0o600)

    config = ConfigLoader.load(target_config)
    repo = config.repos["app"]
    profile = repo.run_profiles["target-profile"]
    backend = repo.run_backends["native"]
    source_revision = "a" * 40
    request = ToolRequest(
        task="target-task",
        repo="app",
        argv=(),
        task_key="discover",
        input_files={
            TARGET_REQUEST_FILE: json.dumps(
                {
                    "protocol_version": 1,
                    "backend": "native",
                    "backend_revision": source_revision,
                    "profile": "target-profile",
                    "profile_revision": profile_revision(
                        profile, backend, prepared_source_revision=source_revision
                    ),
                    "task": "target-task",
                    "repo": "app",
                    "operation": "run",
                    "options": {"source": "target"},
                    "target_alias": None,
                }
            )
        },
        preparation="discover",
        source_revision=source_revision,
        max_stdout_bytes=4096,
        max_stderr_bytes=1024,
        timeout_seconds=2,
    )
    worktree = target / ".worktrees" / "target-task" / "app"
    worktree.mkdir(parents=True)

    class PreparedTarget:
        def __init__(self, deps, task, repos, run_ref_repos):
            self.task = task
            self.repos = list(repos)

        def prepare(self, name):
            return ()

        def context(self, name):
            return ToolContext(
                task=self.task,
                repo=name,
                worktree=worktree,
                source_revision=source_revision,
            )

    class ConfiguredBackendShell:
        def __init__(self):
            self.commands = []

        def spawn_argv(self, args, cwd, env):
            self.commands.append(tuple(args))
            program = (
                "import json, os\n"
                "from pathlib import Path\n"
                "request = json.loads(Path(os.environ['MSHIP_TARGET_REQUEST_FILE']).read_text())\n"
                "bindings = json.loads(Path(os.environ['MSHIP_TARGET_BINDINGS_FILE']).read_text())\n"
                "print(json.dumps({'task': os.environ['MSHIP_TASK'], "
                "'repo': os.environ['MSHIP_REPO'], "
                "'source_revision': os.environ['MSHIP_SOURCE_REVISION'], "
                "'profile': request['profile'], 'backend': request['backend'], "
                "'options': request['options'], 'bindings': bindings, "
                "'ambient_workspace': os.environ.get('MSHIP_WORKSPACE'), "
                "'cwd': os.getcwd()}))\n"
            )
            return ShellRunner().spawn_argv((sys.executable, "-c", program), cwd, env)

    shell = ConfiguredBackendShell()
    monkeypatch.setattr(remote_exec, "_PreparedTask", PreparedTarget)
    import mship.core.serve as serve_mod

    monkeypatch.setattr(serve_mod, "ShellRunner", lambda: shell)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("MSHIP_WORKSPACE", str(decoy))
    monkeypatch.chdir(decoy / "app")
    monkeypatch.setattr(
        Path,
        "cwd",
        classmethod(
            lambda cls: (_ for _ in ()).throw(
                AssertionError("Path.cwd read on daemon profile path")
            )
        ),
    )

    registry_home = tmp_path / "daemon-home"
    store = RegistryStore(registry_path(registry_home))
    now = datetime.now(timezone.utc)
    store.mutate(
        lambda state: state.entries.append(
            WorkspaceEntry(
                id="target-id",
                name="target",
                path=str(target),
                config_path=str(target_config),
                state="healthy",
                detail="",
                first_seen=now,
                last_seen=now,
            )
        )
    )
    app = create_host_app(store, auth_token=None, pr_watch_interval=0)

    with TestClient(app) as client:
        response = client.post(
            "/workspaces/target-id/exec/tool", json=request.to_dict()
        )

    assert response.status_code == 200
    events = list(
        iter_tool_events([response.content], response.headers["X-Mship-Exec-Nonce"])
    )
    result = events[-1].result
    assert result.status == "completed" and result.exit_code == 0
    assert json.loads(result.stdout) == {
        "task": "target-task",
        "repo": "app",
        "source_revision": source_revision,
        "profile": "target-profile",
        "backend": "native",
        "options": {"source": "target"},
        "bindings": {
            "paths": {"target-device": "/private/target-device"},
            "aliases": {"target": "target-alias"},
        },
        "ambient_workspace": None,
        "cwd": str(worktree),
    }
    assert shell.commands == [("task", "target-discover")]
    assert str(decoy) not in response.text
