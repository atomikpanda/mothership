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


def test_capture_posts_one_agent_event_brainstorm_handoff(tmp_path):
    from mship.core.message_store import MessageStore
    client = TestClient(_app(tmp_path))
    tid = client.post("/capture", json={"idea": "a queue tab"}).json()["id"]

    store = MessageStore(tmp_path / ".mothership" / "messages")
    thread = store.get(tid)
    # seed human message + exactly one trailing agent event
    assert [m.role for m in thread.messages] == ["human", "agent"]
    event = thread.messages[-1]
    assert event.kind == "event"
    assert "capture-brainstorm" in event.text          # stable marker
    assert tid in event.text                            # names the thread to brainstorm
    assert "a queue tab" in event.text                  # carries the idea
    assert "mship spec from-thread" in event.text       # tells the driver how to finish
    # this is what makes _drain / inbox wait surface it to a host agent
    assert thread.awaiting_agent_event is True
    assert thread.needs_you is False                    # an event must NOT nag the phone


def test_capture_is_idempotent_on_key(tmp_path):
    client = TestClient(_app(tmp_path))
    r1 = client.post("/capture", json={"idea": "x", "idempotency_key": "k1"})
    r2 = client.post("/capture", json={"idea": "x", "idempotency_key": "k1"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]          # same capture, no duplicate thread
    r3 = client.post("/capture", json={"idea": "x", "idempotency_key": "k2"})
    assert r3.json()["id"] != r1.json()["id"]          # a different key is a different capture


def test_capture_key_matches_by_line_not_prefix(tmp_path):
    """A key must not collide with a longer key that has it as a prefix
    (the marker is compared as a whole line, not a substring)."""
    client = TestClient(_app(tmp_path))
    r1 = client.post("/capture", json={"idea": "x", "idempotency_key": "k1"})
    r2 = client.post("/capture", json={"idea": "y", "idempotency_key": "k1x"})
    assert r1.json()["id"] != r2.json()["id"]          # "k1" must not match "k1x"


def test_keyed_capture_event_still_starts_with_brainstorm_marker(tmp_path):
    """The driver's first-line contract (event body STARTS WITH
    `capture-brainstorm <tid>`) must survive a keyed capture — the key marker
    is appended, not prepended."""
    client = TestClient(_app(tmp_path))
    tid = client.post("/capture", json={"idea": "z", "idempotency_key": "k9"}).json()["id"]
    store = MessageStore(tmp_path / ".mothership" / "messages")
    event = store.get(tid).messages[-1]
    assert event.kind == "event"
    assert event.text.startswith(f"capture-brainstorm {tid}")
    assert "capture-key k9" in event.text


def test_capture_and_draft_path_import_no_llm_sdk():
    """AC5: serve stays LLM-free. The modules the capture→draft path touches must
    not import an LLM SDK — the drafting intelligence runs in the agent, not serve."""
    import inspect
    import mship.core.serve as serve_mod
    import mship.core.spec_draft as draft_mod

    banned = ("import anthropic", "from anthropic", "import openai", "from openai")
    for mod in (serve_mod, draft_mod):
        src = inspect.getsource(mod)
        for token in banned:
            assert token not in src, f"{mod.__name__} imports an LLM SDK ({token!r})"
