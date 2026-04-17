"""`mship skill` — list and install mship-bundled skills into agent dirs.

Skills ship as package data under src/mship/skills/. The CLI is a thin
wrapper around `mship.core.skill_install`, imported as a module so test
monkey-patches at `mship.core.skill_install.X` take effect here.

See docs/superpowers/specs/2026-04-17-claude-skill-install-discoverability-design.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output
from mship.core import skill_install as _si
from mship.core.skill_install import AgentInstallResult


SUPPORTED_AGENTS = ("claude", "codex", "gemini")


_INSTALLERS = {
    "claude": _si.install_for_claude,
    "codex":  _si.install_for_codex,
    # gemini is not bundled — it has its own `gemini extensions install` flow,
    # which lives outside mship's symlink model. Print guidance instead.
}


def _legacy_codex_mothership_warning(output: Output) -> None:
    legacy = Path.home() / ".codex" / "mothership"
    if legacy.exists():
        output.warning(
            f"old skills source `{legacy}` no longer used; "
            "safe to `rm -rf` it"
        )


def register(app: typer.Typer, get_container):
    @app.command(name="skill")
    def skill_cmd(
        action: str = typer.Argument(help="Action: install | list"),
        only: Optional[str] = typer.Option(None, "--only", help="Comma-separated agents (claude,codex,gemini)"),
        force: bool = typer.Option(False, "--force", help="Override safe-skip on foreign content"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip per-agent confirmation prompts"),
    ):
        """Install or list mship-bundled skills."""
        output = Output()
        if action == "list":
            _list(output)
            return
        if action == "install":
            _install(output, only=only, force=force, yes=yes)
            return
        output.error(f"Unknown action: {action}. Use 'install' or 'list'.")
        raise typer.Exit(code=1)


def _list(output: Output) -> None:
    skills = [d.name for d in _si._iter_skill_dirs(_si.pkg_skills_source())]
    output.json({"skills": skills})


def _install(output: Output, *, only: Optional[str], force: bool, yes: bool) -> None:
    detected = _si._detect_agents()
    if only:
        wanted = {a.strip() for a in only.split(",") if a.strip()}
        unknown = wanted - set(SUPPORTED_AGENTS)
        if unknown:
            output.error(f"Unknown agent(s): {', '.join(sorted(unknown))}")
            raise typer.Exit(code=1)
        targets = sorted(wanted)
    else:
        targets = sorted(a for a, present in detected.items() if present)

    _legacy_codex_mothership_warning(output)

    results: list[AgentInstallResult] = []
    for agent in targets:
        installer = _INSTALLERS.get(agent)
        if installer is None:
            output.print(f"  {agent}: bundled install not supported (use the agent's native flow)")
            continue
        results.append(installer(force=force))

    if output.is_tty:
        for r in results:
            extras = []
            if r.replaced:
                extras.append(f"{len(r.replaced)} refreshed")
            if r.skipped:
                extras.append(f"{len(r.skipped)} skipped (use --force)")
            tail = f" ({', '.join(extras)})" if extras else ""
            output.print(f"  {r.agent}: {r.count} skills installed → {r.dest}{tail}")
    else:
        output.json({"installed": [
            {"agent": r.agent, "dest": str(r.dest), "count": r.count,
             "skipped": r.skipped, "replaced": r.replaced}
            for r in results
        ]})
