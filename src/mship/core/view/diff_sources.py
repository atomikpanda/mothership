import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorktreeDiff:
    root: Path
    combined: str
    files_changed: int


def _is_binary(content: bytes) -> bool:
    return b"\0" in content[:8000]


def synthesize_untracked_diff(worktree: Path, rel_path: Path) -> str:
    """Return a 'new file' diff header+hunks for an untracked file.

    Binary files get a stub line.
    """
    abs_path = worktree / rel_path
    data = abs_path.read_bytes()
    header = (
        f"diff --git a/{rel_path} b/{rel_path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{rel_path}\n"
    )
    if _is_binary(data):
        return f"{header}new binary file, {len(data)} bytes\n"

    if not data:
        return header
    lines = data.decode("utf-8", errors="replace").splitlines()
    hunk = f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    return header + hunk


def _list_untracked(worktree: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    raw = result.stdout.decode("utf-8", errors="replace")
    return [Path(p) for p in raw.split("\0") if p]


def _tracked_diff(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def collect_worktree_diff(worktree: Path) -> WorktreeDiff:
    tracked = _tracked_diff(worktree)
    untracked = _list_untracked(worktree)
    synthesized = "".join(synthesize_untracked_diff(worktree, p) for p in untracked)
    combined = tracked + synthesized
    files_changed = combined.count("\ndiff --git ") + (1 if combined.startswith("diff --git ") else 0)
    return WorktreeDiff(root=worktree, combined=combined, files_changed=files_changed)
