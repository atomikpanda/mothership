from __future__ import annotations
from dataclasses import dataclass

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
            raise ValueError("relay.host is required when a `relay:` block is present")
        return RelayConfig(
            host=host,
            ssh_port=int(data.get("ssh_port", 2222)),
            user=data.get("user"),
        )
