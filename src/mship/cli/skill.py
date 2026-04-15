"""`mship skill` — discover and install the mothership skill bundle from GitHub.

Each installable skill lives under `skills/<name>/` in the repo and may contain
SKILL.md plus auxiliary files (references, prompt templates, examples). The
installer fetches the full subtree, preserving layout, and places every skill
under a single `mothership/` namespace:

    ~/.agents/skills/mothership/<skill-name>/

This way the agent sees them as a bundle (e.g. `mothership:brainstorming`)
rather than flat at the top level where names would clash with other plugins.

For Claude Code, Gemini CLI, and Codex, prefer the platform-native install
(plugin marketplace, `gemini extensions install`, or the `.codex/INSTALL.md`
symlink recipe). This CLI is the universal fallback.

`list` reads the live remote tree — no hardcoded list to drift.
`install --all` installs every discovered skill in one shot (the whole bundle).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output


GITHUB_REPO = "atomikpanda/mothership"
GITHUB_BRANCH = "main"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"
SUPPORTED_AGENTS = ("claude", "gemini", "codex")


def register(app: typer.Typer, get_container):
    @app.command(name="skill")
    def skill_cmd(
        action: str = typer.Argument(help="Action: install, list"),
        name: Optional[str] = typer.Argument(None, help="Skill name (omit with --all or for agent auto-install)"),
        dest: Optional[str] = typer.Option(None, "--dest", help="Destination dir (default: ~/.agents/skills/)"),
        install_all: bool = typer.Option(False, "--all", help="Install every available skill (CLI fallback)"),
        only: Optional[str] = typer.Option(None, "--only", help="Comma-separated agents for auto-install: claude,gemini,codex"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    ):
        """Manage mothership skills."""
        output = Output()

        if action == "list":
            _list_skills(output)
            return
        if action == "install":
            if name is None and not install_all:
                _install_for_agents(output, only, yes)
                return
            if only:
                output.error("--only is only valid with the agent auto-install form (no <name>, no --all).")
                raise typer.Exit(code=1)
            _install(output, name, dest, install_all)
            return
        output.error(f"Unknown action: {action}. Use 'install' or 'list'.")
        raise typer.Exit(code=1)


# --- HTTP helpers ----------------------------------------------------------


def _fetch_json(url: str, output: Output) -> dict | list:
    """Fetch a JSON payload from GitHub API with `gh` CLI fallback for private repos."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 404):
            output.error(f"GitHub API error {e.code}: {url}")
            raise typer.Exit(code=1)
    except (urllib.error.URLError, OSError) as e:
        output.error(f"Network error fetching {url}: {e}")
        raise typer.Exit(code=1)

    # Private-repo fallback via `gh api`
    if not shutil.which("gh"):
        output.error(f"Could not fetch {url} and `gh` CLI is not installed.")
        raise typer.Exit(code=1)
    api_path = url.replace("https://api.github.com/", "")
    try:
        result = subprocess.run(
            ["gh", "api", api_path], capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        output.error(f"gh api failed: {e}")
        raise typer.Exit(code=1)
    if result.returncode != 0:
        output.error(f"gh api failed: {result.stderr.strip()}")
        raise typer.Exit(code=1)
    return json.loads(result.stdout)


def _fetch_blob(path: str, output: Output) -> bytes:
    """Fetch raw file bytes at `path` on the default branch."""
    raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{path}"
    try:
        with urllib.request.urlopen(raw_url, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            output.error(f"Fetch failed ({e.code}): {raw_url}")
            raise typer.Exit(code=1)
    except (urllib.error.URLError, OSError) as e:
        output.error(f"Network error fetching {raw_url}: {e}")
        raise typer.Exit(code=1)

    # Private-repo fallback
    if not shutil.which("gh"):
        output.error(f"Could not fetch {path} and `gh` CLI is not installed.")
        raise typer.Exit(code=1)
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}",
                "--jq", ".content",
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        output.error(f"gh api failed: {e}")
        raise typer.Exit(code=1)
    if result.returncode != 0:
        output.error(f"gh api failed: {result.stderr.strip()}")
        raise typer.Exit(code=1)
    return base64.b64decode(result.stdout.strip())


# --- Tree discovery --------------------------------------------------------


def _fetch_tree(output: Output) -> list[dict]:
    """Return the repo's recursive git-tree entries on the default branch."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/{GITHUB_BRANCH}?recursive=1"
    payload = _fetch_json(url, output)
    if isinstance(payload, dict) and payload.get("truncated"):
        output.print("[yellow]warning:[/yellow] repo tree truncated; some skills may be missing")
    tree = payload.get("tree", []) if isinstance(payload, dict) else []
    return [e for e in tree if isinstance(e, dict)]


def _available_skills(tree: list[dict]) -> list[str]:
    """Top-level skills/ subdirs that contain a SKILL.md file."""
    return sorted({
        e["path"].split("/")[1]
        for e in tree
        if e.get("type") == "blob"
        and e["path"].startswith("skills/")
        and e["path"].endswith("/SKILL.md")
        and e["path"].count("/") == 2  # skills/<name>/SKILL.md exactly
    })


def _skill_files(tree: list[dict], name: str) -> list[str]:
    """All blob paths under skills/<name>/, sorted for deterministic install."""
    prefix = f"skills/{name}/"
    return sorted(
        e["path"] for e in tree
        if e.get("type") == "blob" and e["path"].startswith(prefix)
    )


# --- Commands --------------------------------------------------------------


def _list_skills(output: Output) -> None:
    tree = _fetch_tree(output)
    skills = _available_skills(tree)
    if output.is_tty:
        output.print("[bold]Available skills:[/bold]")
        for n in skills:
            output.print(f"  {n}")
        output.print("")
        output.print("Install one: mship skill install <name>")
        output.print("Install all: mship skill install --all")
    else:
        output.json({"skills": skills})


def _install(
    output: Output, name: str | None, dest: str | None, install_all: bool,
) -> None:
    if install_all and name is not None:
        output.error("Pass either <name> OR --all, not both.")
        raise typer.Exit(code=1)
    if not install_all and name is None:
        output.error("Skill name required, or pass --all. Run `mship skill list`.")
        raise typer.Exit(code=1)

    tree = _fetch_tree(output)
    available = _available_skills(tree)
    if not available:
        output.error("No skills found on remote.")
        raise typer.Exit(code=1)

    if install_all:
        targets = available
    else:
        if name not in available:
            output.error(f"Unknown skill: {name}. Available: {', '.join(available)}.")
            raise typer.Exit(code=1)
        targets = [name]

    base_dest = Path(dest) if dest else (Path.home() / ".agents" / "skills")
    bundle_root = base_dest / "mothership"
    installed: list[dict] = []
    for skill_name in targets:
        files = _skill_files(tree, skill_name)
        skill_dest = bundle_root / skill_name
        for repo_path in files:
            # repo_path looks like "skills/<name>/<subpath>"
            subpath = Path(repo_path).relative_to(Path("skills") / skill_name)
            out_file = skill_dest / subpath
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(_fetch_blob(repo_path, output))
        installed.append({"skill": skill_name, "path": str(skill_dest), "files": len(files)})
        if output.is_tty:
            output.success(f"Installed: {skill_name} ({len(files)} files) → {skill_dest}")

    if not output.is_tty:
        output.json({"installed": installed})


# --- Agent auto-install ----------------------------------------------------


def _detect_agents() -> dict[str, bool]:
    """Best-effort detection: CLI on PATH or config dir in $HOME."""
    home = Path.home()
    return {
        "claude": shutil.which("claude") is not None or (home / ".claude").exists(),
        "gemini": shutil.which("gemini") is not None or (home / ".gemini").exists(),
        "codex": shutil.which("codex") is not None or (home / ".codex").exists(),
    }


def _install_claude() -> dict:
    """Claude Code has no shell-level plugin install. Emit the REPL slash commands."""
    return {
        "agent": "claude",
        "ok": True,
        "method": "manual",
        "instructions": [
            "/plugin marketplace add atomikpanda/mothership",
            "/plugin install mothership@mothership-marketplace",
        ],
    }


def _install_gemini() -> dict:
    if not shutil.which("gemini"):
        return {"agent": "gemini", "ok": False, "error": "`gemini` CLI not on PATH"}
    try:
        r = subprocess.run(
            ["gemini", "extensions", "install", GITHUB_URL],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return {"agent": "gemini", "ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"agent": "gemini", "ok": False, "error": (r.stderr or r.stdout).strip()}
    return {"agent": "gemini", "ok": True, "method": "gemini extensions install"}


def _install_codex() -> dict:
    """Clone repo to ~/.codex/mothership and symlink its skills/ dir under ~/.agents/skills/mothership."""
    clone_dir = Path.home() / ".codex" / "mothership"
    if clone_dir.exists():
        try:
            r = subprocess.run(
                ["git", "-C", str(clone_dir), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=120,
            )
        except (subprocess.SubprocessError, OSError) as e:
            return {"agent": "codex", "ok": False, "error": str(e)}
        if r.returncode != 0:
            return {"agent": "codex", "ok": False, "error": (r.stderr or r.stdout).strip()}
        method = "git pull + symlink"
    else:
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = subprocess.run(
                ["git", "clone", GITHUB_URL, str(clone_dir)],
                capture_output=True, text=True, timeout=300,
            )
        except (subprocess.SubprocessError, OSError) as e:
            return {"agent": "codex", "ok": False, "error": str(e)}
        if r.returncode != 0:
            return {"agent": "codex", "ok": False, "error": (r.stderr or r.stdout).strip()}
        method = "git clone + symlink"

    target = clone_dir / "skills"
    link = Path.home() / ".agents" / "skills" / "mothership"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if Path(os.readlink(link)) == target:
            return {"agent": "codex", "ok": True, "method": method, "path": str(link)}
        link.unlink()
    elif link.exists():
        return {
            "agent": "codex", "ok": False,
            "error": f"{link} exists and is not a symlink; remove it and retry",
        }
    link.symlink_to(target)
    return {"agent": "codex", "ok": True, "method": method, "path": str(link)}


_INSTALLERS = {
    "claude": _install_claude,
    "gemini": _install_gemini,
    "codex": _install_codex,
}

_PLAN_DESCRIPTIONS = {
    "claude": "Claude Code: add marketplace + install plugin (manual slash commands in REPL)",
    "gemini": "Gemini CLI: gemini extensions install",
    "codex": "Codex: git clone to ~/.codex/mothership + symlink to ~/.agents/skills/mothership",
}


def _install_for_agents(output: Output, only: str | None, yes: bool) -> None:
    detected = _detect_agents()

    if only:
        wanted = [a.strip() for a in only.split(",") if a.strip()]
        unknown = [a for a in wanted if a not in SUPPORTED_AGENTS]
        if unknown:
            output.error(
                f"Unknown agents: {', '.join(unknown)}. Supported: {', '.join(SUPPORTED_AGENTS)}."
            )
            raise typer.Exit(code=1)
        targets = wanted
    else:
        targets = [a for a in SUPPORTED_AGENTS if detected[a]]

    if not targets:
        output.error(
            "No supported AI agent tools detected on this system. "
            "Install Claude Code, Gemini CLI, or Codex — or use `mship skill install --all` as a fallback."
        )
        raise typer.Exit(code=1)

    if output.is_tty:
        output.print("[bold]Installing mothership for detected agents:[/bold]")
        for a in targets:
            output.print(f"  • {_PLAN_DESCRIPTIONS[a]}")
        if not yes and not typer.confirm("Proceed?", default=True):
            output.print("Aborted.")
            raise typer.Exit(code=0)

    results = [_INSTALLERS[a]() for a in targets]

    for r in results:
        if not output.is_tty:
            continue
        if not r["ok"]:
            output.error(f"{r['agent']}: {r.get('error', 'failed')}")
            continue
        if r.get("method") == "manual":
            output.print(f"[yellow]{r['agent']}:[/yellow] run these inside the Claude Code REPL:")
            for cmd in r.get("instructions", []):
                output.print(f"    {cmd}")
        else:
            extra = f" → {r['path']}" if r.get("path") else ""
            output.success(f"{r['agent']}: installed via {r['method']}{extra}")

    if not output.is_tty:
        output.json({"results": results})

    if any(not r["ok"] for r in results):
        raise typer.Exit(code=1)
