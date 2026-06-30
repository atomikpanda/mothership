"""Hidden mship commands — used by hooks and other internal consumers."""
import json
import os
import sys
from pathlib import Path

import typer


def _staged_source_paths(toplevel: str, container) -> list[str]:
    """List staged paths under src/ or tests/ at `toplevel` when it is the
    workspace root. Returns [] on any error (fail-open by design).

    Used by `_check-commit` to enforce "spawn before editing source files"
    when no task is active. Bare patterns (`src/`, `tests/`) are intentionally
    narrow — anything outside those still commits freely.
    """
    import subprocess
    try:
        tl = Path(toplevel).resolve()
        cfg = Path(container.config_path()).resolve()
        if not cfg.is_file() or tl != cfg.parent:
            return []
        result = subprocess.run(
            ["git", "-C", str(tl), "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode != 0:
            return []
        return [
            p for p in result.stdout.splitlines()
            if p.startswith("src/") or p.startswith("tests/")
        ]
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return []


def register(app: typer.Typer, get_container):
    @app.command(name="_check-commit", hidden=True)
    def check_commit(toplevel: str = typer.Argument(..., help="git rev-parse --show-toplevel value")):
        """Exit 0 if committing at `toplevel` is allowed under the active tasks.

        Rules:
        - No active tasks AND staged paths under src/ or tests/ at the
          workspace root -> reject (exit 1). Closes the "agent edits main
          without spawning" loophole.
        - No active tasks otherwise -> allow (exit 0).
        - Active tasks but toplevel not in any registered worktree -> reject (exit 1).
        - toplevel matches an active task's worktree -> allow (after reconcile gate).

        Fail-open on any exception (corrupt state, missing config, etc.) -> exit 0.
        """
        from mship.core.gate import resolve_bypass, record_bypass
        bypassed, reason = resolve_bypass()
        if bypassed:
            try:
                _c = get_container(required=False)
                if _c is not None:
                    import subprocess as _sp
                    _branch = ""
                    try:
                        _r = _sp.run(
                            ["git", "-C", toplevel, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, check=False, timeout=3,
                        )
                        if _r.returncode == 0:
                            _branch = _r.stdout.strip()
                    except Exception:
                        pass
                    record_bypass(Path(_c.config_path()).parent, op="commit", branch=_branch, reason=reason)
            except Exception:
                pass
            raise typer.Exit(code=0)

        try:
            container = get_container(required=False)
            if container is None:
                raise typer.Exit(code=0)
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        if not state.tasks:
            # In a mship workspace with no active task, source/test edits at
            # the workspace root are almost always "agent edited main without
            # spawning". Require a spawn so the existing worktree-gate covers
            # the rest of the lifecycle.
            staged_source = _staged_source_paths(toplevel, container)
            if staged_source:
                import sys
                n = len(staged_source)
                preview = ", ".join(staged_source[:3])
                if n > 3:
                    preview += f", … (+{n - 3} more)"
                sys.stderr.write(
                    f"⛔ mship: refusing commit — no active task and "
                    f"{n} staged file{'s' if n != 1 else ''} under src/ or "
                    f"tests/ ({preview}).\n"
                    f"   Spawn a task first:\n"
                    f"     mship spawn \"<description>\"\n"
                    f"   then move staged changes into the new worktree:\n"
                    f"     git stash push --staged -m misrouted\n"
                    f"     cd .worktrees/<slug>/<repo> && git stash pop\n"
                    f"   (or `git commit --no-verify` to override).\n"
                )
                raise typer.Exit(code=1)
            raise typer.Exit(code=0)

        try:
            tl = Path(toplevel).resolve()
            registered = [
                (slug, repo, Path(wt).resolve())
                for slug, task in state.tasks.items()
                for repo, wt in task.worktrees.items()
            ]
        except (OSError, RuntimeError):
            raise typer.Exit(code=0)

        matched_task = None
        matched_repo: str | None = None
        for slug, repo, wt in registered:
            if tl == wt:
                matched_task = state.tasks[slug]
                matched_repo = repo
                break

        if matched_task is not None and matched_repo in matched_task.passive_repos:
            import sys
            sys.stderr.write(
                f"⛔ mship: refusing commit — {tl} is a passive worktree of "
                f"`{matched_repo}` for task `{matched_task.slug}`.\n"
                f"   To edit {matched_repo}, close this task and respawn with "
                f"`--repos {matched_repo},...`\n"
                f"   (or `git commit --no-verify` to override).\n"
            )
            raise typer.Exit(code=1)

        if matched_task is not None:
            # Reconcile gate (per-task, unchanged behavior)
            try:
                from mship.core.reconcile.cache import ReconcileCache
                from mship.core.reconcile.fetch import (
                    collect_git_snapshots, fetch_pr_snapshots,
                )
                from mship.core.reconcile.gate import (
                    GateAction, reconcile_now, should_block,
                )
                cache = ReconcileCache(container.state_dir())

                def _fetcher(branches, worktrees_by_branch):
                    return (
                        fetch_pr_snapshots(branches),
                        collect_git_snapshots(worktrees_by_branch),
                    )

                decisions = reconcile_now(state, cache=cache, fetcher=_fetcher)
            except Exception:
                raise typer.Exit(code=0)

            ignored = cache.read_ignores()
            d = decisions.get(matched_task.slug)
            if d is not None:
                action = should_block(d, command="precommit", ignored=ignored)
                if action is GateAction.block:
                    import sys
                    sys.stderr.write(
                        f"\u26d4 mship: refusing commit — task '{matched_task.slug}' has "
                        f"{d.state.value} drift"
                        + (f" (PR #{d.pr_number}).\n" if d.pr_number else ".\n")
                        + "   Run `mship reconcile` for details, or `git commit --no-verify` to override.\n"
                    )
                    raise typer.Exit(code=1)
            raise typer.Exit(code=0)

        # No match — reject with list of active worktrees.
        import shlex
        import subprocess
        import sys
        sys.stderr.write(
            f"\u26d4 mship: refusing commit — {tl} is not a registered worktree.\n"
            f"   Active task worktrees:\n"
        )
        for slug, _repo, wt in registered:
            sys.stderr.write(f"     {wt} ({slug})\n")

        # If the rejected toplevel has uncommitted changes, it's almost
        # certainly a misrouted-edit situation (absolute paths in a tool call
        # bypassed the user's cwd). Show exact recovery commands per worktree.
        has_changes = False
        try:
            probe = subprocess.run(
                ["git", "-C", str(tl), "status", "--porcelain"],
                capture_output=True, text=True, check=False, timeout=5,
            )
            has_changes = probe.returncode == 0 and bool(probe.stdout.strip())
        except (subprocess.SubprocessError, OSError):
            pass

        if has_changes:
            q_tl = shlex.quote(str(tl))
            sys.stderr.write(
                f"\n   {tl} has uncommitted changes — looks like edits landed here\n"
                f"   instead of the worktree. To move them:\n"
            )
            for slug, _repo, wt in registered:
                q_wt = shlex.quote(str(wt))
                sys.stderr.write(
                    f"     git -C {q_tl} stash push -u -m {slug}-misrouted\n"
                    f"     cd {q_wt} && git stash pop\n"
                )
            if len(registered) > 1:
                sys.stderr.write(
                    "   (pick the worktree the edits belong to — don't run both)\n"
                )

        sys.stderr.write(
            "\n   cd into a worktree, or use `git commit --no-verify` to override.\n"
        )
        raise typer.Exit(code=1)

    @app.command(name="_check-push", hidden=True)
    def check_push():
        """Reject pushing a branch-pattern branch that is not a registered task
        branch. Reads git pre-push ref lines from stdin. Fail-open on error."""
        import sys
        from mship.core.gate import resolve_bypass, record_bypass

        try:
            container = get_container(required=False)
            if container is None:
                raise typer.Exit(code=0)
            config = container.config()
            state = container.state_manager().load()
        except typer.Exit:
            raise
        except Exception:
            raise typer.Exit(code=0)

        prefix = config.branch_pattern.split("{slug}", 1)[0]  # e.g. "feat/"
        task_branches = {t.branch for t in state.tasks.values()}

        offending: list[str] = []
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            local_ref, local_sha = parts[0], parts[1]
            if set(local_sha) == {"0"}:           # delete — nothing pushed
                continue
            if not local_ref.startswith("refs/heads/"):
                continue
            branch = local_ref[len("refs/heads/"):]
            if prefix and branch.startswith(prefix) and branch not in task_branches:
                if branch not in offending:
                    offending.append(branch)

        if not offending:
            raise typer.Exit(code=0)

        bypassed, reason = resolve_bypass()
        if bypassed:
            ws_root = Path(container.config_path()).parent
            for b in offending:
                record_bypass(ws_root, op="push", branch=b, reason=reason)
            raise typer.Exit(code=0)

        sys.stderr.write(
            "⛔ mship: refusing push — branch(es) not registered to a task: "
            + ", ".join(offending) + "\n"
            + "   Spawn a task (mship spawn) so the branch is tracked, or set "
            + "MSHIP_BYPASS_GATE=1 (or `git push --no-verify`) to override.\n"
        )
        raise typer.Exit(code=1)

    @app.command(name="_guard-edit", hidden=True)
    def _guard_edit():
        """PreToolUse guard: refuse edits to a repo's MAIN checkout while a task
        is active. Reads the Claude Code hook event JSON from stdin. Denies with
        exit code 2 (stderr shown to the model); allows with exit 0. Fails OPEN
        on any error — never block on uncertainty."""
        from mship.core.edit_guard import evaluate_edit

        if os.environ.get("MSHIP_ALLOW_MAIN_EDIT") == "1":
            raise typer.Exit(code=0)
        try:
            raw = sys.stdin.read()
            event = json.loads(raw) if raw.strip() else {}
            tool_input = event.get("tool_input") or {}
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not target:
                raise typer.Exit(code=0)
            container = get_container(required=False)
            if container is None:
                raise typer.Exit(code=0)
            state = container.state_manager().load()
            config = container.config()
            decision = evaluate_edit(target, state, config)
        except typer.Exit:
            raise
        except Exception:
            raise typer.Exit(code=0)  # fail open
        if decision.allowed:
            raise typer.Exit(code=0)
        sys.stderr.write(decision.reason + "\n")
        raise typer.Exit(code=2)

    @app.command(name="_session-context", hidden=True)
    def session_context():
        """Print the no-active-task notice for the SessionStart hook (else nothing)."""
        from mship.core.gate import no_task_notice
        notice = no_task_notice(Path.cwd())
        if notice:
            import sys
            sys.stdout.write(notice + "\n")
        raise typer.Exit(code=0)

    @app.command(name="_post-checkout", hidden=True)
    def post_checkout(
        prev_head: str = typer.Argument(...),
        new_head: str = typer.Argument(...),
    ):
        """Warn loudly when the checkout doesn't match any active task's worktree."""
        import subprocess
        import sys
        from pathlib import Path

        try:
            container = get_container(required=False)
            if container is None:
                raise typer.Exit(code=0)
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, cwd=Path.cwd(),
            )
        except Exception:
            raise typer.Exit(code=0)
        if result.returncode != 0:
            raise typer.Exit(code=0)
        current_branch = result.stdout.strip()

        if current_branch in {"main", "master", "develop"}:
            raise typer.Exit(code=0)

        if not state.tasks:
            sys.stderr.write(
                f"\u26a0 mship: checked out '{current_branch}' but no active mship task.\n"
                f"  If you're starting feature work, run `mship spawn \"<description>\"`.\n"
            )
            raise typer.Exit(code=0)

        cwd = Path.cwd().resolve()
        matched_task = None
        for task in state.tasks.values():
            for wt in task.worktrees.values():
                try:
                    cwd.relative_to(Path(wt).resolve())
                    matched_task = task
                    break
                except ValueError:
                    continue
            if matched_task is not None:
                break

        if matched_task is None:
            active = ", ".join(sorted(state.tasks.keys()))
            sys.stderr.write(
                f"\u26a0 mship: you checked out '{current_branch}' outside any active worktree.\n"
                f"  Active tasks: {active}\n"
                f"  cd into one of the registered worktrees before editing.\n"
            )
            raise typer.Exit(code=0)

        if current_branch != matched_task.branch:
            sys.stderr.write(
                f"\u26a0 mship: checked out '{current_branch}' but the matched worktree\n"
                f"  belongs to task '{matched_task.slug}' on '{matched_task.branch}'.\n"
            )
        raise typer.Exit(code=0)

    @app.command(name="_journal-commit", hidden=True)
    def journal_commit():
        """Auto-append a commit record to the task whose worktree contains cwd."""
        import subprocess
        from pathlib import Path

        try:
            container = get_container(required=False)
            if container is None:
                raise typer.Exit(code=0)
            state = container.state_manager().load()
        except Exception:
            raise typer.Exit(code=0)

        if not state.tasks:
            raise typer.Exit(code=0)

        cwd = Path.cwd().resolve()
        matched_task = None
        matched_repo: str | None = None
        for task in state.tasks.values():
            for repo_name, wt_path in task.worktrees.items():
                wt_resolved = Path(wt_path).resolve()
                try:
                    cwd.relative_to(wt_resolved)
                    matched_task = task
                    matched_repo = repo_name
                    break
                except ValueError:
                    continue
            if matched_task is not None:
                break

        if matched_task is None:
            raise typer.Exit(code=0)

        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H%n%s"],
                cwd=cwd, capture_output=True, text=True, check=False,
            )
        except Exception:
            raise typer.Exit(code=0)
        if result.returncode != 0:
            raise typer.Exit(code=0)

        lines = result.stdout.splitlines()
        if not lines:
            raise typer.Exit(code=0)
        sha = lines[0].strip()
        subject = lines[1].strip() if len(lines) > 1 else ""

        try:
            container.log_manager().append(
                matched_task.slug,
                f"commit {sha[:10]}: {subject}",
                repo=matched_repo,
                iteration=matched_task.test_iteration if matched_task.test_iteration else None,
                action="committed",
            )
        except Exception:
            pass
        raise typer.Exit(code=0)
