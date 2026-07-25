"""AC7: no secret material in any topology output.

Plant a unique sentinel in every secret-bearing input topology reads, then
assert none survive into the serialized payload. Serializing the WHOLE payload
(not per-field) is deliberate: a new edge that leaks a token fails this test
without anyone remembering to extend it.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from mship.core.relay.health import HealthProbe
from mship.core.run_host.config import RunHostConnection
from mship.core.run_host.store import RunHostStore
from mship.core.topology import probe_topology, topology_payload

SECRETS = {
    "run_host_token": "SENTINEL-runhost-token",
    "serve_token": "SENTINEL-serve-token",
    "env_run_host_token": "SENTINEL-env-runhost-token",
    "gh_token": "SENTINEL-gh-token",
    "run_token": "SENTINEL-run-token",
    "serve_token_env": "SENTINEL-serve-token-env",
    "app_key_body": "SENTINEL-app-private-key-body",
    "subdomain_secret": "SENTINEL-subdomain-secret-bytes",
}


@dataclass
class Cfg:
    workspace: str = "ws"
    run_hosts: tuple = ("mac",)
    repos: dict = None
    relay: object = None

    def __post_init__(self):
        self.repos = self.repos or {}


class Shell:
    def run(self, cmd, cwd=None, **kw):
        class R:
            stdout, stderr, returncode = "", "", 1
        return R()


def _ok_probe(url, token, *, timeout=None):
    return HealthProbe(ok=True, status_code=200)


def test_no_secret_reaches_the_payload(tmp_path: Path):
    state = tmp_path / ".mothership"
    state.mkdir(parents=True)
    (state / "serve-token").write_text(SECRETS["serve_token"])
    (state / "relay-subdomain-secret").write_bytes(
        SECRETS["subdomain_secret"].encode() * 2
    )
    RunHostStore(state).set("mac", RunHostConnection(
        url="https://mac.relay", token=SECRETS["run_host_token"],
    ))
    key = tmp_path / "app.pem"
    key.write_text(f"-----BEGIN PRIVATE KEY-----\n{SECRETS['app_key_body']}\n")

    env = {
        "MSHIP_RUN_HOST_MAC_TOKEN": SECRETS["env_run_host_token"],
        "GH_TOKEN": SECRETS["gh_token"],
        "MSHIP_RUN_TOKEN": SECRETS["run_token"],
        "MSHIP_SERVE_TOKEN": SECRETS["serve_token_env"],
        "MSHIP_GH_APP_ID": "999",
        "MSHIP_GH_APP_KEY": str(key),
        "MSHIP_RELAY_URL": "https://relay.example.com",
    }

    topology = probe_topology(
        config=Cfg(relay=object()), state_dir=state, workspace_root=tmp_path,
        home=tmp_path, env=env, shell=Shell(), now=lambda: "t",
        pid_alive=lambda pid: True, probe=_ok_probe,
    )
    blob = json.dumps(topology_payload(topology))

    leaked = [name for name, value in SECRETS.items() if value in blob]
    assert leaked == [], f"topology payload leaked: {leaked}"


def test_token_presence_is_still_reported_as_a_boolean(tmp_path: Path):
    """Redaction must not cost the operator the information they need: the
    payload says a token IS configured, without saying what it is."""
    state = tmp_path / ".mothership"
    state.mkdir(parents=True)
    RunHostStore(state).set("mac", RunHostConnection(
        url="https://mac.relay", token=SECRETS["run_host_token"],
    ))
    topology = probe_topology(
        config=Cfg(), state_dir=state, workspace_root=tmp_path, home=tmp_path,
        env={}, shell=Shell(), now=lambda: "t", pid_alive=lambda pid: True,
        probe=_ok_probe,
    )
    edge = [e for e in topology.edges if e.name == "run_host:mac"][0]
    assert edge.facts["token_configured"] is True
