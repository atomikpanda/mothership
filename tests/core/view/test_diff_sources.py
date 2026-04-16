import subprocess
from pathlib import Path

from mship.core.view.diff_sources import (
    synthesize_untracked_diff,
    collect_worktree_diff,
    WorktreeDiff,
)
from mship.core.view.diff_sources import (
    FileDiff,
    _LOCKFILE_NAMES,
    split_diff_by_file,
)


_SAMPLE_TWO_FILES = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "index abc..def 100644\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " line\n"
    "+new\n"
    "diff --git a/src/bar.py b/src/bar.py\n"
    "index 111..222 100644\n"
    "--- a/src/bar.py\n"
    "+++ b/src/bar.py\n"
    "@@ -1,2 +1,1 @@\n"
    " keep\n"
    "-dropped\n"
)


def test_split_empty_returns_empty_list():
    assert split_diff_by_file("") == []


def test_split_single_file():
    combined = (
        "diff --git a/foo.txt b/foo.txt\n"
        "--- a/foo.txt\n"
        "+++ b/foo.txt\n"
        "@@ -1,0 +1,1 @@\n"
        "+hi\n"
    )
    (f,) = split_diff_by_file(combined)
    assert f.path == "foo.txt"
    assert f.additions == 1
    assert f.deletions == 0
    assert f.body == combined


def test_split_two_files_roundtrip():
    result = split_diff_by_file(_SAMPLE_TWO_FILES)
    assert [f.path for f in result] == ["src/foo.py", "src/bar.py"]
    assert result[0].additions == 1 and result[0].deletions == 0
    assert result[1].additions == 0 and result[1].deletions == 1
    assert "".join(f.body for f in result) == _SAMPLE_TWO_FILES


def test_split_synthesized_untracked_file():
    combined = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+print('hi')\n"
    )
    (f,) = split_diff_by_file(combined)
    assert f.path == "new.py"
    assert f.additions == 1


def test_split_binary_stub():
    combined = (
        "diff --git a/blob.bin b/blob.bin\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/blob.bin\n"
        "new binary file, 42 bytes\n"
    )
    (f,) = split_diff_by_file(combined)
    assert f.path == "blob.bin"
    assert f.additions == 0
    assert f.deletions == 0


def test_split_malformed_chunk_is_tolerated():
    # Missing +++ line; path is extracted from the diff --git header as a fallback
    combined = "diff --git a/x b/x\nsomething broken\n"
    (f,) = split_diff_by_file(combined)
    assert f.path == "x"
    assert f.body == combined


def test_file_is_lockfile_true_for_known_names():
    for name in ["package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                  "poetry.lock", "uv.lock", "Pipfile.lock",
                  "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum"]:
        f = FileDiff(path=f"some/sub/{name}", additions=0, deletions=0, body="")
        assert f.is_lockfile, f"{name} should be a lockfile"


def test_file_is_lockfile_false_for_other_names():
    for name in ["src/foo.py", "README.md", "Taskfile.yml", "mothership.yaml"]:
        f = FileDiff(path=name, additions=1, deletions=0, body="")
        assert not f.is_lockfile


def test_collect_worktree_diff_exposes_files(tmp_path):
    import subprocess
    from mship.core.view.diff_sources import collect_worktree_diff

    def _git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("seed\n")
    _git("add", ".")
    _git("commit", "-q", "-m", "seed")
    (tmp_path / "seed.txt").write_text("seed\nchanged\n")
    (tmp_path / "added.txt").write_text("added\n")

    wd = collect_worktree_diff(tmp_path)
    paths = sorted(f.path for f in wd.files)
    assert paths == ["added.txt", "seed.txt"]
    # Backward-compat: .combined still produces the full concat.
    assert "+changed" in wd.combined
    assert "+++ b/added.txt" in wd.combined
    assert wd.files_changed == len(wd.files) == 2


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


def test_collect_includes_committed_and_uncommitted_when_base_given(tmp_path: Path):
    _init_repo(tmp_path)
    # Branch off main; commit changes to a file; also make an uncommitted change.
    _git(tmp_path, "checkout", "-q", "-b", "feat/x")
    (tmp_path / "committed.txt").write_text("committed-body\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "add committed.txt")

    # Uncommitted change to a different file.
    (tmp_path / "seed.txt").write_text("seed\nuncommitted-change\n")

    result = collect_worktree_diff(tmp_path, base_branch="main")
    paths = {f.path for f in result.files}
    assert "committed.txt" in paths  # committed portion
    assert "seed.txt" in paths       # uncommitted portion
    combined = result.combined
    assert "committed-body" in combined
    assert "uncommitted-change" in combined


def test_collect_merges_committed_and_uncommitted_on_same_file(tmp_path: Path):
    _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "feat/y")
    # Commit a modification to seed.txt on the branch.
    (tmp_path / "seed.txt").write_text("seed\ncommitted-line\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "modify seed.txt")
    # Further uncommitted modification to the same file.
    (tmp_path / "seed.txt").write_text("seed\ncommitted-line\nuncommitted-line\n")

    result = collect_worktree_diff(tmp_path, base_branch="main")
    files = [f for f in result.files if f.path == "seed.txt"]
    assert len(files) == 1
    body = files[0].body
    assert "committed-line" in body
    assert "uncommitted-line" in body
    assert "-- uncommitted --" in body


def test_collect_base_branch_none_matches_legacy_behavior(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\nchanged\n")
    legacy = collect_worktree_diff(tmp_path)
    with_none = collect_worktree_diff(tmp_path, base_branch=None)
    assert legacy.combined == with_none.combined


def test_status_new_file():
    chunk = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+print('hi')\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "N"
    assert f.old_path is None
    assert f.path == "new.py"


def test_status_deleted_file():
    chunk = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-print('bye')\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "D"
    assert f.old_path is None
    assert f.path == "gone.py"


def test_status_modified_file():
    chunk = (
        "diff --git a/mod.py b/mod.py\n"
        "index abc..def 100644\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,1 +1,2 @@\n"
        " keep\n"
        "+add\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "M"
    assert f.old_path is None


def test_status_rename_without_changes():
    chunk = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "R"
    assert f.path == "new.py"
    assert f.old_path == "old.py"


def test_status_rename_with_changes():
    chunk = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 90%\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "@@ -1,1 +1,2 @@\n"
        " keep\n"
        "+add\n"
    )
    (f,) = split_diff_by_file(chunk)
    assert f.status == "R"
    assert f.path == "new.py"
    assert f.old_path == "old.py"
    assert f.additions == 1
