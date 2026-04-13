import socket

DEFAULT_START_PORT = 47213
BLOCKED_DEV_PORTS = frozenset(
    {3000, 3001, 4200, 5000, 5173, 8000, 8080, 8443, 8888, 9000}
)


class NoFreePortError(Exception):
    pass


def _is_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(
    start: int = DEFAULT_START_PORT,
    max_tries: int = 10,
    explicit: int | None = None,
) -> int:
    if explicit is not None:
        if _is_free(explicit):
            return explicit
        raise NoFreePortError(f"Port {explicit} is in use")

    port = start
    tried = 0
    while tried < max_tries:
        if port not in BLOCKED_DEV_PORTS and _is_free(port):
            return port
        port += 1
        tried += 1
    raise NoFreePortError(
        f"No free port found in {max_tries} tries starting at {start}"
    )
