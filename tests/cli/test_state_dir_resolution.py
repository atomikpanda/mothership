import os
import subprocess
from pathlib import Path

import pytest


def test_resolve_state_dir_in_main_repo(tmp_path: Path):
    """In a plain git repo, state dir is <repo>/.mothership."""
    from mship.cli import _resolve_state_dir

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    config_path = tmp_path / "mothership.yaml"
    config_path.write_text("workspace: t\nrepos: {}\n")

    state_dir = _resolve_state_dir(config_path)
    assert state_dir == tmp_path / ".mothership"


def test_resolve_state_dir_in_worktree(tmp_path: Path):
    """From inside a worktree, state dir anchors to the MAIN repo, not the worktree."""
    from mship.cli import _resolve_state_dir

    main = tmp_path / "main"
    main.mkdir()
    (main / "mothership.yaml").write_text("workspace: t\nrepos: {}\n")

    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=main, check=True, capture_output=True, env=git_env)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=main, check=True, capture_output=True, env=git_env,
    )

    # Create a worktree
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt)],
        cwd=main, check=True, capture_output=True, env=git_env,
    )

    # Config in the worktree (checked out from git)
    wt_config = wt / "mothership.yaml"
    assert wt_config.exists()

    # From main: state dir is main/.mothership
    assert _resolve_state_dir(main / "mothership.yaml") == main / ".mothership"

    # From worktree: state dir STILL anchored to main
    assert _resolve_state_dir(wt_config) == main / ".mothership"


def test_resolve_state_dir_not_a_git_repo(tmp_path: Path):
    """If the directory is not a git repo, fall back to config path parent."""
    from mship.cli import _resolve_state_dir

    config_path = tmp_path / "mothership.yaml"
    config_path.write_text("workspace: t\nrepos: {}\n")

    state_dir = _resolve_state_dir(config_path)
    assert state_dir == tmp_path / ".mothership"
