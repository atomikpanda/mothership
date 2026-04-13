import subprocess
from pathlib import Path

from mship.core.view.diff_sources import (
    synthesize_untracked_diff,
    collect_worktree_diff,
    WorktreeDiff,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "seed.txt").write_text("seed\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "seed")


def test_synthesize_untracked_text_file(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "new.py").write_text("print('hi')\nprint('bye')\n")
    out = synthesize_untracked_diff(tmp_path, Path("new.py"))
    assert "+++ b/new.py" in out
    assert "+print('hi')" in out
    assert "+print('bye')" in out
    assert "new file mode" in out


def test_synthesize_untracked_binary_stub(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    out = synthesize_untracked_diff(tmp_path, Path("blob.bin"))
    assert "new binary file" in out
    assert "4 bytes" in out


def test_synthesize_untracked_empty_file(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "empty.txt").write_text("")
    out = synthesize_untracked_diff(tmp_path, Path("empty.txt"))
    assert "new file mode" in out
    assert "+++ b/empty.txt" in out


def test_collect_includes_modified_and_untracked(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\nchanged\n")
    (tmp_path / "added.txt").write_text("added\n")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "x.txt").write_text("x")

    result = collect_worktree_diff(tmp_path)
    assert isinstance(result, WorktreeDiff)
    assert "changed" in result.combined
    assert "+++ b/added.txt" in result.combined
    assert "ignored/x.txt" not in result.combined
    assert result.files_changed >= 2


def test_collect_clean_worktree_is_empty(tmp_path: Path):
    _init_repo(tmp_path)
    result = collect_worktree_diff(tmp_path)
    assert result.combined == ""
    assert result.files_changed == 0
