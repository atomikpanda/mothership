"""The single derivation of a relay pairing deep-link, shared by `mship serve
--relay` and `mship pair` so their output is byte-for-byte identical for the same
workspace + relay host (spec mship-pair-relay-host, ac3/ac4).

Collaborators are called via their MODULES (keys.*, tunnel.*, token.*, pairing.*)
rather than `from x import name`, so tests that monkeypatch those functions at
their source modules (e.g. tests/cli/test_serve.py) still intercept them here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mship.core.relay import keys, pairing, token, tunnel


@dataclass(frozen=True)
class RelayPairLink:
    host: str
    subdomain: str
    url: str
    token: str
    link: str


def build_relay_pair_link(
    *, workspace: str, host: str, workspace_root: Path, home: Path
) -> RelayPairLink:
    """Derive the opaque per-device subdomain + serve token and build the
    `groundcontrol://add?…` deep-link for `workspace` on relay `host`.

    - subdomain: `device_subdomain(workspace, device_id(pubkey), secret)` from
      `~/.mothership/relay_ed25519(.pub)` + `~/.mothership/relay-subdomain-secret`.
    - token: `ensure_serve_token(workspace_root)` (env > `.mothership/serve-token` >
      generated) — the SAME source `mship serve` uses.
    """
    key_path = keys.ensure_relay_key(home=home)
    secret = keys.ensure_subdomain_secret(home=home)
    dev = tunnel.device_id(keys.relay_public_key(key_path))
    subdomain = tunnel.device_subdomain(workspace, dev, secret)
    url = f"https://{subdomain}.{host}"
    tok = token.ensure_serve_token(workspace_root)
    link = pairing.build_pair_link(url=url, token=tok, workspace=workspace)
    return RelayPairLink(host=host, subdomain=subdomain, url=url, token=tok, link=link)
