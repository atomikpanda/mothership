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

    def ensure_upstream(self, repo_path: Path, branch: str) -> None:
        """Ensure `branch`'s tracking ref resolves. No-op when already set.

        `git push -u` normally sets tracking; this is belt-and-suspenders
        so `mship audit` doesn't report `no_upstream` after a finish where
        push succeeded but tracking config somehow wasn't written.
        """
        upstream_ref = f"{branch}@{{u}}"
        check = self._shell.run(
            f"git rev-parse --abbrev-ref --symbolic-full-name {shlex.quote(upstream_ref)}",
            cwd=repo_path,
        )
        if check.returncode == 0:
            return
        self._shell.run(
            f"git branch --set-upstream-to=origin/{shlex.quote(branch)} {shlex.quote(branch)}",
            cwd=repo_path,
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
            stderr_lower = result.stderr.lower()
            if "already exists" in stderr_lower and "pull request" in stderr_lower:
                existing = self.list_pr_for_branch(repo_path, branch)
                if existing is not None:
                    return existing
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

    def check_merged_into_base(self, repo_path: Path, branch: str, base: str) -> bool:
        """True if `branch` is an ancestor of `base` (i.e. already merged).

        Uses `git merge-base --is-ancestor`: exit 0 = ancestor, 1 = not, >1 = error.
        Any error → False (conservative).
        """
        result = self._shell.run(
            f"git merge-base --is-ancestor {shlex.quote(branch)} {shlex.quote(base)}",
            cwd=repo_path,
        )
        return result.returncode == 0

    def check_pushed_to_origin(self, repo_path: Path, branch: str) -> bool:
        """True if `branch` exists on origin at the exact same SHA as local HEAD.

        Any error or mismatch → False (conservative).
        """
        local = self._shell.run(
            f"git rev-parse {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if local.returncode != 0:
            return False
        local_sha = local.stdout.strip()

        remote = self._shell.run(
            f"git ls-remote origin {shlex.quote(branch)}",
            cwd=repo_path,
        )
        if remote.returncode != 0:
            return False
        # Output: "<sha>\trefs/heads/<branch>\n"
        for line in remote.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].strip() == local_sha:
                return True
        return False

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

    def get_merge_commit(self, pr_url: str) -> str | None:
        """Return the integration-side commit SHA for a merged PR, or None.

        Works for merge / squash / rebase styles — gh stores the resulting
        commit on the base branch in `mergeCommit.oid` regardless of style.
        Returns None on any failure (PR not merged, gh down, parse error).
        """
        result = self._shell.run(
            f"gh pr view {shlex.quote(pr_url)} --json mergeCommit -q .mergeCommit.oid",
            cwd=Path("."),
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None

    def fetch_remote_branch(self, repo_path: Path, base: str) -> bool:
        """Refresh `origin/<base>` from the remote. False on network/auth failure."""
        result = self._shell.run(
            f"git fetch origin {shlex.quote(base)}",
            cwd=repo_path,
        )
        return result.returncode == 0

    def list_pr_for_branch(self, repo_path: Path, branch: str) -> str | None:
        """Return the URL of any PR (open/closed/merged) whose head is `branch`, or None.

        Used to:
        - Pre-check whether a PR already exists before calling `create_pr`
          (idempotent retry after mid-loop crash).
        - Fallback-harvest on `gh pr create`'s `already exists` error.
        """
        result = self._shell.run(
            f"gh pr list --head {shlex.quote(branch)} --state all "
            f"--json url -q '.[0].url'",
            cwd=repo_path,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
        return url or None

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
            members = pr.get("members", [pr["repo"]])
            repo_label = (
                pr["repo"] if len(members) == 1
                else f"{pr['repo']} (+{', '.join(m for m in members if m != pr['repo'])})"
            )
            if current_repo in members:
                order_label = "this PR"
            elif pr["order"] == 1:
                order_label = "merge first"
            else:
                order_label = f"merge #{pr['order']}"
            lines.append(
                f"| {pr['order']} | {repo_label} | {pr['url']} | {order_label} |"
            )

        deps_note = " → ".join(pr["repo"] for pr in prs)
        lines.append("")
        lines.append(f"⚠ Merge in order: {deps_note}")

        return "\n".join(lines)
