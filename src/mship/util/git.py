import hashlib
import subprocess
from pathlib import Path


class WorktreeRemovalRefused(RuntimeError):
    """Git could not establish that removing a worktree is safe."""


def worktree_recovery_ref(worktree_path: Path, head: str) -> str:
    """Return the durable recovery ref namespace for a worktree HEAD."""
    resolved_path = Path(worktree_path).resolve()
    path_hash = hashlib.sha256(str(resolved_path).encode()).hexdigest()
    return f"refs/mship/recovery/{path_hash}/{head}"


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

    def _run_removal_git(
        self, repo_path: Path, args: list[str], operation: str
    ) -> subprocess.CompletedProcess[str]:
        """Run a cleanup prerequisite, turning uncertainty into a refusal."""
        try:
            result = subprocess.run(
                args,
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise WorktreeRemovalRefused(
                f"Refusing to remove worktree: could not {operation}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "git failed"
            raise WorktreeRemovalRefused(
                f"Refusing to remove worktree: could not {operation}: {detail}"
            )
        return result

    def _registered_worktrees_for_removal(self, repo_path: Path) -> list[dict[str, str]]:
        result = self._run_removal_git(
            repo_path,
            ["git", "worktree", "list", "--porcelain"],
            "list registered worktrees",
        )
        worktrees: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line.split(" ", 1)[1]}
            elif line.startswith("HEAD "):
                current["head"] = line.split(" ", 1)[1]
        if current:
            worktrees.append(current)
        return worktrees

    def worktree_remove(self, repo_path: Path, worktree_path: Path) -> str:
        """Safely remove a registered linked worktree and return its recovery ref.

        This performs conservative pre-flight checks only; callers must still
        ensure concurrent writers have stopped before asking to remove a tree.
        """
        try:
            target = Path(worktree_path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorktreeRemovalRefused(
                f"Refusing to remove unresolvable worktree {worktree_path}: {exc}"
            ) from exc

        repo_root_result = self._run_removal_git(
            repo_path,
            ["git", "rev-parse", "--show-toplevel"],
            "identify the repository root",
        )
        try:
            repo_root = Path(repo_root_result.stdout.strip()).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorktreeRemovalRefused(
                "Refusing to remove worktree: repository root is unavailable"
            ) from exc
        if target == repo_root:
            raise WorktreeRemovalRefused(
                f"Refusing to remove canonical worktree {target}"
            )

        try:
            registered_worktrees = self._registered_worktrees_for_removal(repo_path)
            primary_worktree = Path(registered_worktrees[0]["path"]).resolve()
            registered = [
                worktree
                for worktree in registered_worktrees
                if Path(worktree["path"]).resolve() == target
            ]
        except (IndexError, KeyError, OSError, RuntimeError) as exc:
            raise WorktreeRemovalRefused(
                "Refusing to remove worktree: registered worktree path is unavailable"
            ) from exc
        if target == primary_worktree:
            raise WorktreeRemovalRefused(
                f"Refusing to remove canonical worktree {target}"
            )
        if len(registered) != 1:
            raise WorktreeRemovalRefused(
                f"Refusing to remove unregistered worktree {target}"
            )

        head_result = self._run_removal_git(
            target,
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            "resolve the worktree HEAD",
        )
        head = head_result.stdout.strip()
        if not head or registered[0].get("head") != head:
            raise WorktreeRemovalRefused(
                f"Refusing to remove worktree {target}: registered HEAD changed"
            )

        status = self._run_removal_git(
            target,
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            "inspect worktree contents",
        )
        if status.stdout.strip():
            raise WorktreeRemovalRefused(
                f"Refusing to remove worktree {target}: it has tracked, untracked, "
                "or ignored content"
            )

        try:
            recovery_ref = worktree_recovery_ref(target, head)
        except (OSError, RuntimeError) as exc:
            raise WorktreeRemovalRefused(
                f"Refusing to remove worktree {target}: cannot name recovery ref"
            ) from exc
        self._run_removal_git(
            repo_path,
            ["git", "update-ref", recovery_ref, head],
            "create the recovery ref",
        )

        rechecked_head = self._run_removal_git(
            target,
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            "recheck the worktree HEAD",
        ).stdout.strip()
        if rechecked_head != head:
            raise WorktreeRemovalRefused(
                f"Refusing to remove worktree {target}: HEAD changed during pre-flight"
            )

        self._run_removal_git(
            repo_path,
            ["git", "worktree", "remove", str(target)],
            "remove the registered worktree",
        )
        return recovery_ref

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
