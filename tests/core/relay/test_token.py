from pathlib import Path
from mship.core.relay.token import ensure_serve_token

def test_generates_and_persists(tmp_path: Path):
    t1 = ensure_serve_token(tmp_path)            # tmp_path = workspace root
    assert isinstance(t1, str) and len(t1) >= 32
    assert (tmp_path / ".mothership" / "serve-token").read_text().strip() == t1
    t2 = ensure_serve_token(tmp_path)            # stable across calls
    assert t2 == t1

def test_respects_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MSHIP_SERVE_TOKEN", "explicit")
    assert ensure_serve_token(tmp_path) == "explicit"   # env wins, no file write
    assert not (tmp_path / ".mothership" / "serve-token").exists()
