"""Gitignored role->connection store + `--remote[=role]` resolution.

Two-layer run-host model (see `mship.core.config` for the public layer):
`mothership.yaml` declares only logical role *names* (`run_hosts: [...]`,
optionally opted into per-repo via `RepoConfig.run_host`). This module owns
the private layer: `RunHostStore` persists each role's concrete
`{url, token}` in the gitignored `<state_dir>/run-hosts.yaml` (state_dir is
the `.mothership` dir itself — see `mship.core.state.StateManager` for the
same anchoring), and `resolve_run_host` picks the connection for a given
invocation.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.run_host.config import RunHostConnection


class RunHostError(Exception):
    """Actionable failure resolving a run-host role to a connection.

    Raised by `resolve_run_host` for an ambiguous role, an unknown role (not
    declared in `config.run_hosts`), or a role that's declared but has no
    connection mapped in the store yet.
    """


def _env_key(role: str, field: str) -> str:
    """`MSHIP_RUN_HOST_<ROLE>_<FIELD>`; role upper-cased, `-` -> `_`."""
    normalized = role.upper().replace("-", "_")
    return f"MSHIP_RUN_HOST_{normalized}_{field}"


class RunHostStore:
    """Filesystem-backed `{role: {url, token}}` map at
    `<state_dir>/run-hosts.yaml`.

    `state_dir` is the `.mothership` directory itself (the file is *not*
    nested one level deeper under another `.mothership/`), matching how
    `StateManager` and `InboxLease` anchor their files — see
    `mship.cli._resolve_state_dir` for how that directory is located.

    Per-role env overrides win over the file, mirroring
    `mship.core.relay.token.ensure_serve_token`'s env>file precedence:
    `MSHIP_RUN_HOST_<ROLE>_URL` / `_TOKEN` (role upper-cased, `-` -> `_`).
    """

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / "run-hosts.yaml"

    def _read_all(self) -> dict[str, dict[str, str]]:
        if not self._path.exists():
            return {}
        raw = yaml.safe_load(self._path.read_text())
        return raw or {}

    def _write_all(self, data: dict[str, dict[str, str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(data, sort_keys=True)
        # Create the tmp file 0600 FROM THE START (os.open with mode 0o600),
        # not under the process umask (typically 0644) then chmod'd afterward —
        # otherwise the token sits world-readable for the window between write
        # and chmod. `os.open` applies the mode subject to umask, so we also
        # chmod the final file to guarantee 0600 even under an odd umask.
        tmp = self._path.with_suffix(".yaml.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
        except BaseException:
            # Don't leave a partial tmp behind if the write fails.
            tmp.unlink(missing_ok=True)
            raise
        os.chmod(tmp, 0o600)  # belt-and-suspenders vs. a permissive umask
        tmp.replace(self._path)

    def get(self, role: str) -> RunHostConnection | None:
        """The connection for `role`, or None if neither the file nor the
        env overrides supply both a url and a token."""
        entry = self._read_all().get(role, {})
        url = os.environ.get(_env_key(role, "URL")) or entry.get("url")
        token = os.environ.get(_env_key(role, "TOKEN")) or entry.get("token")
        if not url or not token:
            return None
        return RunHostConnection(url=url, token=token)

    def set(self, role: str, conn: RunHostConnection) -> None:
        data = self._read_all()
        data[role] = {"url": conn.url, "token": conn.token}
        self._write_all(data)

    def remove(self, role: str) -> None:
        data = self._read_all()
        if role in data:
            del data[role]
            self._write_all(data)

    def redacted_list(self) -> list[tuple[str, str]]:
        """`(role, url)` for every role mapped in the file, role-sorted.
        Tokens are never returned by this method."""
        return sorted((role, entry.get("url", "")) for role, entry in self._read_all().items())


def resolve_run_host(
    role: str | None,
    *,
    repo: RepoConfig | None,
    config: WorkspaceConfig,
    store: RunHostStore,
) -> RunHostConnection:
    """Pick the run-host connection for a `--remote[=role]` invocation.

    Precedence (most specific wins):
        1. explicit `role` (an operator-supplied `--remote=<role>`)
        2. `repo.run_host` (the repo's declared default role)
        3. the sole entry in `config.run_hosts`, if there is exactly one

    Raises `RunHostError` with an actionable message when:
        - no role resolves and `config.run_hosts` is empty (nothing declared)
        - no role resolves and `config.run_hosts` has 2+ entries (ambiguous;
          message asks for an explicit `--remote=<role>`)
        - the resolved role isn't in `config.run_hosts` (unknown role, e.g. a
          typo in `repo.run_host` or an explicit `--remote`)
        - the resolved role is declared but `store.get(role)` is None (names
          `mship run-host add <role>` as the fix)
    """
    known = config.run_hosts

    if role is not None:
        chosen = role
    elif repo is not None and repo.run_host:
        chosen = repo.run_host
    elif len(known) == 1:
        chosen = known[0]
    elif not known:
        raise RunHostError(
            "no run_hosts declared in mothership.yaml; add a `run_hosts:` "
            "list of role names before using --remote"
        )
    else:
        raise RunHostError(
            f"ambiguous run-host: multiple roles are configured "
            f"({', '.join(known)}) and none was specified; pass "
            f"--remote=<role> to pick one"
        )

    if chosen not in known:
        raise RunHostError(
            f"unknown run-host role {chosen!r}; not declared in this "
            f"workspace's `run_hosts:` list. Declared roles: "
            f"{sorted(known)}"
        )

    conn = store.get(chosen)
    if conn is None:
        raise RunHostError(
            f"run-host role {chosen!r} is declared but has no connection "
            f"mapped on this machine; run `mship run-host add {chosen}` to "
            f"map it to a {{url, token}}"
        )
    return conn
