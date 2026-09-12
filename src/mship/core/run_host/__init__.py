"""Public run-host models and lazy registry exports."""
from __future__ import annotations

from typing import TYPE_CHECKING

from mship.core.run_host.config import HostRegistration, MigrationReport, RunHostConnection

if TYPE_CHECKING:
    from mship.core.run_host.store import RunHostError, RunHostStore, resolve_run_host

__all__ = [
    "HostRegistration",
    "MigrationReport",
    "RunHostConnection",
    "RunHostError",
    "RunHostStore",
    "resolve_run_host",
]


def __getattr__(name: str):
    if name in {"RunHostError", "RunHostStore", "resolve_run_host"}:
        from mship.core.run_host import store

        return getattr(store, name)
    raise AttributeError(name)
