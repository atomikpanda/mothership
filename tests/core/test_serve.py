from pathlib import Path
from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.state import StateManager


def _app(tmp_path: Path):
    state = StateManager(tmp_path / ".mothership")
    return create_app(
        specs_dir=tmp_path / "specs",
        state_manager=state,
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
    )


def test_health(tmp_path):
    client = TestClient(_app(tmp_path))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "workspace": "test-ws"}
