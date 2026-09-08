from __future__ import annotations

from pathlib import Path


class LegacyMigrationRequired(RuntimeError):
    """A legacy Task/WorkItem store must be migrated before it can be written."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        super().__init__(
            f"legacy Task/WorkItem storage detected at {state_dir}; "
            "stop active writers and run mship state migrate before writing"
        )
