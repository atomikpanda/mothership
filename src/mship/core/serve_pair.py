from __future__ import annotations

import socket

from mship.core.relay.pairing import build_pair_link

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_UNSPECIFIED = {"0.0.0.0", "::"}


def _primary_ipv4() -> str | None:
    """Best-effort primary outbound IPv4. A UDP socket's getsockname yields the
    route's source address WITHOUT sending any packets. None on failure."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def resolve_advertised_host(host: str, primary_ip=None) -> str | None:
    """Address to advertise in a pairing QR, or None when not reachable from a phone.

    concrete non-loopback host -> itself; 0.0.0.0/:: -> best-effort primary IPv4
    (or None); loopback -> None. `primary_ip` (a no-arg callable) is resolved at
    call time so it can be overridden in tests / monkeypatched."""
    if host in _UNSPECIFIED:
        return (primary_ip or _primary_ipv4)()
    if host in _LOOPBACK:
        return None
    return host


def serve_pair_link(
    host: str, port: int, token: str | None, workspace: str, primary_ip=None
) -> str | None:
    """The groundcontrol://add pairing link to print for a non-relay serve, or None
    when not pairable (no token, or no reachable advertised host)."""
    if not token:
        return None
    adv = resolve_advertised_host(host, primary_ip=primary_ip)
    if adv is None:
        return None
    return build_pair_link(url=f"http://{adv}:{port}", token=token, workspace=workspace)
