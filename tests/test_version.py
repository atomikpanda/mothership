"""Guard: the package __version__ must match pyproject's version so they can't
drift (they were out of sync — 0.1.0 vs 0.2.0 — before the 0.3.0 bump)."""
import tomllib
from pathlib import Path

import mship


def test_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    assert mship.__version__ == data["project"]["version"]
