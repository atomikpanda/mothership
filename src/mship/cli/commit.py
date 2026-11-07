"""`mship commit <msg>` — post-finish patch workflow.

Iterates `task.affected_repos` and commits staged changes in each worktree
that has them. Post-finish, clean indexes with an existing PR push commits
that are ahead of their upstream. Each successful action is journaled. See #29.
"""
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output


def register(app: typer.Typer, get_container):
    @app.command(rich_help_panel="Workflow")
    def commit(
        message: str = typer.Argument(..., help="Commit message (same for every repo with staged changes)"),
        task: Optional[str] = typer.Option(
            None, "--task",
            help="Target task slug. Defaults to cwd (worktree) > MSHIP_TASK env var.",
        ),
    ):
        """Commit staged changes; push new or existing commits if finished."""
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
        skipped: list[dict[str, str]] = []
        push_only_noop = False

        for repo_name in t.affected_repos:
            wt = t.worktrees.get(repo_name)
            if wt is None:
                skipped.append({"repo": repo_name, "reason": "nothing staged"})
                continue
            wt_path = Path(wt)
            if not wt_path.is_dir():
                skipped.append({"repo": repo_name, "reason": "nothing staged"})
                continue

            pr_url = t.pr_urls.get(repo_name)
            staged_check = shell.run("git diff --cached --quiet", cwd=wt_path)
            if staged_check.returncode == 0:
                if t.finished_at is not None and pr_url:
                    ahead_r = shell.run("git rev-list --count @{u}..HEAD", cwd=wt_path)
                    if ahead_r.returncode != 0:
                        output.error(
                            f"{repo_name}: unable to determine commits ahead of upstream — "
                            f"{ahead_r.stderr.strip() or 'unknown error'}"
                        )
                        raise typer.Exit(code=1)
                    try:
                        ahead = int(ahead_r.stdout.strip())
                    except ValueError:
                        output.error(
                            f"{repo_name}: unable to determine commits ahead of upstream — "
                            f"invalid count {ahead_r.stdout.strip()!r}"
                        )
                        raise typer.Exit(code=1)
                    if ahead < 0:
                        output.error(
                            f"{repo_name}: unable to determine commits ahead of upstream — "
                            f"invalid count {ahead}"
                        )
                        raise typer.Exit(code=1)
                    if ahead:
                        push_r = shell.run("git push", cwd=wt_path)
                        if push_r.returncode != 0:
                            output.error(
                                f"{repo_name}: git push failed — "
                                f"{push_r.stderr.strip() or 'unknown error'}"
                            )
                            raise typer.Exit(code=1)
                        log_mgr.append(
                            t.slug, "pushed existing commits to existing PR",
                            repo=repo_name, action="pushed",
                        )
                        results.append({
                            "repo": repo_name,
                            "action": "pushed",
                            "pushed": True,
                            "pr_url": pr_url,
                        })
                        continue
                    skipped.append({"repo": repo_name, "reason": "nothing to push"})
                    push_only_noop = True
                    continue
                skipped.append({"repo": repo_name, "reason": "nothing staged"})
                continue
            if staged_check.returncode != 1:
                output.error(
                    f"{repo_name}: git diff --cached failed — "
                    f"{staged_check.stderr.strip() or 'unknown error'}"
                )
                raise typer.Exit(code=1)

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
                evidence=sha or None,
            )

            pushed = False
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
                "action": "committed",
                "commit_sha": sha,
                "pushed": pushed,
                "pr_url": pr_url if pushed else None,
            })

        if not results and not push_only_noop:
            output.error(
                "nothing staged in any affected repo. "
                "Run `git add <files>` first."
            )
            raise typer.Exit(code=1)

        if results:
            # Agent-agnostic activity heartbeat: a successful commit or push is task work.
            state_mgr.record_activity(t.slug)

        if output.human_mode:
            for r in results:
                if r["action"] == "pushed":
                    output.print(f"  {r['repo']}: pushed existing commits → {r['pr_url']}")
                    continue
                short = r["commit_sha"][:8] if r["commit_sha"] else "(no sha)"
                base = f"  {r['repo']}: committed {short}"
                if r["pushed"]:
                    output.print(base + f" → pushed to {r['pr_url']}")
                else:
                    output.print(base + " (not pushed — task not finished)")
            for s in skipped:
                output.print(f"  {s['repo']}: skipped ({s['reason']})")
        else:
            output.json({
                "task": t.slug,
                "repos": [
                    *results,
                    *[{"repo": s["repo"], "skipped": s["reason"]} for s in skipped],
                ],
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })
