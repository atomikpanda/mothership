import subprocess
from pathlib import Path


class GitRunner:
    """Git operations for worktree and branch management."""

    def worktree_add(self, repo_path: Path, worktree_path: Path, branch: str) -> None:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def worktree_remove(self, repo_path: Path, worktree_path: Path) -> None:
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def branch_delete(self, repo_path: Path, branch: str) -> None:
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def is_ignored(self, repo_path: Path, pattern: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "-q", pattern],
            cwd=repo_path,
            capture_output=True,
        )
        return result.returncode == 0

    def add_to_gitignore(self, repo_path: Path, pattern: str) -> None:
        gitignore = repo_path / ".gitignore"
        if gitignore.exists():
            content = gitignore.read_text()
            if pattern in content.splitlines():
                return
            if not content.endswith("\n"):
                content += "\n"
            content += f"{pattern}\n"
        else:
            content = f"{pattern}\n"
        gitignore.write_text(content)

    def has_uncommitted_changes(self, repo_path: Path) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())

    def run_worktree_prune(self, repo_path: Path) -> None:
        """Clean up stale git worktree tracking."""
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_path,
            capture_output=True,
        )

    def worktree_list(self, repo_path: Path) -> list[dict[str, str]]:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        worktrees = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line.split(" ", 1)[1]}
            elif line.startswith("branch "):
                current["branch"] = line.split(" ", 1)[1]
        if current:
            worktrees.append(current)
        return worktrees
