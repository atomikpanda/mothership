"""Private run-host registry locations."""
from pathlib import Path
from typing import Mapping


def run_host_config_dir(home: Path, environ: Mapping[str, str]) -> Path:
    """Return XDG user configuration directory without relocating workspace state."""
    value = environ.get("XDG_CONFIG_HOME", "")
    base = Path(value) if value and Path(value).is_absolute() else home / ".config"
    return base / "mothership"
