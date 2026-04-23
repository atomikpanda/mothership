"""`mship pr` — aggregate PR state across active tasks. See #41."""
import json
import shlex
from pathlib import Path
from typing import Optional

import typer

from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def pr():
        """Show PR state for every active task with recorded PR URLs."""
        container = get_container()
        output = Output()
        state = container.state_manager().load()
        shell = container.shell()

        tasks_with_prs: list[dict] = []

        for slug, task in sorted(state.tasks.items()):
            if not task.pr_urls:
                continue
            prs: list[dict] = []
            for repo_name, url in sorted(task.pr_urls.items()):
                info = _fetch_pr_info(shell, url)
                prs.append({"repo": repo_name, "url": url, **info})
            tasks_with_prs.append({"slug": slug, "prs": prs})

        if not tasks_with_prs:
            if output.is_tty:
                output.print("No active tasks with recorded PRs.")
            else:
                output.json({"tasks": []})
            return

        if output.is_tty:
            for t in tasks_with_prs:
                output.print(f"[bold]{t['slug']}[/bold]")
                for p in t["prs"]:
                    state_color = {
                        "open": "green",
                        "merged": "magenta",
                        "closed": "red",
                        "unknown": "yellow",
                    }.get(p["state"], "white")
                    num = f"#{p['number']}" if p["number"] else "?"
                    base = p.get("base") or "?"
                    output.print(
                        f"  {p['repo']}: {num} "
                        f"[{state_color}]{p['state']}[/{state_color}] "
                        f"base={base}  {p['url']}"
                    )
        else:
            output.json({"tasks": tasks_with_prs})

    def _fetch_pr_info(shell, url: str) -> dict:
        """Return {state, number, base, url}. Sets state='unknown' on any gh failure."""
        cmd = (
            f"gh pr view {shlex.quote(url)} "
            "--json state,number,baseRefName -q "
            "'[.state,.number,.baseRefName] | @tsv'"
        )
        r = shell.run(cmd, cwd=Path("."))
        if r.returncode != 0:
            return {"state": "unknown", "number": None, "base": None}
        parts = r.stdout.strip().split("\t")
        if len(parts) < 3:
            return {"state": "unknown", "number": None, "base": None}
        raw_state, num_str, base = parts[0], parts[1], parts[2]
        mapping = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}
        state = mapping.get(raw_state.upper(), "unknown")
        try:
            number = int(num_str)
        except (ValueError, TypeError):
            number = None
        return {"state": state, "number": number, "base": base}
