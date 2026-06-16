"""The shipped example workspace config (examples/mothership.yaml) must stay valid."""
from pathlib import Path

import yaml

from mship.core.config import WorkspaceConfig

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "mothership.yaml"


def test_example_two_repo_config_parses():
    raw = yaml.safe_load(EXAMPLE.read_text())
    config = WorkspaceConfig(**raw)          # schema validation (no filesystem checks)
    assert config.workspace
    assert set(config.repos) == {"api", "shared"}
    assert config.repos["api"].type == "service"
    assert config.repos["shared"].type == "library"


def test_example_declares_a_compile_dependency():
    raw = yaml.safe_load(EXAMPLE.read_text())
    config = WorkspaceConfig(**raw)
    deps = config.repos["api"].depends_on
    names = [(d.repo if hasattr(d, "repo") else d) for d in deps]
    assert "shared" in names
