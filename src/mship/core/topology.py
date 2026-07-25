"""Read-only connectivity topology: what this machine is wired to, and whether
each edge is healthy.

ONE implementation, three thin callers — `mship net status`, `mship doctor`'s
connectivity group, and `GET /net/topology` on serve — so probe logic lives in
exactly one module.

Two invariants the callers depend on:

1. **Read-only.** Nothing here creates, writes, or rotates state. That rules out
   the `ensure_*` helpers in `relay.keys` and `relay.token` (they generate on
   absence) — this module reads their paths instead.
2. **Never raises.** A broken environment is the EXPECTED input; this tool is
   most needed exactly when connectivity is broken. Every failure becomes an
   edge status plus a fix hint, and every network probe is timeout-bounded.

`GET /net/topology`'s payload is the UI contract for the serve-host console, so
it carries `version` (SCHEMA_VERSION) and must be renderable on its own — no
companion in-process data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

#: Bound on every network probe. Short on purpose: `doctor` was fast before
#: connectivity checks existed, and an unreachable edge must not stall it.
PROBE_TIMEOUT_SECONDS = 3.0

#: `absent` = not configured on this machine (nothing to fix), distinct from
#: `fail` = configured but broken.
STATUSES = ("ok", "warn", "fail", "absent")

# --- status codes ---------------------------------------------------------
# One code per distinguishable state so a UI can branch without parsing prose.
# The prose lives in `Edge.detail` / `Edge.fix`.

SERVE_RELAY_RUNNING = "serve_relay_running"
SERVE_RELAY_STALE = "serve_relay_stale"
SERVE_RELAY_ABSENT = "serve_relay_absent"

RELAY_OK = "relay_ok"
RELAY_UNREACHABLE = "relay_unreachable"
RELAY_AUTH_FAILED = "relay_auth_failed"
RELAY_SUBDOMAIN_DRIFT = "relay_subdomain_drift"
RELAY_NOT_CONFIGURED = "relay_not_configured"
RELAY_NOT_RUNNING = "relay_not_running"
RELAY_NO_PUBLIC_URL = "relay_no_public_url"

#: Probes were skipped (`--no-network`), so reachability is UNKNOWN. Distinct
#: from `*_unreachable`, which asserts a probe ran and failed — a console that
#: conflated them would report a healthy relay as down.
PROBE_SKIPPED = "probe_skipped"

RUN_HOSTS_NONE_DECLARED = "run_hosts_none_declared"
RUN_HOSTS_AMBIGUOUS_DEFAULT = "run_hosts_ambiguous_default"
RUN_HOSTS_OK = "run_hosts_ok"
RUN_HOSTS_STORE_UNREADABLE = "run_hosts_store_unreadable"
RUN_HOST_OK = "run_host_ok"
RUN_HOST_UNKNOWN_ROLE = "run_host_unknown_role"
RUN_HOST_UNMAPPED = "run_host_unmapped"
RUN_HOST_UNREACHABLE = "run_host_unreachable"
RUN_HOST_NOT_BOOTSTRAPPED = "run_host_not_bootstrapped"
RUN_HOST_STALE_TOKEN = "run_host_stale_token"
RUN_HOST_ORPHAN_MAPPING = "run_host_orphan_mapping"

GH_AUTH_BROKER = "gh_auth_broker"
GH_AUTH_ENV_TOKEN = "gh_auth_env_token"
GH_AUTH_RELAY_ATTACH = "gh_auth_relay_attach"
GH_AUTH_NONE = "gh_auth_none"

#: A misconfiguration, not a model: MSHIP_GH_APP_ID is set but its key file is
#: unreadable, so `GET /gh-token` cannot mint App tokens for callers.
GH_AUTH_APP_KEY_UNREADABLE = "gh_auth_app_key_unreadable"

EGRESS_ROUTED = "egress_routed"
EGRESS_ABSENT = "egress_absent"
EGRESS_UNKNOWN = "egress_unknown"


@dataclass(frozen=True)
class Edge:
    """One connectivity edge (or node) and its health.

    `facts` holds REDACTED, source-annotated values only — urls, hostnames,
    booleans, and where an effective value came from. Never a token, key, or
    credential body (see `tests/core/test_topology_redaction.py`).
    """
    kind: str          # serve | relay | run_host | gh_auth | egress
    name: str          # "serve", "relay", "run_host:mac-studio", ...
    status: str        # one of STATUSES
    code: str          # one of the module's status-code constants
    detail: str        # human summary
    fix: str | None    # actionable next step; None when there is nothing to fix
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Topology:
    version: int
    workspace: str
    probed_at: str     # ISO-8601, UTC
    edges: list[Edge]


def topology_payload(topology: Topology) -> dict[str, Any]:
    """The JSON body shared by `mship net status --json` and
    `GET /net/topology` — ONE serializer, so the CLI and the endpoint can never
    disagree about the contract."""
    return {
        "version": topology.version,
        # Additive, so SCHEMA_VERSION does not change: existing consumers keep
        # working and new ones can rely on it being present.
        "mship_version": _mship_version(),
        "workspace": topology.workspace,
        "probed_at": topology.probed_at,
        "edges": [
            {
                "kind": e.kind,
                "name": e.name,
                "status": e.status,
                "code": e.code,
                "detail": e.detail,
                "fix": e.fix,
                "facts": dict(e.facts),
            }
            for e in topology.edges
        ],
    }


def _mship_version() -> str:
    """The running mship version, for the console footer.

    An additive payload field: a server-rendered page is a snapshot, so it must
    say which build produced it — and a separately-shipped frontend has to be
    able to render that footer from the endpoint response alone, which it could
    not do if the version were passed beside the payload into a template
    context. Never raises; an unresolvable version is reported as "unknown".
    """
    try:
        from importlib.metadata import version
        return version("mothership")
    except Exception:
        return "unknown"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _default_probe(url: str, token: str, *, timeout: float = PROBE_TIMEOUT_SECONDS):
    from mship.core.relay.health import probe_health
    return probe_health(url, token, timeout=timeout)


def _serve_token(workspace_root, *, env) -> str | None:
    """This host's serve bearer, read WITHOUT creating one.

    Mirrors `relay.token.ensure_serve_token`'s precedence — env override BEFORE
    the persisted file — minus the generate-on-absence step. The env value is
    never written to the file, so reading only the file would probe a running
    relay with a stale bearer and report a healthy tunnel as `relay_auth_failed`.

    A corrupt (non-UTF-8) token file yields None rather than raising:
    `UnicodeDecodeError` is a ValueError, not an OSError, so both are caught.
    """
    from pathlib import Path

    from_env = env.get("MSHIP_SERVE_TOKEN")
    if from_env:
        return from_env
    try:
        return Path(workspace_root).joinpath(".mothership", "serve-token").read_text().strip()
    except (OSError, ValueError):
        return None


def _subdomain_drift(record, *, home) -> str | None:
    """The subdomain this machine derives NOW, when it differs from the running
    record's — i.e. anything paired against the record is stale. None when they
    agree, or when the inputs to derive it are missing/unreadable.

    Strictly read-only: uses the key/secret PATHS, never the `ensure_*`
    generators (which would create them as a side effect of reporting).
    """
    from pathlib import Path

    from mship.core.relay.keys import relay_key_path, subdomain_secret_path
    from mship.core.relay.tunnel import device_id, device_subdomain

    if not record.subdomain or not record.workspace:
        return None
    try:
        secret = subdomain_secret_path(home).read_bytes()
        pub = Path(str(relay_key_path(home)) + ".pub").read_text()
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError on a binary/corrupt .pub file.
        return None
    try:
        expected = device_subdomain(record.workspace, device_id(pub), secret)
    except Exception:
        return None
    return None if expected == record.subdomain else expected


def _serve_and_relay_edges(
    *, config, workspace_root, home, env, probe, pid_alive, skip_network, timeout,
) -> list[Edge]:
    """The `serve --relay` process on this machine and the relay edge it owns.

    Both come from the same runtime record, so they are built together: the
    relay edge is only meaningful when a live relay-serve wrote that record.
    """
    from mship.core.relay.runtime import read_runtime_record

    record = read_runtime_record(workspace_root)
    relay_configured = getattr(config, "relay", None) is not None

    if record is None:
        serve = Edge(
            kind="serve", name="serve", status="absent", code=SERVE_RELAY_ABSENT,
            detail="no relay-serve record on this machine",
            fix=("start one with `mship serve --relay` (a local-only "
                 "`mship serve` writes no record and needs no relay)"),
            facts={"relay_configured": relay_configured},
        )
        relay = Edge(
            kind="relay", name="relay", status="absent", code=RELAY_NOT_CONFIGURED,
            detail=("relay configured but not running" if relay_configured
                    else "no `relay:` block in mothership.yaml"),
            fix=("run `mship serve --relay`" if relay_configured else
                 "add a `relay:` block (host, ssh_port, user) to mothership.yaml, "
                 "then `mship relay enroll` and `mship serve --relay`"),
            facts={"relay_configured": relay_configured},
        )
        return [serve, relay]

    alive = pid_alive(record.pid)
    serve = Edge(
        kind="serve", name="serve",
        status="ok" if alive else "fail",
        code=SERVE_RELAY_RUNNING if alive else SERVE_RELAY_STALE,
        detail=(f"relay-serve pid {record.pid} running" if alive else
                f"stale record: pid {record.pid} is gone"),
        fix=None if alive else (
            "the recorded relay-serve died; restart `mship serve --relay` "
            "(the stale record is cleared on next start)"
        ),
        facts={
            "mode": "relay", "pid": record.pid, "host": record.host,
            "subdomain": record.subdomain, "url": record.url,
            "workspace": record.workspace, "ssh_port": record.ssh_port,
        },
    )

    facts = {"host": record.host, "subdomain": record.subdomain, "url": record.url}
    drift = _subdomain_drift(record, home=home)
    if drift is not None:
        facts["expected_subdomain"] = drift

    if not alive:
        relay = Edge(
            kind="relay", name="relay", status="fail", code=RELAY_NOT_RUNNING,
            detail="no live relay-serve owns this tunnel",
            fix="restart `mship serve --relay` on this machine",
            facts=facts,
        )
        return [serve, relay]

    if drift is not None:
        relay = Edge(
            kind="relay", name="relay", status="warn", code=RELAY_SUBDOMAIN_DRIFT,
            detail=(f"running subdomain {record.subdomain!r} is not the one this "
                    f"machine now derives ({drift!r})"),
            fix=("anything paired against the old subdomain is stale — re-pair "
                 "(`mship pair`, re-scan the QR) or restart `mship serve --relay` "
                 "to publish the derived subdomain"),
            facts=facts,
        )
        return [serve, relay]

    if skip_network:
        relay = Edge(
            kind="relay", name="relay", status="warn", code=PROBE_SKIPPED,
            detail="network probes skipped; relay reachability unknown",
            fix="re-run without --no-network to probe the relay URL",
            facts=facts,
        )
        return [serve, relay]

    if not record.url:
        relay = Edge(
            kind="relay", name="relay", status="warn", code=RELAY_NO_PUBLIC_URL,
            detail="the relay-serve record carries no public url to probe",
            fix=("restart `mship serve --relay` so it re-publishes its public "
                 "URL into the runtime record"),
            facts=facts,
        )
        return [serve, relay]

    token = _serve_token(workspace_root, env=env)
    result = probe(record.url, token or "", timeout=timeout)
    if result.ok:
        relay = Edge(
            kind="relay", name="relay", status="ok", code=RELAY_OK,
            detail=f"{record.url} reachable", fix=None, facts=facts,
        )
    elif result.status_code in (401, 403):
        relay = Edge(
            kind="relay", name="relay", status="fail", code=RELAY_AUTH_FAILED,
            detail=(f"{record.url} reachable but rejected this host's bearer "
                    f"(HTTP {result.status_code})"),
            fix=("the tunnel is up but the token does not match — restart "
                 "`mship serve --relay` and re-pair the phone"),
            facts=facts,
        )
    else:
        why = result.error or f"HTTP {result.status_code}"
        relay = Edge(
            kind="relay", name="relay", status="fail", code=RELAY_UNREACHABLE,
            detail=f"{record.url} unreachable ({why})",
            fix=("check `mship serve --relay` is running here and the relay host "
                 "is up; an orphaned ssh tunnel duplicating this subdomain also "
                 "presents as unreachable — kill it and restart serve"),
            facts=facts,
        )
    return [serve, relay]



def _run_host_edges(
    *, config, state_dir, env, probe, skip_network, timeout,
) -> list[Edge]:
    """One edge per role — declared, mapped, reachable — plus aggregate edges
    for "nothing declared" and "a bare --remote would be ambiguous".

    Fix hints intentionally mirror `run_host.store.RunHostError` and
    `remote_client._http_status_message`, so the CLI error an operator hits and
    the topology hint they read say the same thing.
    """
    from mship.core.run_host.store import RunHostStore, _env_key

    declared = list(getattr(config, "run_hosts", ()) or ())
    repos = getattr(config, "repos", {}) or {}
    store = RunHostStore(state_dir)
    edges: list[Edge] = []
    # Private read: `redacted_list()` drops the token entirely, but this edge
    # must report `token_configured` (a boolean) AND whether the effective value
    # came from the file or an env override — neither is derivable from it.
    #
    # A hand-edited run-hosts.yaml is a realistic broken input, and `_read_all`
    # does not guard it: malformed YAML raises, and a non-mapping document
    # returns a str whose `.get` would blow up. Neither may break a report whose
    # whole job is to run when things are broken.
    store_error: str | None = None
    try:
        raw = store._read_all()
        mapped = dict(raw) if isinstance(raw, dict) else {}
        if not isinstance(raw, dict):
            store_error = "run-hosts.yaml is not a mapping of role -> {url, token}"
    except Exception as exc:
        mapped, store_error = {}, str(exc).splitlines()[0][:200]

    if store_error is not None:
        return [Edge(
            kind="run_host", name="run_hosts", status="warn",
            code=RUN_HOSTS_STORE_UNREADABLE,
            detail=f"could not read the run-host store ({store_error})",
            fix=(f"fix or remove `run-hosts.yaml` in {state_dir}, then re-map "
                 f"roles with `mship run-host add <role>`"),
            facts={"declared": declared, "store_path": str(state_dir / "run-hosts.yaml")},
        )]

    repo_defaults = {
        name: getattr(r, "run_host", None)
        for name, r in repos.items() if getattr(r, "run_host", None)
    }

    # --- aggregate: nothing declared / ambiguous bare --remote -------------
    if not declared:
        edges.append(Edge(
            kind="run_host", name="run_hosts", status="absent",
            code=RUN_HOSTS_NONE_DECLARED,
            detail="no run_hosts declared in mothership.yaml",
            fix=("add a `run_hosts:` list of role names to mothership.yaml "
                 "before using --remote"),
            facts={"declared": []},
        ))
    elif len(declared) > 1 and not repo_defaults:
        edges.append(Edge(
            kind="run_host", name="run_hosts", status="warn",
            code=RUN_HOSTS_AMBIGUOUS_DEFAULT,
            detail=(f"{len(declared)} roles declared ({', '.join(declared)}) and no "
                    f"repo declares a default, so a bare `--remote` is ambiguous"),
            fix=("pass --remote=<role> explicitly, or declare `run_host: <role>` "
                 "on the repo"),
            facts={"declared": declared},
        ))
    else:
        edges.append(Edge(
            kind="run_host", name="run_hosts", status="ok", code=RUN_HOSTS_OK,
            detail=f"{len(declared)} role(s) declared", fix=None,
            facts={"declared": declared, "repo_defaults": repo_defaults},
        ))

    # --- unknown roles: a repo points at a role that isn't declared --------
    for repo_name, role in sorted(repo_defaults.items()):
        if role in declared:
            continue
        edges.append(Edge(
            kind="run_host", name=f"run_host:{role}", status="fail",
            code=RUN_HOST_UNKNOWN_ROLE,
            detail=(f"repo {repo_name!r} declares run_host {role!r}, which is not "
                    f"in this workspace's run_hosts list"),
            fix=(f"add {role!r} to `run_hosts:` in mothership.yaml, or fix the "
                 f"typo on repo {repo_name!r}"),
            # `role` is included so a consumer can name it (the console fills
            # it into the remediation snippet); without it the card would render
            # a literal `{role}` placeholder.
            facts={"role": role, "declared_by_repo": repo_name, "known_roles": declared},
        ))

    # --- per declared role -------------------------------------------------
    for role in declared:
        entry = mapped.get(role, {})
        url_env, token_env = _env_key(role, "URL"), _env_key(role, "TOKEN")
        url = env.get(url_env) or entry.get("url")
        token = env.get(token_env) or entry.get("token")
        facts = {
            "role": role,
            "url": url,
            "url_source": (f"env:{url_env}" if env.get(url_env)
                           else "file" if entry.get("url") else None),
            "token_configured": bool(token),
            "token_source": (f"env:{token_env}" if env.get(token_env)
                             else "file" if entry.get("token") else None),
        }

        if not url or not token:
            edges.append(Edge(
                kind="run_host", name=f"run_host:{role}", status="fail",
                code=RUN_HOST_UNMAPPED,
                detail=(f"role {role!r} is declared but has no connection mapped "
                        f"on this machine"),
                fix=(f"run `mship run-host add {role} --pair-link '...'` (get the "
                     f"link by running `mship pair` on that machine)"),
                facts=facts,
            ))
            continue

        if skip_network:
            edges.append(Edge(
                kind="run_host", name=f"run_host:{role}", status="warn",
                code=PROBE_SKIPPED,
                detail="network probes skipped; reachability unknown",
                fix="re-run without --no-network to probe this run host",
                facts=facts,
            ))
            continue

        result = probe(url, token, timeout=timeout)
        if result.ok:
            edges.append(Edge(
                kind="run_host", name=f"run_host:{role}", status="ok",
                code=RUN_HOST_OK, detail=f"{url} reachable", fix=None, facts=facts,
            ))
        elif result.status_code == 503:
            edges.append(Edge(
                kind="run_host", name=f"run_host:{role}", status="fail",
                code=RUN_HOST_NOT_BOOTSTRAPPED,
                detail=f"{url} is reachable but has no workspace wired in (503)",
                fix=("bootstrap that machine as an mship workspace and restart "
                     "`mship serve --relay` there"),
                facts=facts,
            ))
        elif result.status_code in (401, 403):
            edges.append(Edge(
                kind="run_host", name=f"run_host:{role}", status="fail",
                code=RUN_HOST_STALE_TOKEN,
                detail=(f"{url} rejected the mapped bearer token "
                        f"(HTTP {result.status_code})"),
                fix=(f"the mapping is stale — re-run `mship run-host add {role}` "
                     f"with a fresh pair link/token"),
                facts=facts,
            ))
        else:
            why = result.error or f"HTTP {result.status_code}"
            edges.append(Edge(
                kind="run_host", name=f"run_host:{role}", status="fail",
                code=RUN_HOST_UNREACHABLE,
                detail=f"{url} unreachable ({why})",
                fix=(f"confirm that machine is up with `mship serve --relay` "
                     f"running; re-pair if its relay subdomain changed"),
                facts=facts,
            ))

    # --- orphan mappings: mapped here, not declared anywhere ---------------
    for role in sorted(set(mapped) - set(declared)):
        edges.append(Edge(
            kind="run_host", name=f"run_host:{role}", status="warn",
            code=RUN_HOST_ORPHAN_MAPPING,
            detail=(f"role {role!r} is mapped on this machine but not declared in "
                    f"mothership.yaml, so nothing can select it"),
            fix=(f"add {role!r} to `run_hosts:` in mothership.yaml, or drop the "
                 f"mapping with `mship run-host remove {role}`"),
            facts={"role": role},
        ))

    return edges



_GH_AUTH_CODES = {
    "relay_attach": GH_AUTH_RELAY_ATTACH,
    "env_token": GH_AUTH_ENV_TOKEN,
    "broker": GH_AUTH_BROKER,
    "none": GH_AUTH_NONE,
}


def _gh_auth_edge(*, env) -> Edge:
    """How THIS machine authenticates to GitHub, plus whether it also serves
    App-backed tokens to others.

    Those are two different questions, and conflating them misreports the first.
    A GitHub App configured here is consumed ONLY by `GET /gh-token` to mint
    tokens for CALLERS (Broker B) — this host's own git operations still go
    through `gh_auth.resolve_token`. So with GH_TOKEN set alongside App creds,
    the model in effect is the env token, and the App is reported as the serve
    capability it actually is (`serves_app_backed_tokens`).

    Reports presence/readability, never a credential — and never the App id,
    which identifies a specific private key.
    """
    from pathlib import Path

    from mship.core.gh_auth import classify_gh_auth

    app_id = env.get("MSHIP_GH_APP_ID") or None
    app_key_path = env.get("MSHIP_GH_APP_KEY") or None
    app_key_readable = bool(app_key_path) and Path(app_key_path).is_file()
    serves_app_tokens = bool(app_id and app_key_readable)
    broker_url = env.get("MSHIP_GH_BROKER_URL") or None
    relay_url = env.get("MSHIP_RELAY_URL") or None
    run_token = env.get("MSHIP_RUN_TOKEN") or None
    explicit = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or None

    model = classify_gh_auth(
        relay_url=relay_url, run_token=run_token,
        explicit_token=explicit, broker_url=broker_url,
    )
    facts = {
        "model": model,
        "serves_app_backed_tokens": serves_app_tokens,
        "app_id_configured": bool(app_id),
        "app_key_readable": app_key_readable,
        "broker_url": broker_url,          # a URL, not a credential
        "relay_url": relay_url,
        "run_token_configured": bool(run_token),
        "env_token_configured": bool(explicit),
    }
    app_note = (
        " This host also serves App-backed tokens via GET /gh-token."
        if serves_app_tokens else ""
    )

    # A half-configured App is a real misconfiguration: serve refuses to start
    # rather than silently pushing as a different identity, so say so plainly.
    if app_id and not app_key_readable:
        return Edge(
            kind="gh_auth", name="gh_auth", status="fail",
            code=GH_AUTH_APP_KEY_UNREADABLE,
            detail="MSHIP_GH_APP_ID is set but MSHIP_GH_APP_KEY is not a readable file",
            fix=("fix the MSHIP_GH_APP_KEY path, or unset it to fall back to "
                 "`gh auth token` deliberately"),
            facts=facts,
        )

    if model == "none":
        return Edge(
            kind="gh_auth", name="gh_auth", status="warn", code=GH_AUTH_NONE,
            detail="no GitHub auth configured for this machine's own git operations." + app_note,
            fix=("set MSHIP_GH_BROKER_URL + MSHIP_SERVE_TOKEN to use a broker, "
                 "or GH_TOKEN/GITHUB_TOKEN for a direct token"),
            facts=facts,
        )
    return Edge(
        kind="gh_auth", name="gh_auth", status="ok", code=_GH_AUTH_CODES[model],
        detail=f"GitHub auth model in effect: {model}." + app_note,
        fix=None, facts=facts,
    )


def _egress_edge(*, shell) -> Edge:
    """Is this machine's git routed through a relay egress proxy?

    Detected by reading git's global `insteadOf` rewrites — the ones
    `relay.worker_config.relay_git_config_commands` installs — and recognizing
    them via `relay.contract.PREFIX_HOST`, so detection cannot drift from
    installation. Read-only (`git config --get-regexp` writes nothing).
    """
    from pathlib import Path

    from mship.core.relay.contract import PREFIX_HOST

    cmd = 'git config --global --get-regexp "^url\\..*\\.insteadof$"'
    try:
        result = shell.run(cmd, cwd=Path("."))
    except Exception as exc:
        return Edge(
            kind="egress", name="egress", status="warn", code=EGRESS_UNKNOWN,
            detail=f"could not read git global config ({exc})",
            fix="ensure `git` is installed and on PATH to report egress routing",
            facts={},
        )

    bases: set[str] = set()
    for line in (getattr(result, "stdout", "") or "").splitlines():
        key = line.strip().split(" ", 1)[0]
        if not key.startswith("url.") or not key.lower().endswith(".insteadof"):
            continue
        rewritten = key[len("url."):-len(".insteadof")]
        for prefix in PREFIX_HOST:
            if rewritten.endswith(prefix):
                bases.add(rewritten[: -len(prefix)])

    if not bases:
        return Edge(
            kind="egress", name="egress", status="absent", code=EGRESS_ABSENT,
            detail="git is not routed through a relay egress on this machine",
            fix=None,
            facts={"prefixes": sorted(PREFIX_HOST)},
        )
    base = sorted(bases)[0]
    return Edge(
        kind="egress", name="egress", status="ok", code=EGRESS_ROUTED,
        detail=f"git is routed through {base}", fix=None,
        facts={"relay_base": base, "prefixes": sorted(PREFIX_HOST)},
    )


def probe_topology(
    *,
    config,
    state_dir,
    workspace_root,
    home=None,
    env=None,
    probe=None,
    pid_alive=None,
    shell=None,
    now=None,
    skip_network: bool = False,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Topology:
    """This machine's connectivity topology. Read-only; never raises.

    Every collaborator is injectable so the suite needs no live network, no real
    home directory, and no running serve:
      `probe`     -> `relay.health.probe_health`
      `pid_alive` -> `relay.runtime._pid_alive`
      `shell`     -> `util.shell.ShellRunner`
      `now`       -> UTC ISO-8601 clock
      `env`       -> `os.environ`

    `skip_network=True` reports config-level state only (used by `mship doctor
    --no-network` so a previously-fast command stays fast).
    """
    import os
    from pathlib import Path

    from mship.core.relay.runtime import _pid_alive

    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    probe = _default_probe if probe is None else probe
    pid_alive = _pid_alive if pid_alive is None else pid_alive
    now = _utc_now_iso if now is None else now
    if shell is None:
        from mship.util.shell import ShellRunner
        shell = ShellRunner()

    edges: list[Edge] = []
    edges.extend(_serve_and_relay_edges(
        config=config, workspace_root=Path(workspace_root), home=home, env=env,
        probe=probe, pid_alive=pid_alive, skip_network=skip_network,
        timeout=timeout,
    ))
    edges.extend(_run_host_edges(
        config=config, state_dir=Path(state_dir), env=env, probe=probe,
        skip_network=skip_network, timeout=timeout,
    ))
    edges.append(_gh_auth_edge(env=env))
    edges.append(_egress_edge(shell=shell))
    return Topology(
        version=SCHEMA_VERSION,
        workspace=getattr(config, "workspace", "") or "",
        probed_at=now(),
        edges=edges,
    )
