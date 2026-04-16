import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


_LOCKFILE_NAMES: frozenset[str] = frozenset({
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
})


@dataclass(frozen=True)
class FileDiff:
    path: str
    additions: int
    deletions: int
    body: str
    status: str = "M"  # "N" | "M" | "D" | "R"
    old_path: str | None = None  # set only when status == "R"

    @property
    def is_lockfile(self) -> bool:
        return Path(self.path).name in _LOCKFILE_NAMES


@dataclass(frozen=True)
class WorktreeDiff:
    root: Path
    files: tuple[FileDiff, ...]

    @property
    def files_changed(self) -> int:
        return len(self.files)

    @property
    def combined(self) -> str:
        return "".join(f.body for f in self.files)


def _is_binary(content: bytes) -> bool:
    return b"\0" in content[:8000]


def synthesize_untracked_diff(worktree: Path, rel_path: Path) -> str:
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
        cwd=worktree, check=True, capture_output=True,
    )
    raw = result.stdout.decode("utf-8", errors="replace")
    return [Path(p) for p in raw.split("\0") if p]


def _tracked_diff(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=worktree, check=True, capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


_PATH_FROM_PLUS = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_PATH_FROM_MINUS = re.compile(r"^--- a/(.+)$", re.MULTILINE)
_PATH_FROM_HEADER = re.compile(r"^diff --git a/(\S+) b/\S+", re.MULTILINE)

_RENAME_FROM = re.compile(r"^rename from (.+)$", re.MULTILINE)
_RENAME_TO = re.compile(r"^rename to (.+)$", re.MULTILINE)


def _detect_status(header: str) -> tuple[str, str | None]:
    """Return (status, old_path) from a diff chunk's header region.

    `header` is the slice of the chunk before the first `@@` line (or the
    whole chunk when there are no hunks, e.g. pure renames)."""
    m_from = _RENAME_FROM.search(header)
    m_to = _RENAME_TO.search(header)
    if m_from and m_to:
        return "R", m_from.group(1)
    if "\nnew file mode " in "\n" + header or header.startswith("new file mode "):
        return "N", None
    if "\ndeleted file mode " in "\n" + header or header.startswith("deleted file mode "):
        return "D", None
    return "M", None


def _parse_one_chunk(chunk: str) -> FileDiff:
    # Header region: everything up to the first `@@` hunk (if any).
    hunk_idx = chunk.find("\n@@")
    header = chunk if hunk_idx == -1 else chunk[:hunk_idx]

    status, old_path = _detect_status(header)

    if status == "R":
        # Path is the rename target; extracted from `rename to <new>`.
        m = _RENAME_TO.search(header)
        path = m.group(1) if m else "<unknown>"
    else:
        m = _PATH_FROM_PLUS.search(chunk)
        if m and m.group(1) != "/dev/null":
            path = m.group(1)
        else:
            m = _PATH_FROM_MINUS.search(chunk)
            if m and m.group(1) != "/dev/null":
                path = m.group(1)
            else:
                m = _PATH_FROM_HEADER.search(chunk)
                path = m.group(1) if m else "<unknown>"

    additions = 0
    deletions = 0
    for line in chunk.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return FileDiff(
        path=path,
        additions=additions,
        deletions=deletions,
        body=chunk,
        status=status,
        old_path=old_path,
    )


def split_diff_by_file(combined: str) -> list[FileDiff]:
    """Parse a concatenated 'diff --git' stream into per-file chunks."""
    if not combined:
        return []
    # Use a sentinel so the split preserves the 'diff --git ' prefix on each chunk.
    parts = combined.split("\ndiff --git ")
    chunks: list[str] = []
    first = parts[0]
    if first.startswith("diff --git "):
        chunks.append(first if first.endswith("\n") else first + "\n")
    elif first.strip():
        # Leading content before any 'diff --git' header — unusual; keep as-is.
        chunks.append(first if first.endswith("\n") else first + "\n")
    for rest in parts[1:]:
        chunk = "diff --git " + rest
        if not chunk.endswith("\n"):
            chunk += "\n"
        chunks.append(chunk)
    return [_parse_one_chunk(c) for c in chunks if c.strip()]


def _committed_diff(worktree: Path, base_branch: str) -> str:
    """`git diff <merge-base>...HEAD` — commits on HEAD not in base_branch."""
    try:
        mb = subprocess.run(
            ["git", "merge-base", base_branch, "HEAD"],
            cwd=worktree, check=True, capture_output=True,
        ).stdout.decode("utf-8", errors="replace").strip()
    except subprocess.CalledProcessError:
        return ""
    if not mb:
        return ""
    result = subprocess.run(
        ["git", "diff", f"{mb}..HEAD"],
        cwd=worktree, check=True, capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def _merge_file_diffs(
    committed: list[FileDiff], uncommitted: list[FileDiff]
) -> list[FileDiff]:
    """Merge per-file diffs. Files in both get additions/deletions summed and
    bodies concatenated with a `-- uncommitted --` separator. Status is
    inherited from the committed side when both sides exist (the review
    lens is 'what's new on this branch vs. base')."""
    by_path: dict[str, FileDiff] = {f.path: f for f in committed}
    for u in uncommitted:
        if u.path in by_path:
            c = by_path[u.path]
            body = c.body.rstrip("\n") + "\n-- uncommitted --\n" + u.body
            by_path[u.path] = FileDiff(
                path=u.path,
                additions=c.additions + u.additions,
                deletions=c.deletions + u.deletions,
                body=body,
                status=c.status,
                old_path=c.old_path,
            )
        else:
            by_path[u.path] = u
    return list(by_path.values())


def collect_worktree_diff(
    worktree: Path, base_branch: str | None = None
) -> WorktreeDiff:
    tracked = _tracked_diff(worktree)
    untracked_paths = _list_untracked(worktree)
    synthesized = "".join(synthesize_untracked_diff(worktree, p) for p in untracked_paths)
    uncommitted_combined = tracked + synthesized
    uncommitted_files = split_diff_by_file(uncommitted_combined)

    if base_branch is None:
        return WorktreeDiff(root=worktree, files=tuple(uncommitted_files))

    committed_combined = _committed_diff(worktree, base_branch)
    committed_files = split_diff_by_file(committed_combined)
    merged = _merge_file_diffs(committed_files, uncommitted_files)
    return WorktreeDiff(root=worktree, files=tuple(merged))
