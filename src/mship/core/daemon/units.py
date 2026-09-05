"""Supervisor unit rendering + mshipd exec resolution.

Exec resolution is sibling-first: `Path(sys.executable).parent / "mshipd"`
(same venv bin dir ⇒ provably the same installed distribution — exactly the uv
tool-install layout), else a `which("mshipd")` VERIFIED to live under the
invoking CLI's `sys.prefix`, else `<sys.executable> -m mship.core.daemon`.

A which() hit OUTSIDE sys.prefix (stale uv-tool shim while running `uv run
mship` from a checkout — the documented worktree workflow) refuses: baking a
worktree venv's python into a persistent unit would make `uv tool install
--force` + `mship daemon restart` (the deploy step) silently deploy nothing.

Heredoc-style unit constants follow the repo's one unit-generation precedent
(`scripts/relay-bootstrap.sh`).
"""
from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path
from typing import Callable

SYSTEMD_UNIT_NAME = "mship-daemon.service"
LAUNCHD_LABEL = "com.mothership.daemon"


class DaemonExecResolutionError(RuntimeError):
    pass


def _running_from_dev_tree() -> str | None:
    """A checkout's own venv must never be persisted into a supervisor unit —
    upgrades (`uv tool install --force` + restart) would silently deploy
    nothing, or break when the worktree is removed. Two signals, either
    refuses:

    - the imported mship package resolves OUTSIDE sys.prefix (editable
      install — exactly what `uv run mship` from a checkout produces);
    - the venv sits beside a pyproject.toml declaring this same project
      (a non-editable `.venv` inside the checkout).
    """
    import mship

    prefix = Path(sys.prefix).resolve()
    try:
        pkg = Path(mship.__file__).resolve()
    except (TypeError, OSError):
        return None
    if not pkg.is_relative_to(prefix):
        return f"mship is imported from {pkg.parent} (editable checkout), not the running venv"
    project = prefix.parent / "pyproject.toml"
    try:
        if project.is_file():
            import tomllib

            data = tomllib.loads(project.read_text(encoding="utf-8"))
            # Parsed, not substring-matched: `name='mothership'`, extra spaces,
            # or any other valid TOML spelling must be detected too, or a
            # checkout venv gets baked into the unit and upgrades deploy nothing.
            if (data.get("project") or {}).get("name") == "mothership":
                return f"the running venv sits inside a mothership checkout ({prefix.parent})"
    except (OSError, ValueError):
        pass
    return None


def resolve_mshipd_argv(which: Callable[[str], str | None] = shutil.which) -> list[str]:
    dev_reason = _running_from_dev_tree()
    if dev_reason is not None:
        raise DaemonExecResolutionError(
            f"{dev_reason} — you are running mship from a dev tree; install the tool "
            "first (uv tool install --force <path>) and re-run mship daemon install."
        )
    exe = Path(sys.executable)
    sibling = exe.parent / "mshipd"
    if sibling.exists():
        return [str(sibling)]
    found = which("mshipd")
    if found is not None:
        found_path = Path(found)
        try:
            inside_prefix = found_path.resolve().is_relative_to(Path(sys.prefix).resolve())
        except OSError:
            inside_prefix = False
        if inside_prefix:
            return [str(found_path)]
        raise DaemonExecResolutionError(
            f"mshipd on PATH ({found}) belongs to a different installation than this "
            f"mship CLI ({sys.prefix}) — you are likely running mship from a dev tree; "
            "install the tool first (uv tool install --force <path>) and re-run "
            "mship daemon install."
        )
    return [str(exe), "-m", "mship.core.daemon"]


def render_systemd_unit(argv: list[str]) -> str:
    exec_start = shlex.join(argv)
    # StartLimit* live in [Unit]; Restart*/ExecStart in [Service]. Section
    # placement is load-bearing — systemd silently ignores misplaced directives.
    return f"""\
[Unit]
Description=Mothership daemon (mshipd) — host control plane
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def render_launchd_plist(argv: list[str], log_dir: Path) -> str:
    # plistlib, not string templating: argv/log paths land in XML, and a path
    # containing &, <, > would otherwise render a plist launchctl rejects.
    import plistlib

    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": list(argv),
            # Match user/<uid>; the default Aqua session requires gui/<uid>.
            "LimitLoadToSessionType": "Background",
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 5,
            "StandardOutPath": str(log_dir / "launchd.out.log"),
            "StandardErrorPath": str(log_dir / "launchd.err.log"),
        },
        sort_keys=False,
    ).decode()


def systemd_unit_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME


def launchd_plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
