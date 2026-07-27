"""The `task setup` cache key on a run host.

A cache key of exactly the shape any build cache uses — and with the same
failure mode, which is precisely why the inputs are DECLARED rather than
guessed: an operator can see and widen a declared key, and cannot inspect a
heuristic.
"""
from pathlib import Path

from mship.core.remote_setup import (
    FIRST_MATERIALIZATION_KEY,
    key_file,
    needs_setup,
    record_setup,
    setup_key,
)


def _worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "package.json").write_text('{"deps": 1}\n')
    (wt / "src" / "app.js").write_text("console.log(1)\n")
    return wt


def test_no_declared_inputs_gives_the_first_materialization_key(tmp_path):
    """ac17: nothing to invalidate against, so the key never changes."""
    assert setup_key(_worktree(tmp_path), []) == FIRST_MATERIALIZATION_KEY


def test_the_key_changes_when_a_declared_input_changes(tmp_path):
    """ac16: a dependency change pays once."""
    wt = _worktree(tmp_path)
    before = setup_key(wt, ["package.json"])
    (wt / "package.json").write_text('{"deps": 2}\n')
    assert setup_key(wt, ["package.json"]) != before


def test_the_key_is_unchanged_by_a_source_only_edit(tmp_path):
    """ac16, the case the whole feature exists for: the common iteration pays
    nothing."""
    wt = _worktree(tmp_path)
    before = setup_key(wt, ["package.json"])
    (wt / "src" / "app.js").write_text("console.log(2)\n")
    assert setup_key(wt, ["package.json"]) == before


def test_globs_are_matched_inside_the_worktree(tmp_path):
    wt = _worktree(tmp_path)
    (wt / "src" / "package.json").write_text('{"nested": 1}\n')
    before = setup_key(wt, ["**/package.json"])
    (wt / "src" / "package.json").write_text('{"nested": 2}\n')
    assert setup_key(wt, ["**/package.json"]) != before


def test_a_declared_input_that_does_not_exist_is_not_an_error(tmp_path):
    """A repo that declares `uv.lock` before it has one must still run."""
    assert setup_key(_worktree(tmp_path), ["uv.lock"])


def test_adding_a_file_a_pattern_matches_moves_the_key(tmp_path):
    wt = _worktree(tmp_path)
    before = setup_key(wt, ["*.lock"])
    (wt / "uv.lock").write_text("locked\n")
    assert setup_key(wt, ["*.lock"]) != before


def test_widening_the_declaration_moves_the_key_by_itself(tmp_path):
    """The declaration is part of the key, so an operator who widens
    `setup_inputs` gets a re-run rather than a stale skip."""
    wt = _worktree(tmp_path)
    assert setup_key(wt, ["package.json"]) != setup_key(wt, ["package.json", "*.lock"])


def test_deleting_a_declared_input_moves_the_key(tmp_path):
    """The too-lazy failure named in the task: a dependency file the operator
    deleted between runs must not be silently treated as unchanged just
    because the glob no longer sees it — its absence IS the change."""
    wt = _worktree(tmp_path)
    before = setup_key(wt, ["package.json"])
    (wt / "package.json").unlink()
    assert setup_key(wt, ["package.json"]) != before


def test_a_content_identical_rename_still_moves_the_key(tmp_path):
    """Hashing only file bytes (and not the path) would miss a rename: same
    bytes, different location, key must still move — a setup script that
    reads a manifest by its old name would otherwise run against a target
    that silently vanished."""
    wt = _worktree(tmp_path)
    (wt / "package.json").write_text("fixed-content\n")
    before = setup_key(wt, ["package*.json"])
    (wt / "package.json").rename(wt / "package2.json")
    assert setup_key(wt, ["package*.json"]) != before


def test_path_and_content_cannot_be_confused_across_a_boundary(tmp_path):
    """Concatenating `path + content` with no separator would make file `a`
    with content `b` indistinguishable from file `ab` with empty content —
    two genuinely different worktree states collapsing onto the same key is
    exactly the too-lazy failure this key exists to rule out."""
    wt1 = tmp_path / "wt1"
    wt1.mkdir()
    (wt1 / "a").write_text("b")
    wt2 = tmp_path / "wt2"
    wt2.mkdir()
    (wt2 / "ab").write_text("")
    assert setup_key(wt1, ["*"]) != setup_key(wt2, ["*"])


def test_every_declared_pattern_contributes_not_just_the_last(tmp_path):
    """A change matched by an earlier pattern in the list must move the key
    even though a later pattern in the same declaration matches nothing — a
    bug that only tracked the final pattern would mask it."""
    wt = _worktree(tmp_path)
    before = setup_key(wt, ["package.json", "*.lock"])
    (wt / "package.json").write_text('{"deps": 2}\n')
    assert setup_key(wt, ["package.json", "*.lock"]) != before


def test_setup_key_is_deterministic_for_an_unchanged_worktree(tmp_path):
    """Same declared inputs, same on-disk state, called twice: the key must
    not depend on filesystem iteration order happening to differ between
    calls."""
    wt = _worktree(tmp_path)
    assert setup_key(wt, ["package.json", "**/*.js"]) == setup_key(
        wt, ["package.json", "**/*.js"]
    )


def test_needs_setup_is_true_when_nothing_was_recorded(tmp_path):
    assert needs_setup(tmp_path / "absent.key", "abc")


def test_recording_then_asking_again_says_no(tmp_path):
    path = key_file(tmp_path, "t1", "api")
    record_setup(path, "abc")
    assert not needs_setup(path, "abc")
    assert needs_setup(path, "different")


def test_the_key_file_path_is_per_task_and_per_repo(tmp_path):
    assert key_file(tmp_path, "t1", "api") != key_file(tmp_path, "t2", "api")
    assert key_file(tmp_path, "t1", "api") != key_file(tmp_path, "t1", "web")


def test_a_task_name_cannot_escape_the_state_directory(tmp_path):
    """The task name arrives over the wire, where `core/serve.py` permits `/`
    and `.`, so `../..` would otherwise be a legal path component here."""
    path = key_file(tmp_path, "../../etc", "api")
    root = (tmp_path / ".mothership").resolve()
    assert root in path.resolve().parents
