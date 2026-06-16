from __future__ import annotations
import os, secrets
from pathlib import Path

def ensure_serve_token(workspace_root: Path) -> str:
    """Return the serve bearer token: env override > persisted file > freshly generated+persisted."""
    env = os.environ.get("MSHIP_SERVE_TOKEN")
    if env:
        return env
    path = workspace_root / ".mothership" / "serve-token"
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token
