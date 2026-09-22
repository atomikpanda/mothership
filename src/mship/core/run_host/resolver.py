"""Identity-bound, in-memory relay bearer resolution for run-host operations."""
from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from mship.core.relay import host_contract
from mship.core.relay.tls_ask import host_subdomain_allowed
from mship.core.run_host.config import (
    RunHostConnection,
    HostRegistration,
    RelayRunHostIdentity,
    ResolvedRunHostConnection,
)
from mship.core.run_host.pairing_store import RelayPairingStore
from mship.core.run_host.store import RunHostError

_MAX_DIRECTORY_BYTES = 64 * 1024
_MAX_TOKEN_BYTES = 4096
_MAX_EXPIRY_SECONDS = 24 * 60 * 60


class RelayResolutionError(RunHostError):
    """Safe relay setup/exchange error, with no protocol or credential detail."""


def _origin_for_relay(public_url: object, relay: str, subdomain: object) -> str:
    if not isinstance(public_url, str) or len(public_url) > 2048 or not isinstance(subdomain, str):
        raise RelayResolutionError("relay directory binding is invalid; re-enrol the host or correct its identity mapping")
    subdomain = subdomain.lower()
    expected = f"https://{subdomain}.{relay}"
    if not host_subdomain_allowed(subdomain) or public_url != expected:
        raise RelayResolutionError("relay directory binding is invalid; re-enrol the host or correct its identity mapping")
    try:
        parsed = urlsplit(public_url)
    except ValueError:
        raise RelayResolutionError("relay directory binding is invalid; re-enrol the host or correct its identity mapping") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != f"{subdomain}.{relay}"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RelayResolutionError("relay directory binding is invalid; re-enrol the host or correct its identity mapping")
    return expected


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _bounded_json(response: httpx.Response, limit: int) -> object:
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > limit:
            raise ValueError("response body exceeds limit")
        body.extend(chunk)
    payload = json.loads(
        body,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number: {value}")),
    )

    def validate(value: object, depth: int = 0) -> None:
        if depth > 16:
            raise ValueError("JSON payload is too deeply nested")
        if isinstance(value, dict):
            if len(value) > 256:
                raise ValueError("JSON object is too large")
            for item in value.values():
                validate(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > 256:
                raise ValueError("JSON list is too large")
            for item in value:
                validate(item, depth + 1)

    validate(payload)
    return payload


def _selected_entry(payload: object, identity: RelayRunHostIdentity) -> tuple[str, str]:
    if not isinstance(payload, dict) or set(payload) != {"hosts"} or not isinstance(payload["hosts"], list) or len(payload["hosts"]) > 256:
        raise RelayResolutionError("relay directory response is invalid; re-enrol the host or correct the identity mapping")
    matches = [entry for entry in payload["hosts"] if isinstance(entry, dict) and entry.get("host_id") == identity.host_id]
    if len(matches) != 1:
        raise RelayResolutionError("relay directory could not select the configured host; re-enrol it or correct the identity mapping")
    entry = matches[0]
    if set(entry) - {"host_id", "state", "label", "instance_id", "key_fingerprint", "machine_fingerprint", "subdomain", "public_url", "mship_version", "capabilities", "runner", "refresh", "first_seen", "last_seen", "previous_instance_id", "request_id", "created_at"}:
        raise RelayResolutionError("relay directory response is invalid; re-enrol the host or correct the identity mapping")
    if entry.get("state") != "online" or entry.get("host_id") != identity.host_id or entry.get("instance_id") != identity.instance_id:
        raise RelayResolutionError("relay directory binding changed; re-enrol the host or correct the identity mapping")
    refresh = entry.get("refresh")
    if not isinstance(refresh, str) or not refresh or len(refresh) > _MAX_TOKEN_BYTES:
        raise RelayResolutionError("relay host refresh is missing or revoked; re-enrol the host and re-pair if needed")
    return _origin_for_relay(entry.get("public_url"), identity.relay, entry.get("subdomain")), refresh


class RunHostResolver:
    """Resolve each selected registration at its actual HTTP/Git boundary."""

    def __init__(self, *, pairing_store: RelayPairingStore | None = None, transport: httpx.BaseTransport | None = None, monotonic: Callable[[], float] = time.monotonic, headroom_s: float = 30.0) -> None:
        if not math.isfinite(headroom_s) or headroom_s < 0:
            raise ValueError("resolver headroom must be finite and non-negative")
        self._pairing_store = pairing_store or RelayPairingStore()
        self._transport = transport
        self._monotonic = monotonic
        self._headroom_s = headroom_s
        self._cache: dict[tuple[tuple[str, ...], str], tuple[str, float]] = {}

    def select_relay_identity(self, *, relay: str, host_id: str, workspace_id: str) -> RelayRunHostIdentity:
        """Validate one directory selection for setup without retaining its refresh."""
        provisional = RelayRunHostIdentity(relay, host_id, workspace_id, "pending")
        fleet = self._fleet(provisional)
        payload = self._directory(provisional, fleet)
        if not isinstance(payload, dict) or not isinstance(payload.get("hosts"), list):
            raise RelayResolutionError("relay directory response is invalid; re-enrol the host or correct the identity mapping")
        entries = [entry for entry in payload["hosts"] if isinstance(entry, dict) and entry.get("host_id") == host_id]
        if len(entries) != 1 or not isinstance(entries[0].get("instance_id"), str) or not entries[0]["instance_id"]:
            raise RelayResolutionError("relay directory could not select the configured host; re-enrol it or correct the identity mapping")
        identity = RelayRunHostIdentity(relay, host_id, workspace_id, entries[0]["instance_id"])
        _selected_entry(payload, identity)
        return identity

    def _fleet(self, identity: RelayRunHostIdentity) -> str:
        try:
            return self._pairing_store.get(identity.relay)
        except RunHostError as exc:
            raise RelayResolutionError(str(exc)) from None

    def _directory(self, identity: RelayRunHostIdentity, fleet: str) -> object:
        try:
            with httpx.Client(transport=self._transport, follow_redirects=False, timeout=5.0) as client:
                with client.stream(
                    "GET",
                    host_contract.enroll_base_url(identity.relay) + host_contract.LIST_PATH,
                    headers={host_contract.FLEET_TOKEN_HEADER: fleet},
                ) as response:
                    if response.status_code == 401:
                        raise RelayResolutionError("relay pairing is missing or revoked; securely transfer a fresh relay account link and run `mship run-host pair-relay`")
                    if response.status_code != 200 or response.is_redirect:
                        raise RelayResolutionError("relay directory selection failed; re-enrol the host or correct the identity mapping")
                    return _bounded_json(response, _MAX_DIRECTORY_BYTES)
        except RelayResolutionError:
            raise
        except httpx.HTTPError:
            raise RelayResolutionError("could not contact the authenticated relay directory; check pairing and relay availability") from None
        except (UnicodeError, ValueError, RecursionError):
            raise RelayResolutionError("relay directory response is invalid; re-enrol the host or correct the identity mapping") from None

    def _exchange(self, origin: str, refresh: str) -> tuple[str, float]:
        try:
            with httpx.Client(transport=self._transport, follow_redirects=False, timeout=5.0) as client:
                with client.stream("POST", origin + "/host/token", json={"refresh": refresh}) as response:
                    if response.status_code in {401, 403}:
                        raise RelayResolutionError("selected relay host refresh is expired or revoked; re-enrol the host and re-pair if needed")
                    if response.status_code != 200 or response.is_redirect:
                        raise RelayResolutionError("selected relay host token exchange failed; re-enrol the host and re-pair if needed")
                    payload = _bounded_json(response, _MAX_DIRECTORY_BYTES)
        except RelayResolutionError:
            raise
        except httpx.HTTPError:
            raise RelayResolutionError("selected relay host token exchange failed; re-enrol the host and re-pair if needed") from None
        except (UnicodeError, ValueError, RecursionError):
            raise RelayResolutionError("selected relay host token exchange failed; re-enrol the host and re-pair if needed") from None
        if not isinstance(payload, dict) or set(payload) != {"token", "expires_in"} or not isinstance(payload["token"], str) or not payload["token"] or len(payload["token"]) > _MAX_TOKEN_BYTES:
            raise RelayResolutionError("selected relay host token exchange failed; re-enrol the host and re-pair if needed")
        expires_in = payload["expires_in"]
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not math.isfinite(expires_in) or expires_in <= 0 or expires_in > _MAX_EXPIRY_SECONDS:
            raise RelayResolutionError("selected relay host token exchange failed; re-enrol the host and re-pair if needed")
        return payload["token"], self._monotonic() + float(expires_in)

    def resolve(self, host: HostRegistration, *, force_refresh: bool = False) -> ResolvedRunHostConnection:
        connection = host.connection
        if isinstance(connection, RunHostConnection):
            return ResolvedRunHostConnection(connection.url, connection.token, ("direct", connection.url), None)
        identity = ("relay", connection.relay, connection.host_id, connection.workspace_id, connection.instance_id)
        fleet = self._fleet(connection)
        origin, refresh = _selected_entry(self._directory(connection, fleet), connection)
        key = (identity, origin)
        cached = self._cache.get(key)
        now = self._monotonic()
        if force_refresh:
            self._cache.pop(key, None)
            cached = None
        if cached is not None and now < cached[1] - self._headroom_s:
            return ResolvedRunHostConnection(origin, cached[0], identity, cached[1])
        for old_key in tuple(self._cache):
            if old_key[0] == identity and old_key != key:
                self._cache.pop(old_key, None)
        token, expires_at = self._exchange(origin, refresh)
        self._cache[key] = (token, expires_at)
        return ResolvedRunHostConnection(origin, token, identity, expires_at)
