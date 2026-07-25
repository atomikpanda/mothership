import json
from dataclasses import dataclass
from pathlib import Path

from mship.core.relay.health import HealthProbe
from mship.core.relay.runtime import RelayRuntimeRecord, write_runtime_record
from mship.core.topology import (
    EGRESS_ABSENT,
    EGRESS_ROUTED,
    EGRESS_UNKNOWN,
    GH_AUTH_ENV_TOKEN,
    GH_AUTH_BROKER,
    GH_AUTH_NONE,
    GH_AUTH_RELAY_ATTACH,
    RELAY_AUTH_FAILED,
    RELAY_NOT_CONFIGURED,
    RELAY_NOT_RUNNING,
    RELAY_OK,
    RELAY_UNREACHABLE,
    RUN_HOST_NOT_BOOTSTRAPPED,
    RUN_HOST_OK,
    RUN_HOST_ORPHAN_MAPPING,
    RUN_HOST_STALE_TOKEN,
    RUN_HOST_UNKNOWN_ROLE,
    RUN_HOST_UNMAPPED,
    RUN_HOST_UNREACHABLE,
    RUN_HOSTS_AMBIGUOUS_DEFAULT,
    RUN_HOSTS_NONE_DECLARED,
    SERVE_RELAY_ABSENT,
    SERVE_RELAY_RUNNING,
    probe_topology,
)


@dataclass
class FakeConfig:
    workspace: str = "ws"
    run_hosts: tuple = ()
    repos: dict = None
    relay: object = None

    def __post_init__(self):
        if self.repos is None:
            self.repos = {}


@dataclass
class FakeRepo:
    run_host: str | None = None


class FakeShell:
    def __init__(self, stdout="", returncode=0, raises=None):
        self._stdout, self._rc, self._raises = stdout, returncode, raises
        self.calls = []

    def run(self, cmd, cwd=None, **kw):
        self.calls.append(cmd)
        if self._raises:
            raise self._raises
        stdout, rc = self._stdout, self._rc

        class R:
            pass

        R.stdout, R.stderr, R.returncode = stdout, "", rc
        return R()


def _probe(**by_url):
    """Return a probe fn serving canned HealthProbes keyed by url."""
    def fn(url, token, *, timeout=None):
        return by_url.get(url, HealthProbe(ok=False, error="no route"))
    return fn


def _edges(t, kind):
    return [e for e in t.edges if e.kind == kind]


def _run(tmp_path, config, *, probe=None, env=None, shell=None, **kw):
    return probe_topology(
        config=config, state_dir=tmp_path / ".mothership", workspace_root=tmp_path,
        home=tmp_path, env=env or {}, probe=probe or _probe(), now=lambda: "t",
        pid_alive=lambda pid: True, shell=shell or FakeShell(), **kw,
    )


# --- serve + relay edges ---------------------------------------------------

def test_relay_absent_when_no_relay_configured(tmp_path: Path):
    t = _run(tmp_path, FakeConfig())
    relay = _edges(t, "relay")[0]
    assert relay.status == "absent" and relay.code == RELAY_NOT_CONFIGURED
    assert relay.fix is not None          # tells you how to configure one
    serve = _edges(t, "serve")[0]
    assert serve.code == SERVE_RELAY_ABSENT


def test_relay_reachable_reports_ok_and_no_fix(tmp_path: Path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="relay.example.com", pid=1, subdomain="abc-123456",
        url="https://abc-123456.relay.example.com", workspace="ws",
    ))
    t = _run(
        tmp_path, FakeConfig(relay=object()),
        probe=_probe(**{
            "https://abc-123456.relay.example.com": HealthProbe(ok=True, status_code=200)
        }),
    )
    relay = _edges(t, "relay")[0]
    assert relay.status == "ok" and relay.code == RELAY_OK and relay.fix is None
    assert _edges(t, "serve")[0].code == SERVE_RELAY_RUNNING


def test_relay_unreachable_carries_fix(tmp_path: Path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="relay.example.com", pid=1, subdomain="abc-123456",
        url="https://abc-123456.relay.example.com",
    ))
    t = _run(tmp_path, FakeConfig(relay=object()), probe=_probe())
    relay = _edges(t, "relay")[0]
    assert relay.status == "fail" and relay.code == RELAY_UNREACHABLE
    assert "serve --relay" in relay.fix


def test_relay_401_is_auth_failed_not_unreachable(tmp_path: Path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="relay.example.com", pid=1, subdomain="s", url="https://s.relay",
    ))
    t = _run(tmp_path, FakeConfig(relay=object()),
             probe=_probe(**{"https://s.relay": HealthProbe(ok=False, status_code=401)}))
    assert _edges(t, "relay")[0].code == RELAY_AUTH_FAILED


def test_dead_pid_reports_not_running_and_skips_the_probe(tmp_path: Path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="relay.example.com", pid=999999, subdomain="s", url="https://s.relay",
    ))
    calls = []

    def counting_probe(url, token, *, timeout=None):
        calls.append(url)
        return HealthProbe(ok=True, status_code=200)

    t = probe_topology(
        config=FakeConfig(relay=object()), state_dir=tmp_path / ".mothership",
        workspace_root=tmp_path, home=tmp_path, env={},
        probe=counting_probe, now=lambda: "t", pid_alive=lambda pid: False,
        shell=FakeShell(),
    )
    assert _edges(t, "relay")[0].code == RELAY_NOT_RUNNING
    assert calls == []          # no point probing a tunnel whose serve is gone


def test_skip_network_never_probes(tmp_path: Path):
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="relay.example.com", pid=1, subdomain="s", url="https://s.relay",
    ))
    calls = []

    def counting_probe(url, token, *, timeout=None):
        calls.append(url)
        return HealthProbe(ok=True, status_code=200)

    t = _run(tmp_path, FakeConfig(relay=object()), probe=counting_probe,
             skip_network=True)
    assert calls == []
    assert _edges(t, "relay")[0].status in ("warn", "absent")


def test_probed_at_and_version_present(tmp_path: Path):
    t = _run(tmp_path, FakeConfig(), probe=_probe())
    assert t.probed_at == "t"
    assert t.version == 1 and t.workspace == "ws"


# --- run-host edges --------------------------------------------------------

def _map_role(tmp_path: Path, role: str, url: str, token: str = "tok"):
    from mship.core.run_host.config import RunHostConnection
    from mship.core.run_host.store import RunHostStore
    RunHostStore(tmp_path / ".mothership").set(role, RunHostConnection(url=url, token=token))


def _named(t, name):
    return [e for e in t.edges if e.name == name][0]


def test_no_roles_declared_is_an_absent_aggregate_edge(tmp_path: Path):
    t = _run(tmp_path, FakeConfig())
    agg = _named(t, "run_hosts")
    assert agg.status == "absent" and agg.code == RUN_HOSTS_NONE_DECLARED


def test_declared_but_unmapped_role_names_run_host_add(tmp_path: Path):
    t = _run(tmp_path, FakeConfig(run_hosts=("mac",)))
    edge = _named(t, "run_host:mac")
    assert edge.status == "fail" and edge.code == RUN_HOST_UNMAPPED
    assert "mship run-host add mac" in edge.fix


def test_mapped_and_reachable_role_is_ok_with_source(tmp_path: Path):
    _map_role(tmp_path, "mac", "https://mac.relay")
    t = _run(tmp_path, FakeConfig(run_hosts=("mac",)),
             probe=_probe(**{"https://mac.relay": HealthProbe(ok=True, status_code=200)}))
    edge = _named(t, "run_host:mac")
    assert edge.status == "ok" and edge.code == RUN_HOST_OK
    assert edge.facts["url"] == "https://mac.relay"
    assert edge.facts["url_source"] == "file"
    assert edge.facts["token_configured"] is True


def test_env_override_is_reported_as_the_effective_source(tmp_path: Path):
    _map_role(tmp_path, "mac", "https://from-file")
    t = _run(
        tmp_path, FakeConfig(run_hosts=("mac",)),
        env={"MSHIP_RUN_HOST_MAC_URL": "https://from-env"},
        probe=_probe(**{"https://from-env": HealthProbe(ok=True, status_code=200)}),
    )
    edge = _named(t, "run_host:mac")
    assert edge.facts["url"] == "https://from-env"
    assert edge.facts["url_source"] == "env:MSHIP_RUN_HOST_MAC_URL"


def test_503_is_not_bootstrapped(tmp_path: Path):
    _map_role(tmp_path, "mac", "https://mac.relay")
    t = _run(tmp_path, FakeConfig(run_hosts=("mac",)),
             probe=_probe(**{"https://mac.relay": HealthProbe(ok=False, status_code=503)}))
    edge = _named(t, "run_host:mac")
    assert edge.code == RUN_HOST_NOT_BOOTSTRAPPED
    assert "bootstrap" in edge.fix


def test_401_is_stale_token(tmp_path: Path):
    _map_role(tmp_path, "mac", "https://mac.relay")
    t = _run(tmp_path, FakeConfig(run_hosts=("mac",)),
             probe=_probe(**{"https://mac.relay": HealthProbe(ok=False, status_code=401)}))
    edge = _named(t, "run_host:mac")
    assert edge.code == RUN_HOST_STALE_TOKEN
    assert "mship run-host add mac" in edge.fix


def test_transport_error_is_unreachable(tmp_path: Path):
    _map_role(tmp_path, "mac", "https://mac.relay")
    t = _run(tmp_path, FakeConfig(run_hosts=("mac",)), probe=_probe())
    assert _named(t, "run_host:mac").code == RUN_HOST_UNREACHABLE


def test_repo_declaring_an_undeclared_role_is_unknown_role(tmp_path: Path):
    cfg = FakeConfig(run_hosts=("mac",), repos={"api": FakeRepo(run_host="typo")})
    t = _run(tmp_path, cfg)
    edge = _named(t, "run_host:typo")
    assert edge.status == "fail" and edge.code == RUN_HOST_UNKNOWN_ROLE
    assert "api" in edge.detail          # names the repo that points at it


def test_ambiguous_default_when_two_roles_and_no_repo_default(tmp_path: Path):
    _map_role(tmp_path, "mac", "https://mac")
    _map_role(tmp_path, "linux", "https://linux")
    t = _run(tmp_path, FakeConfig(run_hosts=("mac", "linux")))
    agg = _named(t, "run_hosts")
    assert agg.status == "warn" and agg.code == RUN_HOSTS_AMBIGUOUS_DEFAULT
    assert "--remote=<role>" in agg.fix


def test_mapping_for_an_undeclared_role_is_an_orphan_warning(tmp_path: Path):
    _map_role(tmp_path, "gone", "https://gone")
    t = _run(tmp_path, FakeConfig(run_hosts=("mac",)))
    edge = _named(t, "run_host:gone")
    assert edge.status == "warn" and edge.code == RUN_HOST_ORPHAN_MAPPING


def test_unmapped_role_is_never_probed(tmp_path: Path):
    calls = []

    def counting(url, token, *, timeout=None):
        calls.append(url)
        return HealthProbe(ok=True, status_code=200)

    _run(tmp_path, FakeConfig(run_hosts=("mac",)), probe=counting)
    assert calls == []


# --- gh auth + egress edges ------------------------------------------------

def test_gh_auth_none_when_nothing_configured(tmp_path: Path):
    t = _run(tmp_path, FakeConfig(), shell=FakeShell())
    edge = _edges(t, "gh_auth")[0]
    assert edge.code == GH_AUTH_NONE and edge.status == "warn"
    assert "MSHIP_GH_BROKER_URL" in edge.fix


def test_gh_auth_reports_the_app_capability_without_leaking_the_key(tmp_path: Path):
    key = tmp_path / "app.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nsupersecret\n")
    t = _run(
        tmp_path, FakeConfig(), shell=FakeShell(),
        env={"MSHIP_GH_APP_ID": "12345", "MSHIP_GH_APP_KEY": str(key)},
    )
    edge = _edges(t, "gh_auth")[0]
    assert edge.facts["serves_app_backed_tokens"] is True
    assert edge.facts["app_key_readable"] is True
    assert "supersecret" not in json.dumps(edge.facts)
    assert "12345" not in json.dumps(edge.facts)   # an App id is credential-adjacent


def test_gh_auth_broker_and_relay_attach(tmp_path: Path):
    t = _run(tmp_path, FakeConfig(), shell=FakeShell(),
             env={"MSHIP_GH_BROKER_URL": "https://b", "MSHIP_SERVE_TOKEN": "s"})
    assert _edges(t, "gh_auth")[0].code == GH_AUTH_BROKER

    t2 = _run(tmp_path, FakeConfig(), shell=FakeShell(),
              env={"MSHIP_RELAY_URL": "https://r", "MSHIP_RUN_TOKEN": "rt"})
    assert _edges(t2, "gh_auth")[0].code == GH_AUTH_RELAY_ATTACH


def test_egress_routed_when_git_config_has_the_rewrite(tmp_path: Path):
    shell = FakeShell(stdout=(
        "url.https://egress.example.com/gh/.insteadof https://github.com/\n"
        "url.https://egress.example.com/api/.insteadof https://api.github.com/\n"
    ))
    t = _run(tmp_path, FakeConfig(), shell=shell)
    edge = _edges(t, "egress")[0]
    assert edge.code == EGRESS_ROUTED and edge.status == "ok"
    assert edge.facts["relay_base"] == "https://egress.example.com"


def test_egress_absent_when_git_config_is_clean(tmp_path: Path):
    t = _run(tmp_path, FakeConfig(), shell=FakeShell(stdout="", returncode=1))
    edge = _edges(t, "egress")[0]
    assert edge.code == EGRESS_ABSENT and edge.status == "absent"


def test_egress_unknown_when_git_is_unavailable(tmp_path: Path):
    t = _run(tmp_path, FakeConfig(), shell=FakeShell(raises=OSError("no git")))
    edge = _edges(t, "egress")[0]
    assert edge.code == EGRESS_UNKNOWN and edge.status == "warn"


def test_fully_broken_environment_still_returns_every_edge(tmp_path: Path):
    """AC4: serve down, relay unreachable, no run hosts mapped, no git,
    no auth — probe_topology must return a report, not raise."""
    cfg = FakeConfig(run_hosts=("mac", "linux"), relay=object())
    t = probe_topology(
        config=cfg, state_dir=tmp_path / "nope", workspace_root=tmp_path / "nope",
        home=tmp_path / "nope", env={}, probe=_probe(),
        shell=FakeShell(raises=OSError("boom")), now=lambda: "t",
        pid_alive=lambda pid: False,
    )
    assert {e.kind for e in t.edges} == {"serve", "relay", "run_host", "gh_auth", "egress"}
    # every unhealthy edge offers a next step
    assert all(e.fix for e in t.edges if e.status in ("fail", "warn"))


def test_every_probe_call_is_timeout_bounded(tmp_path: Path):
    seen = []

    def recording(url, token, *, timeout=None):
        seen.append(timeout)
        return HealthProbe(ok=True, status_code=200)

    _map_role(tmp_path, "mac", "https://mac")
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="h", pid=1, subdomain="s", url="https://s.relay", workspace="ws",
    ))
    _run(tmp_path, FakeConfig(run_hosts=("mac",), relay=object()),
         probe=recording, shell=FakeShell())
    assert seen and all(isinstance(x, float) and x > 0 for x in seen)


# --- code vocabulary guards ------------------------------------------------

def test_documented_failure_modes_have_distinct_codes():
    """AC8: docs/remote-run.md's troubleshooting rows map 1:1 onto codes."""
    documented = {
        "unknown role": RUN_HOST_UNKNOWN_ROLE,
        "ambiguous run-host": RUN_HOSTS_AMBIGUOUS_DEFAULT,
        "role unmapped on this machine": RUN_HOST_UNMAPPED,
        "relay unreachable": RUN_HOST_UNREACHABLE,
        "remote not bootstrapped (503)": RUN_HOST_NOT_BOOTSTRAPPED,
        "stale token (401)": RUN_HOST_STALE_TOKEN,
    }
    assert len(set(documented.values())) == len(documented)


def test_every_status_code_constant_is_unique():
    """A copy-pasted constant would silently merge two states in the UI."""
    from mship.core import topology as topo

    codes = [
        v for k, v in vars(topo).items()
        if k.isupper() and isinstance(v, str) and not k.startswith("_")
        and k != "SCHEMA_VERSION"
    ]
    assert len(codes) == len(set(codes)), "duplicate status-code value"


def test_no_unhealthy_edge_is_ever_left_without_a_fix(tmp_path: Path):
    """Across every unhealthy shape this module can produce, `fix` is set —
    a status code with no next step is a dead end for the operator."""
    cfg = FakeConfig(run_hosts=("mac", "linux"), relay=object(),
                     repos={"api": FakeRepo(run_host="typo")})
    _map_role(tmp_path, "linux", "https://linux.relay")
    _map_role(tmp_path, "orphan", "https://orphan.relay")
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="h", pid=1, subdomain="s", url="https://s.relay", workspace="ws",
    ))
    t = _run(
        tmp_path, cfg,
        probe=_probe(**{"https://linux.relay": HealthProbe(ok=False, status_code=503)}),
        shell=FakeShell(raises=OSError("no git")),
    )
    unhealthy = [e for e in t.edges if e.status in ("warn", "fail")]
    assert unhealthy, "expected this fixture to produce unhealthy edges"
    missing = [e.name for e in unhealthy if not e.fix]
    assert missing == [], f"edges with no fix hint: {missing}"


# --- not-probed is its own state, and a corrupt store cannot crash the probe ---

def test_skipped_probe_is_not_reported_as_unreachable(tmp_path: Path):
    """A UI branching on `relay_unreachable` must not light up merely because
    probes were skipped — "not probed" is a different state from "down"."""
    from mship.core.topology import PROBE_SKIPPED

    _map_role(tmp_path, "mac", "https://mac.relay")
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="h", pid=1, subdomain="s", url="https://s.relay", workspace="ws",
    ))
    t = _run(tmp_path, FakeConfig(run_hosts=("mac",), relay=object()),
             skip_network=True)

    assert _named(t, "relay").code == PROBE_SKIPPED
    assert _named(t, "run_host:mac").code == PROBE_SKIPPED
    assert RELAY_UNREACHABLE not in {e.code for e in t.edges}
    assert RUN_HOST_UNREACHABLE not in {e.code for e in t.edges}


def test_relay_record_without_a_url_is_its_own_state(tmp_path: Path):
    from mship.core.topology import RELAY_NO_PUBLIC_URL

    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="h", pid=1, subdomain="s", url=None, workspace="ws",
    ))
    t = _run(tmp_path, FakeConfig(relay=object()))
    assert _named(t, "relay").code == RELAY_NO_PUBLIC_URL


def test_corrupt_run_host_store_does_not_raise(tmp_path: Path):
    """AC4: a broken environment is the expected input. A hand-edited
    run-hosts.yaml must degrade to a reported edge, not a traceback."""
    from mship.core.topology import RUN_HOSTS_STORE_UNREADABLE

    state = tmp_path / ".mothership"
    state.mkdir(parents=True)
    (state / "run-hosts.yaml").write_text("mac: {url: [unclosed\n")

    t = _run(tmp_path, FakeConfig(run_hosts=("mac",)))
    edge = _named(t, "run_hosts")
    assert edge.status == "warn" and edge.code == RUN_HOSTS_STORE_UNREADABLE
    assert "run-hosts.yaml" in edge.fix
    # the other edge kinds still reported
    assert {e.kind for e in t.edges} >= {"serve", "relay", "gh_auth", "egress"}


def test_non_mapping_run_host_store_does_not_raise(tmp_path: Path):
    from mship.core.topology import RUN_HOSTS_STORE_UNREADABLE

    state = tmp_path / ".mothership"
    state.mkdir(parents=True)
    (state / "run-hosts.yaml").write_text("just a string\n")

    t = _run(tmp_path, FakeConfig(run_hosts=("mac",)))
    assert _named(t, "run_hosts").code == RUN_HOSTS_STORE_UNREADABLE


# --- Greptile P1 findings ---------------------------------------------------

def test_relay_probe_uses_the_env_serve_token_when_set(tmp_path: Path):
    """Greptile P1: `ensure_serve_token` is env-override > file, and the env
    value is never written to the file. Probing with the file value would 401 on
    a healthy relay and report relay_auth_failed."""
    state = tmp_path / ".mothership"
    state.mkdir(parents=True)
    (state / "serve-token").write_text("stale-file-token\n")
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="h", pid=1, subdomain="s", url="https://s.relay", workspace="ws",
    ))
    seen = {}

    def recording(url, token, *, timeout=None):
        seen["token"] = token
        return HealthProbe(ok=True, status_code=200)

    t = _run(tmp_path, FakeConfig(relay=object()), probe=recording,
             env={"MSHIP_SERVE_TOKEN": "live-env-token"})
    assert seen["token"] == "live-env-token"
    assert _named(t, "relay").code == RELAY_OK


def test_relay_probe_falls_back_to_the_file_token(tmp_path: Path):
    state = tmp_path / ".mothership"
    state.mkdir(parents=True)
    (state / "serve-token").write_text("file-token\n")
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="h", pid=1, subdomain="s", url="https://s.relay", workspace="ws",
    ))
    seen = {}

    def recording(url, token, *, timeout=None):
        seen["token"] = token
        return HealthProbe(ok=True, status_code=200)

    _run(tmp_path, FakeConfig(relay=object()), probe=recording, env={})
    assert seen["token"] == "file-token"


def test_non_utf8_serve_token_does_not_raise(tmp_path: Path):
    """Greptile P1: read_text raises UnicodeDecodeError (a ValueError, NOT an
    OSError) on a corrupt token, which an OSError-only handler misses."""
    state = tmp_path / ".mothership"
    state.mkdir(parents=True)
    (state / "serve-token").write_bytes(b"\xff\xfe not utf-8 \x00")
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="h", pid=1, subdomain="s", url="https://s.relay", workspace="ws",
    ))
    t = _run(tmp_path, FakeConfig(relay=object()))
    assert {e.kind for e in t.edges} >= {"serve", "relay"}


def test_non_utf8_relay_pubkey_does_not_raise(tmp_path: Path):
    """Same hole in the drift check's read of the public key."""
    state = tmp_path / ".mothership"
    state.mkdir(parents=True)
    (state / "relay-subdomain-secret").write_bytes(b"x" * 32)
    (state / "relay_ed25519.pub").write_bytes(b"\xff\xfe binary")
    write_runtime_record(tmp_path, RelayRuntimeRecord(
        host="h", pid=1, subdomain="s", url="https://s.relay", workspace="ws",
    ))
    t = _run(tmp_path, FakeConfig(relay=object()))
    assert _named(t, "relay").code in {RELAY_OK, RELAY_UNREACHABLE}


def test_env_token_wins_over_app_creds_in_the_reported_model(tmp_path: Path):
    """Greptile P1: App creds serve `GET /gh-token` for CALLERS (Broker B) —
    they are not how this machine authenticates its own git ops. With GH_TOKEN
    set, `resolve_token` uses GH_TOKEN, so that is the model in effect."""
    from mship.core.topology import GH_AUTH_ENV_TOKEN

    key = tmp_path / "app.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nx\n")
    t = _run(tmp_path, FakeConfig(), env={
        "MSHIP_GH_APP_ID": "1", "MSHIP_GH_APP_KEY": str(key),
        "GH_TOKEN": "ghp_x",
    })
    edge = _edges(t, "gh_auth")[0]
    assert edge.code == GH_AUTH_ENV_TOKEN
    # the App capability is still reported, as what it actually is
    assert edge.facts["serves_app_backed_tokens"] is True


def test_app_creds_alone_report_the_broker_capability_not_a_client_model(tmp_path: Path):
    from mship.core.topology import GH_AUTH_NONE

    key = tmp_path / "app.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nx\n")
    t = _run(tmp_path, FakeConfig(), env={
        "MSHIP_GH_APP_ID": "1", "MSHIP_GH_APP_KEY": str(key),
    })
    edge = _edges(t, "gh_auth")[0]
    # This host mints App tokens for others but has no client auth of its own.
    assert edge.code == GH_AUTH_NONE
    assert edge.facts["serves_app_backed_tokens"] is True
    assert "App-backed" in edge.detail


def test_unreadable_app_key_is_a_distinct_failure(tmp_path: Path):
    from mship.core.topology import GH_AUTH_APP_KEY_UNREADABLE

    t = _run(tmp_path, FakeConfig(), env={
        "MSHIP_GH_APP_ID": "1", "MSHIP_GH_APP_KEY": str(tmp_path / "missing.pem"),
    })
    edge = _edges(t, "gh_auth")[0]
    assert edge.status == "fail" and edge.code == GH_AUTH_APP_KEY_UNREADABLE
