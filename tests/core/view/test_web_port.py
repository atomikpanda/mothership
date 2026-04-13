import socket
import pytest

from mship.core.view.web_port import (
    pick_port,
    NoFreePortError,
    DEFAULT_START_PORT,
    BLOCKED_DEV_PORTS,
)


def _occupy(port: int) -> socket.socket:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def test_default_start_is_uncommon():
    assert DEFAULT_START_PORT >= 40000
    assert DEFAULT_START_PORT not in BLOCKED_DEV_PORTS


def test_picks_default_when_free():
    assert pick_port() == DEFAULT_START_PORT


def test_skips_occupied_port():
    s = _occupy(DEFAULT_START_PORT)
    try:
        assert pick_port() == DEFAULT_START_PORT + 1
    finally:
        s.close()


def test_skips_blocked_dev_ports():
    assert pick_port(start=3000) != 3000
    assert pick_port(start=3000) not in BLOCKED_DEV_PORTS


def test_honors_explicit_port():
    assert pick_port(explicit=DEFAULT_START_PORT) == DEFAULT_START_PORT


def test_explicit_port_in_use_raises():
    s = _occupy(DEFAULT_START_PORT)
    try:
        with pytest.raises(NoFreePortError):
            pick_port(explicit=DEFAULT_START_PORT)
    finally:
        s.close()


def test_exhausted_scan_raises():
    # Scan a tiny range that's fully blocked: all ports in blocklist
    with pytest.raises(NoFreePortError):
        pick_port(start=3000, max_tries=1)
