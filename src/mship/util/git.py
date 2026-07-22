import subprocess
from pathlib import Path


class GitRunner:
    """Git operations for worktree and branch management."""

    def worktree_add(
        self, repo_path: Path, worktree_path: Path, branch: str, start_point: str | None = None
    ) -> None:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "worktree", "add", str(worktree_path), "-b", branch]
        if start_point is not None:
            cmd.append(start_point)
        subprocess.run(cmd, cwd=repo_path, check=True, capture_output=True, text=True)

    def has_remote(self, repo_path: Path, remote: str = "origin") -> bool:
        """True if `remote` is configured for the repo."""
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            cwd=repo_path, capture_output=True, text=True,
        )
        return result.returncode == 0

    def fast_forward_if_clean(self, repo_path: Path, base: str, remote: str = "origin") -> bool:
        """Best-effort fast-forward of the canonical checkout's `base` to `<remote>/<base>`.

        Only acts when the checkout is ON `base`, has no uncommitted changes, and a
        fast-forward is possible. Returns whether it advanced. Never resets or forces;
        a diverged/ahead/dirty/off-base checkout is left untouched.
        """
        cur = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if cur.returncode != 0 or cur.stdout.strip() != base:
            return False
        if self.has_uncommitted_changes(repo_path):
            return False
        result = subprocess.run(
            ["git", "merge", "--ff-only", f"{remote}/{base}"],
            cwd=repo_path, capture_output=True, text=True,
        )
        return result.returncode == 0

    def worktree_add_detached(self, repo_path: Path, worktree_path: Path, ref: str) -> None:
        """Create a detached-HEAD worktree at `worktree_path` pointing at `ref`.

        `ref` may be a SHA, a tag, or a remote ref like `origin/main`.
        """
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), ref],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def ref_exists(self, repo_path: Path, ref: str) -> bool:
        """True if `ref` resolves to a commit in the repo.

        Works for local branches (`feat/x`), remote-tracking refs
        (`origin/feat/x`, only after a fetch), tags, and SHAs. Used by
        `spawn --base` to validate the requested base before cutting worktrees.
        """
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=repo_path, capture_output=True, text=True,
        )
        return result.returncode == 0

    def fetch_remote_ref(self, repo_path: Path, ref: str, remote: str = "origin") -> bool:
        """Fetch a single ref from `remote`. Returns True on success, False on any failure.

        Used by passive-worktree materialization: we want to know if origin has
        the ref, and we want it locally as `<remote>/<ref>` for `worktree add`.
        """
        try:
            result = subprocess.run(
                ["git", "fetch", remote, ref],
                cwd=repo_path, capture_output=True, text=True, check=False, timeout=60,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

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

    def add(self, repo_path: Path, path: Path) -> None:
        """Stage a path (spec-storage-visibility-policy migrate-storage: track the
        newly-materialised spec representation so it lands in the next commit)."""
        subprocess.run(
            ["git", "add", "--", str(path)],
            cwd=repo_path, check=True, capture_output=True, text=True,
        )

    def rm(self, repo_path: Path, path: Path, *, cached: bool = False, force: bool = False) -> None:
        """Remove `path` from the index (and the working tree unless `cached`).
        Used by migrate-storage to drop a spec's old on-disk representation.

        `force` (git rm -f) overrides git's staged/working-tree-changes safety —
        correct for migration, where the old representation's content is already
        preserved in the freshly-written new representation, so removing it can
        never lose data. Still raises CalledProcessError on a GENUINE failure (the
        caller must NOT swallow it, or a spec could be left readable in a mode
        meant to hide it)."""
        args = ["git", "rm", "-q"]
        if cached:
            args.append("--cached")
        if force:
            args.append("-f")
        args += ["--", str(path)]
        subprocess.run(args, cwd=repo_path, check=True, capture_output=True, text=True)

    def is_tracked(self, repo_path: Path, path: Path) -> bool:
        """True when `path` is tracked in the index (git ls-files --error-unmatch).
        Lets migrate-storage act only when needed (idempotent) while still failing
        loud on a genuine git error, rather than blanket-swallowing failures."""
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(path)],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        return result.returncode == 0

    def remove_from_gitignore(self, repo_path: Path, pattern: str) -> None:
        """Drop `pattern` from `.gitignore` (inverse of add_to_gitignore). Used when
        migrating back to `committed` so specs become public + trackable again."""
        gitignore = repo_path / ".gitignore"
        if not gitignore.exists():
            return
        kept = [ln for ln in gitignore.read_text().splitlines() if ln.strip() != pattern]
        gitignore.write_text("\n".join(kept) + ("\n" if kept else ""))

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

    def has_unpushed_commits(self, worktree_path: Path) -> bool:
        """True if the worktree's current branch has commits not safely on origin.

        Teardown guard (spec auto-advance-on-merge):
        - No `origin` remote at all -> False (nothing to push to; a no-remote
          checkout can't have "unpushed" work in the sense this guard protects,
          and every no-remote fixture must still tear down).
        - Upstream tracking ref set (post-`finish`, this is origin/<branch>) ->
          True iff `git rev-list --count @{u}..HEAD` > 0.
        - `origin` exists but the branch has NO upstream tracking ref (it was
          never `push -u`'d) -> True: we can't prove its commits are on origin,
          so refuse (conservative).
        Any git error -> True (conservative: refuse rather than risk data loss).
        """
        if not self.has_remote(worktree_path, "origin"):
            return False
        upstream = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=worktree_path, capture_output=True, text=True,
        )
        if upstream.returncode != 0:
            return True
        ahead = subprocess.run(
            ["git", "rev-list", "--count", "@{u}..HEAD"],
            cwd=worktree_path, capture_output=True, text=True,
        )
        if ahead.returncode != 0:
            return True
        try:
            return int(ahead.stdout.strip() or "0") > 0
        except ValueError:
            return True

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
