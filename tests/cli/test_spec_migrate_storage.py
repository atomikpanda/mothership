import subprocess
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread

import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.spec_store import SpecStore
from mship.core.spec_storage import SpecStorage

runner = CliRunner()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


@pytest.fixture
def workspace(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "mothership.yaml").write_text("workspace: demo\nspec_storage: committed\n")
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    container.config.reset(); container.state_manager.reset(); container.log_manager.reset()
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    # Create + commit a committed spec.
    runner.invoke(app, ["spec", "new", "--title", "Design X", "--id", "design-x"])
    _git(tmp_path, "add", "specs")
    _git(tmp_path, "commit", "-q", "-m", "add spec")
    yield tmp_path
    container.config_path.reset_override(); container.state_dir.reset_override(); container.config.reset()
    container.state_manager.reset(); container.log_manager.reset()


def _set_mode(root: Path, mode: str) -> None:
    (root / "mothership.yaml").write_text(f"workspace: demo\nspec_storage: {mode}\n")
    container.config.reset()


def test_migrate_committed_to_encrypted(workspace: Path):
    _set_mode(workspace, "encrypted")
    res = runner.invoke(app, ["spec", "migrate-storage"])
    assert res.exit_code == 0, res.output
    specs = workspace / "specs"
    enc = list(specs.glob("*.md.enc"))
    assert len(enc) == 1 and b"Design X" not in enc[0].read_bytes()
    # Plaintext removed from disk AND from the git index.
    assert list(specs.glob("*.md")) == []
    tracked = _git(workspace, "ls-files", "specs").stdout
    assert "design-x.md" not in tracked or "design-x.md.enc" in tracked
    assert ".enc" in _git(workspace, "ls-files", "specs").stdout


def test_migrate_committed_to_local(workspace: Path):
    _set_mode(workspace, "local")
    res = runner.invoke(app, ["spec", "migrate-storage"])
    assert res.exit_code == 0, res.output
    md = list((workspace / "specs").glob("*.md"))
    assert len(md) == 1 and "Design X" in md[0].read_text()  # still readable locally
    # Untracked + gitignored now.
    assert not any("design-x" in path for path in _git(workspace, "ls-files", "specs").stdout.splitlines())
    check = subprocess.run(
        ["git", "check-ignore", "-q", str(md[0].relative_to(workspace))], cwd=workspace
    )
    assert check.returncode == 0


def test_migrate_encrypted_back_to_committed(workspace: Path):
    _set_mode(workspace, "encrypted")
    runner.invoke(app, ["spec", "migrate-storage"])
    _set_mode(workspace, "committed")
    res = runner.invoke(app, ["spec", "migrate-storage"])
    assert res.exit_code == 0, res.output
    md = list((workspace / "specs").glob("*.md"))
    assert len(md) == 1 and "Design X" in md[0].read_text()
    assert list((workspace / "specs").glob("*.md.enc")) == []  # ciphertext gone


def test_migration_holds_spec_lock_until_mutation_can_write_target(
    workspace: Path, monkeypatch,
):
    _set_mode(workspace, "encrypted")
    store = SpecStore(workspace / "specs")
    mutation_finished = Event()
    mutation_started = Event()
    original_write = SpecStorage.write
    writer: Thread | None = None

    def mutate() -> None:
        store.mutate_inbox(
            "design-x", "archive", "archive-during-migration",
            datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        mutation_finished.set()

    def interleave_migration(storage, path, text):
        nonlocal writer
        if storage.mode == "encrypted" and writer is None:
            writer = Thread(target=mutate)
            writer.start()
            mutation_started.set()
            assert not mutation_finished.wait(timeout=0.1)
        return original_write(storage, path, text)

    monkeypatch.setattr(SpecStorage, "write", interleave_migration)
    res = runner.invoke(app, ["spec", "migrate-storage"])
    assert res.exit_code == 0, res.output
    assert mutation_started.is_set()
    assert writer is not None
    writer.join(timeout=1)
    assert mutation_finished.is_set()
    assert store.find_by_id("design-x").inbox.manual_archived is True
