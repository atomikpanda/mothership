from __future__ import annotations
from dataclasses import dataclass

# Spelled once: `from_mapping` raises it, and the daemon config's `relay:`
# validator (`core/daemon/registry.py`) rejects an empty block with the same
# sentence, so an operator sees one message whichever end catches the typo.
RELAY_HOST_REQUIRED = "relay.host is required when a `relay:` block is present"


@dataclass(frozen=True)
class RelayConfig:
    host: str
    ssh_port: int = 2222
    user: str | None = None       # ssh user; None → ssh default

    @staticmethod
    def from_mapping(data: dict | None) -> "RelayConfig | None":
        if not data:
            return None
        host = data.get("host")
        if not host:
            raise ValueError(RELAY_HOST_REQUIRED)
        return RelayConfig(
            host=host,
            ssh_port=int(data.get("ssh_port", 2222)),
            user=data.get("user"),
        )
