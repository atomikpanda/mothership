"""`mship commit <msg>` — post-finish patch workflow.

Iterates `task.affected_repos` and commits staged changes in each worktree
that has them. Post-finish: also pushes to the existing PR. Always journals
one entry per repo committed. See #29.
"""
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command()
    def commit(
        message: str = typer.Argument(..., help="Commit message (same for every repo with staged changes)"),
        task: Optional[str] = typer.Option(
            None, "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var.",
        ),
    ):
        """Commit staged changes across task worktrees; push if finished."""
        import shlex

        container = get_container()
        output = Output()
        state_mgr = container.state_manager()
        state = state_mgr.load()

        resolved = resolve_for_command("commit", state, task, output)
        t = resolved.task

        shell = container.shell()
        log_mgr = container.log_manager()

        results: list[dict] = []
        skipped: list[str] = []

        for repo_name in t.affected_repos:
            wt = t.worktrees.get(repo_name)
            if wt is None:
                skipped.append(repo_name)
                continue
            wt_path = Path(wt)
            if not wt_path.is_dir():
                skipped.append(repo_name)
                continue

            staged_check = shell.run("git diff --cached --quiet", cwd=wt_path)
            if staged_check.returncode == 0:
                skipped.append(repo_name)
                continue

            commit_r = shell.run(
                f"git commit -m {shlex.quote(message)}", cwd=wt_path,
            )
            if commit_r.returncode != 0:
                output.error(
                    f"{repo_name}: git commit failed — {commit_r.stderr.strip() or 'unknown error'}"
                )
                raise typer.Exit(code=1)

            sha_r = shell.run("git rev-parse HEAD", cwd=wt_path)
            sha = sha_r.stdout.strip() if sha_r.returncode == 0 else ""

            log_mgr.append(
                t.slug, message, repo=repo_name, action="committed",
            )

            pushed = False
            pr_url = t.pr_urls.get(repo_name)
            if t.finished_at is not None and pr_url:
                push_r = shell.run("git push", cwd=wt_path)
                if push_r.returncode != 0:
                    output.error(
                        f"{repo_name}: git push failed — {push_r.stderr.strip() or 'unknown error'}"
                    )
                    raise typer.Exit(code=1)
                pushed = True

            results.append({
                "repo": repo_name,
                "commit_sha": sha,
                "pushed": pushed,
                "pr_url": pr_url if pushed else None,
            })

        if not results:
            output.error(
                "nothing staged in any affected repo. "
                "Run `git add <files>` first."
            )
            raise typer.Exit(code=1)

        if output.is_tty:
            for r in results:
                short = r["commit_sha"][:8] if r["commit_sha"] else "(no sha)"
                base = f"  {r['repo']}: committed {short}"
                if r["pushed"]:
                    output.print(base + f" → pushed to {r['pr_url']}")
                else:
                    output.print(base + " (not pushed — task not finished)")
            for s in skipped:
                output.print(f"  {s}: skipped (nothing staged)")
        else:
            output.json({
                "task": t.slug,
                "repos": [
                    *results,
                    *[{"repo": s, "skipped": "nothing staged"} for s in skipped],
                ],
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })
