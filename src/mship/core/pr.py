import shlex
from pathlib import Path

from mship.util.shell import ShellRunner


class PRManager:
    """Create and manage PRs via the gh CLI."""

    def __init__(self, shell: ShellRunner) -> None:
        self._shell = shell

    def check_gh_available(self) -> None:
        result = self._shell.run("gh auth status", cwd=Path("."))
        if result.returncode == 127:
            raise RuntimeError(
                "gh CLI not found. Install it: https://cli.github.com"
            )
        if result.returncode != 0:
            raise RuntimeError(
                "gh CLI not authenticated. Run `gh auth login` first."
            )

    def push_branch(self, repo_path: Path, branch: str) -> None:
        result = self._shell.run(
            f"git push -u origin {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to push branch '{branch}': {result.stderr.strip()}"
            )

    def create_pr(
        self, repo_path: Path, branch: str, title: str, body: str,
        base: str | None = None,
    ) -> str:
        safe_title = shlex.quote(title)
        safe_body = shlex.quote(body)
        cmd = (
            f"gh pr create --title {safe_title} --body {safe_body} "
            f"--head {shlex.quote(branch)}"
        )
        if base is not None:
            cmd += f" --base {shlex.quote(base)}"
        result = self._shell.run(cmd, cwd=repo_path)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create PR: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def count_commits_ahead(self, repo_path: Path, base: str, branch: str) -> int:
        """Return the number of commits on `branch` not on `base`.

        Uses `origin/<base>` so the comparison is against the remote (same
        reference gh will use). Returns 0 on any git failure (fail-closed: a
        caller treating 0 as "empty" will surface a clear error instead of
        attempting a doomed push).
        """
        spec = f"origin/{base}..{branch}"
        result = self._shell.run(
            f"git rev-list --count {shlex.quote(spec)}",
            cwd=repo_path,
        )
        if result.returncode != 0:
            return 0
        try:
            return int(result.stdout.strip() or "0")
        except ValueError:
            return 0

    def verify_base_exists(self, repo_path: Path, base: str) -> bool:
        """Return True if `base` exists as a head on origin, else False.

        Network/auth failures are treated as False (fail-closed).
        """
        result = self._shell.run(
            f"git ls-remote --heads origin {shlex.quote(base)}",
            cwd=repo_path,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())

    def check_pr_state(self, pr_url: str) -> str:
        """Return 'merged', 'closed', 'open', or 'unknown' for a PR URL.

        Uses `gh pr view --json state`. Any failure returns 'unknown'.
        """
        result = self._shell.run(
            f"gh pr view {shlex.quote(pr_url)} --json state -q .state",
            cwd=Path("."),
        )
        if result.returncode != 0:
            return "unknown"
        raw = result.stdout.strip().upper()
        mapping = {"MERGED": "merged", "CLOSED": "closed", "OPEN": "open"}
        return mapping.get(raw, "unknown")

    def get_pr_body(self, pr_url: str) -> str:
        result = self._shell.run(
            f"gh pr view {shlex.quote(pr_url)} --json body -q .body",
            cwd=Path("."),
        )
        return result.stdout.strip()

    def update_pr_body(self, pr_url: str, body: str) -> None:
        safe_body = shlex.quote(body)
        self._shell.run(
            f"gh pr edit {shlex.quote(pr_url)} --body {safe_body}",
            cwd=Path("."),
        )

    def build_coordination_block(
        self,
        task_slug: str,
        prs: list[dict],
        current_repo: str,
    ) -> str:
        if len(prs) <= 1:
            return ""

        lines = [
            "",
            "---",
            "",
            "## Cross-repo coordination (mothership)",
            "",
            f"This PR is part of a coordinated change: `{task_slug}`",
            "",
            "| # | Repo | PR | Merge order |",
            "|---|------|----|-------------|",
        ]

        for pr in prs:
            if pr["repo"] == current_repo:
                order_label = "this PR"
            elif pr["order"] == 1:
                order_label = "merge first"
            else:
                order_label = f"merge #{pr['order']}"
            lines.append(
                f"| {pr['order']} | {pr['repo']} | {pr['url']} | {order_label} |"
            )

        deps_note = " → ".join(pr["repo"] for pr in prs)
        lines.append("")
        lines.append(f"⚠ Merge in order: {deps_note}")

        return "\n".join(lines)
