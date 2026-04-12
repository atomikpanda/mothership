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
AVAILABLE_SKILLS = ["working-with-mothership"]


def register(app: typer.Typer, get_container):
    @app.command(name="skill")
    def skill_cmd(
        action: str = typer.Argument(help="Action: install, list"),
        name: Optional[str] = typer.Argument(None, help="Skill name (e.g. working-with-mothership)"),
        dest: Optional[str] = typer.Option(None, "--dest", help="Destination directory (default: ~/.agents/skills/)"),
    ):
        """Manage mothership skills."""
        output = Output()

        if action == "list":
            _list_skills(output)
        elif action == "install":
            _install_skill(output, name, dest)
        else:
            output.error(f"Unknown action: {action}. Use 'install' or 'list'.")
            raise typer.Exit(code=1)


def _fetch_skill_content(name: str, output: Output) -> str:
    """Fetch SKILL.md content from GitHub. Try raw.githubusercontent.com first,
    fall back to `gh` CLI for private repos."""
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/skills/{name}/SKILL.md"

    # Try direct fetch first (works for public repos)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Could be a private repo — fall through to gh CLI
            pass
        else:
            output.error(f"Could not fetch skill '{name}': HTTP {e.code}")
            raise typer.Exit(code=1)
    except (urllib.error.URLError, OSError) as e:
        output.error(f"Could not fetch skill '{name}': {e}")
        output.error("Check your network connection or try again later.")
        raise typer.Exit(code=1)

    # Fallback: use gh CLI (works for private repos with gh auth)
    if not shutil.which("gh"):
        output.error(
            f"Could not fetch skill '{name}' from public URL and `gh` CLI is not installed. "
            f"Install gh (https://cli.github.com) or ensure the repo is public."
        )
        raise typer.Exit(code=1)

    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{GITHUB_REPO}/contents/skills/{name}/SKILL.md",
                "--jq", ".content",
                "-H", "Accept: application/vnd.github.v3+json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as e:
        output.error(f"Could not fetch skill '{name}' via gh CLI: {e}")
        raise typer.Exit(code=1)

    if result.returncode != 0:
        output.error(f"Could not fetch skill '{name}' via gh CLI: {result.stderr.strip()}")
        output.error("Is `gh` authenticated? Try `gh auth login`.")
        raise typer.Exit(code=1)

    # GitHub API returns base64-encoded content
    import base64
    try:
        return base64.b64decode(result.stdout.strip()).decode("utf-8")
    except Exception as e:
        output.error(f"Could not decode skill content: {e}")
        raise typer.Exit(code=1)


def _list_skills(output: Output):
    """List available skills."""
    if output.is_tty:
        output.print("[bold]Available skills:[/bold]")
        for name in AVAILABLE_SKILLS:
            output.print(f"  {name}")
        output.print(f"\nInstall with: mship skill install <name>")
    else:
        output.json({"skills": AVAILABLE_SKILLS})


def _install_skill(output: Output, name: str | None, dest: str | None):
    """Fetch a skill from GitHub and install it."""
    if name is None:
        output.error("Skill name required. Run `mship skill list` to see available skills.")
        raise typer.Exit(code=1)

    if name not in AVAILABLE_SKILLS:
        output.error(f"Unknown skill: {name}. Run `mship skill list` to see available skills.")
        raise typer.Exit(code=1)

    content = _fetch_skill_content(name, output)

    if dest:
        dest_dir = Path(dest) / name
    else:
        dest_dir = Path.home() / ".agents" / "skills" / name

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / "SKILL.md"
    dest_file.write_text(content)

    if output.is_tty:
        output.success(f"Installed: {name}")
        output.print(f"  Location: {dest_dir}")
    else:
        output.json({"skill": name, "path": str(dest_dir)})
