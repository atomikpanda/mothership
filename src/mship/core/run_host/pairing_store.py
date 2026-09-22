"""Owner-private relay fleet credentials for run-host resolution."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None

from mship.core.relay.config import canonical_relay_host
from mship.core.run_host.paths import run_host_config_dir
from mship.core.run_host.store import RunHostError


class RelayPairingStore:
    """Flock'd private mapping from relay domain to its paired fleet credential."""

    def __init__(self, config_dir: Path | None = None) -> None:
        self._dir = Path(config_dir) if config_dir is not None else run_host_config_dir(Path.home(), os.environ)
        self.path = self._dir / "relay-pairings.json"
        self._lock = self.path.with_name(self.path.name + ".lock")

    def _lock_file(self) -> int:
        if fcntl is None:
            raise RunHostError("relay pairing requires POSIX file locking; this platform cannot safely store it")
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._dir.chmod(0o700)
        fd = os.open(self._lock, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(self._lock, 0o600)
        return fd

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError, UnicodeError) as exc:
            raise RunHostError("could not read private relay pairing store; re-pair the coordinator") from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {"version", "pairings"}
            or raw.get("version") != 1
            or not isinstance(raw["pairings"], dict)
        ):
            raise RunHostError("private relay pairing store is invalid; re-pair the coordinator")
        pairings: dict[str, str] = {}
        for relay, token in raw["pairings"].items():
            if (
                not isinstance(relay, str)
                or canonical_relay_host(relay) != relay
                or not relay
                or not isinstance(token, str)
                or not token
            ):
                raise RunHostError("private relay pairing store is invalid; re-pair the coordinator")
            pairings[relay] = token
        return pairings

    def _write(self, pairings: dict[str, str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._dir.chmod(0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self._dir)
        temp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "pairings": pairings}, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def put(self, relay: str, fleet_token: str) -> None:
        relay = canonical_relay_host(relay)
        if not relay or not isinstance(fleet_token, str) or not fleet_token:
            raise RunHostError("relay pairing is invalid")
        fd = self._lock_file()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            pairings = self._load()
            pairings[relay] = fleet_token
            self._write(pairings)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def get(self, relay: str) -> str:
        relay = canonical_relay_host(relay)
        token = self._load().get(relay)
        if not token:
            raise RunHostError("relay pairing is missing or revoked; securely transfer a fresh relay account link and run `mship run-host pair-relay`")
        return token
