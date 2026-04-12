import shutil
from importlib import resources
from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(name="skill")
    def skill_cmd(
        action: str = typer.Argument(help="Action: install, list"),
        name: Optional[str] = typer.Argument(None, help="Skill name (e.g. working-with-mothership)"),
        dest: Optional[str] = typer.Option(None, "--dest", help="Destination directory (default: ~/.claude/skills/)"),
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


def _list_skills(output: Output):
    """List available bundled skills."""
    skills_pkg = resources.files("mship.bundled_skills")
    skill_names = []
    for item in skills_pkg.iterdir():
        if item.is_dir() and (item / "SKILL.md").is_file():
            skill_names.append(item.name)

    if not skill_names:
        output.print("No bundled skills found")
        return

    if output.is_tty:
        output.print("[bold]Available skills:[/bold]")
        for name in sorted(skill_names):
            output.print(f"  {name}")
        output.print(f"\nInstall with: mship skill install <name>")
    else:
        output.json({"skills": sorted(skill_names)})


def _install_skill(output: Output, name: str | None, dest: str | None):
    """Install a bundled skill to the user's skills directory."""
    if name is None:
        output.error("Skill name required. Run `mship skill list` to see available skills.")
        raise typer.Exit(code=1)

    # Find the bundled skill
    skills_pkg = resources.files("mship.bundled_skills")
    skill_dir = skills_pkg / name
    skill_file = skill_dir / "SKILL.md"

    if not skill_file.is_file():
        output.error(f"Unknown skill: {name}. Run `mship skill list` to see available skills.")
        raise typer.Exit(code=1)

    # Determine destination
    if dest:
        dest_dir = Path(dest) / name
    else:
        dest_dir = Path.home() / ".claude" / "skills" / name

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / "SKILL.md"

    # Copy the skill file
    content = skill_file.read_text()
    dest_file.write_text(content)

    if output.is_tty:
        output.success(f"Installed: {name}")
        output.print(f"  Location: {dest_dir}")
        output.print(f"\nAdd to your Claude Code settings if not auto-discovered:")
        output.print(f'  "skills": ["{dest_dir}"]')
    else:
        output.json({"skill": name, "path": str(dest_dir)})
