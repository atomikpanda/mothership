from pathlib import Path

from fastapi.testclient import TestClient

from mship.core.serve import create_app
from mship.core.state import StateManager
from mship.core.message_store import MessageStore


def _app(tmp_path: Path):
    state = StateManager(tmp_path / ".mothership")
    return create_app(
        specs_dir=tmp_path / "specs",
        state_manager=state,
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
    )


def test_capture_seeds_thread_with_the_idea(tmp_path):
    client = TestClient(_app(tmp_path))
    r = client.post("/capture", json={"idea": "a queue tab for approvals"})
    assert r.status_code == 200, r.text
    thread = r.json()
    tid = thread["id"]
    assert thread["subject"].startswith("a queue tab")
    # first message is the human idea
    assert thread["messages"][0]["role"] == "human"
    assert thread["messages"][0]["text"] == "a queue tab for approvals"

    store = MessageStore(tmp_path / ".mothership" / "messages")
    assert store.get(tid) is not None


def test_capture_rejects_empty_idea(tmp_path):
    client = TestClient(_app(tmp_path))
    r = client.post("/capture", json={"idea": "   "})
    assert r.status_code == 400
