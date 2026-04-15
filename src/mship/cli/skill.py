"""`mship skill` — discover and install vendored skill bundles from GitHub.

Each installable skill lives under `skills/<name>/` in the repo and may contain
SKILL.md plus auxiliary files (references, prompt templates, examples). The
installer fetches the full subtree, preserving layout, to
`~/.agents/skills/<name>/` by default.

`list` reads the live remote tree — no hardcoded list to drift.
`install --all` installs every discovered skill in one shot.
"""
from __future__ import annotations

import base64
import json
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


def register(app: typer.Typer, get_container):
    @app.command(name="skill")
    def skill_cmd(
        action: str = typer.Argument(help="Action: install, list"),
        name: Optional[str] = typer.Argument(None, help="Skill name (omit with --all)"),
        dest: Optional[str] = typer.Option(None, "--dest", help="Destination dir (default: ~/.agents/skills/)"),
        install_all: bool = typer.Option(False, "--all", help="Install every available skill"),
    ):
        """Manage mothership skills."""
        output = Output()

        if action == "list":
            _list_skills(output)
            return
        if action == "install":
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
    installed: list[dict] = []
    for skill_name in targets:
        files = _skill_files(tree, skill_name)
        skill_dest = base_dest / skill_name
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
