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


def _parse_one_chunk(chunk: str) -> FileDiff:
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
    return FileDiff(path=path, additions=additions, deletions=deletions, body=chunk)


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


def collect_worktree_diff(worktree: Path) -> WorktreeDiff:
    tracked = _tracked_diff(worktree)
    untracked_paths = _list_untracked(worktree)
    synthesized = "".join(synthesize_untracked_diff(worktree, p) for p in untracked_paths)
    combined = tracked + synthesized
    files = tuple(split_diff_by_file(combined))
    return WorktreeDiff(root=worktree, files=files)
