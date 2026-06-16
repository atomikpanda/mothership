from __future__ import annotations
from pathlib import Path
from mship.core.relay.config import RelayConfig
from mship.util.slug import slugify

def subdomain_for(workspace: str) -> str:
    return slugify(workspace)

def build_tunnel_argv(rc: RelayConfig, *, subdomain: str, local_port: int, key_path: Path) -> list[str]:
    target = f"{rc.user}@{rc.host}" if rc.user else rc.host
    return [
        "ssh",
        "-p", str(rc.ssh_port),
        "-i", str(key_path),
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-N",
        "-R", f"{subdomain}:80:localhost:{local_port}",
        target,
    ]
