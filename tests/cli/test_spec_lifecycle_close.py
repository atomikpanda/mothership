"""Tests for MOS-183: mship close auto-advances bound spec dispatched→implemented."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mship.core.spec_draft import new_spec
from mship.core.spec_store import SpecStore, SPECS_DIRNAME
from mship.core.state import Task, WorkspaceState, StateManager

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_dispatched_spec(specs_dir: Path, task_slug: str) -> str:
    """Create a spec in dispatched state; return its id."""
    specs_dir.mkdir(parents=True, exist_ok=True)
    store = SpecStore(specs_dir)
    spec = new_spec(f"Feature for {task_slug}", now=_NOW, task_slug=task_slug)
    spec.status = "dispatched"
    store.save(spec)
    return spec.id


def _make_task(tmp_path: Path, spec_id: str | None = None, pr_urls: dict | None = None) -> Task:
    return Task(
        slug="mytask",
        description="My task",
        phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["repo-a"],
        branch="feat/mytask",
        spec_id=spec_id,
        pr_urls=pr_urls or {},
    )


class TestSpecLifecycleOnClose:
    def test_close_all_merged_advances_dispatched_spec_to_implemented(self, tmp_path):
        """When all PRs merged and task has a dispatched spec, close advances it to implemented."""
        specs_dir = tmp_path / SPECS_DIRNAME
        spec_id = _make_dispatched_spec(specs_dir, "mytask")
        task = _make_task(tmp_path, spec_id=spec_id, pr_urls={"repo-a": "https://github.com/x/y/pull/1"})

        from mship.core.spec_lifecycle import advance_spec_on_close
        advance_spec_on_close(
            task=task,
            specs_dir=specs_dir,
            merged_count=1,
            closed_count=0,
        )

        store = SpecStore(specs_dir)
        updated = store.find_by_id(spec_id)
        assert updated is not None
        assert updated.status == "implemented"

    def test_close_not_all_merged_does_not_advance(self, tmp_path):
        """When PRs are not all merged, spec stays in dispatched."""
        specs_dir = tmp_path / SPECS_DIRNAME
        spec_id = _make_dispatched_spec(specs_dir, "mytask")
        task = _make_task(tmp_path, spec_id=spec_id)

        from mship.core.spec_lifecycle import advance_spec_on_close
        advance_spec_on_close(
            task=task,
            specs_dir=specs_dir,
            merged_count=0,
            closed_count=1,
        )

        store = SpecStore(specs_dir)
        updated = store.find_by_id(spec_id)
        assert updated is not None
        assert updated.status == "dispatched"

    def test_close_no_spec_id_is_noop(self, tmp_path):
        """If task has no spec_id, advance_spec_on_close is a safe no-op."""
        specs_dir = tmp_path / SPECS_DIRNAME
        specs_dir.mkdir(parents=True)
        task = _make_task(tmp_path, spec_id=None)

        from mship.core.spec_lifecycle import advance_spec_on_close
        # Should not raise
        advance_spec_on_close(
            task=task,
            specs_dir=specs_dir,
            merged_count=1,
            closed_count=0,
        )

    def test_close_non_dispatched_spec_is_not_advanced(self, tmp_path):
        """If bound spec is not in dispatched state (e.g., approved), it is not advanced."""
        specs_dir = tmp_path / SPECS_DIRNAME
        specs_dir.mkdir(parents=True)
        store = SpecStore(specs_dir)
        spec = new_spec("Feature", now=_NOW, task_slug="mytask")
        spec.status = "approved"
        store.save(spec)

        task = _make_task(tmp_path, spec_id=spec.id)

        from mship.core.spec_lifecycle import advance_spec_on_close
        advance_spec_on_close(
            task=task,
            specs_dir=specs_dir,
            merged_count=1,
            closed_count=0,
        )

        updated = store.find_by_id(spec.id)
        assert updated is not None
        assert updated.status == "approved"

    def test_close_missing_spec_file_is_noop(self, tmp_path):
        """If spec_id points to a nonexistent spec, advance_spec_on_close is a safe no-op."""
        specs_dir = tmp_path / SPECS_DIRNAME
        specs_dir.mkdir(parents=True)
        task = _make_task(tmp_path, spec_id="ghost-spec-id")

        from mship.core.spec_lifecycle import advance_spec_on_close
        # Should not raise
        advance_spec_on_close(
            task=task,
            specs_dir=specs_dir,
            merged_count=1,
            closed_count=0,
        )


class TestSpecImplementedCommand:
    """Tests for `mship spec implemented <id>` manual lifecycle command."""

    def _app(self, tmp_path: Path):
        from mship.cli.spec import register
        import typer

        class FakeContainer:
            def config_path(self):
                return str(tmp_path / "mothership.yaml")

        app = typer.Typer()
        register(app, lambda: FakeContainer())
        return app

    def test_spec_implemented_advances_dispatched_spec(self, tmp_path):
        """mship spec implemented <id> advances a dispatched spec to implemented."""
        specs_dir = tmp_path / SPECS_DIRNAME
        spec_id = _make_dispatched_spec(specs_dir, "my-task")

        from typer.testing import CliRunner
        app = self._app(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["spec", "implemented", spec_id])
        assert result.exit_code == 0, result.output

        store = SpecStore(specs_dir)
        updated = store.find_by_id(spec_id)
        assert updated is not None
        assert updated.status == "implemented"

    def test_spec_implemented_unknown_id_exits_nonzero(self, tmp_path):
        """mship spec implemented with unknown id exits non-zero."""
        (tmp_path / SPECS_DIRNAME).mkdir()

        from typer.testing import CliRunner
        app = self._app(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["spec", "implemented", "no-such-id"])
        assert result.exit_code != 0

    def test_spec_implemented_wrong_status_exits_nonzero(self, tmp_path):
        """mship spec implemented on a non-dispatched spec exits non-zero."""
        specs_dir = tmp_path / SPECS_DIRNAME
        specs_dir.mkdir(parents=True)
        store = SpecStore(specs_dir)
        spec = new_spec("Feature", now=_NOW, task_slug="t")
        spec.status = "approved"
        store.save(spec)

        from typer.testing import CliRunner
        app = self._app(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["spec", "implemented", spec.id])
        assert result.exit_code != 0

    def test_spec_implemented_non_tty_returns_json(self, tmp_path):
        """mship spec implemented non-TTY output is valid JSON."""
        import json
        specs_dir = tmp_path / SPECS_DIRNAME
        spec_id = _make_dispatched_spec(specs_dir, "my-task")

        from typer.testing import CliRunner
        app = self._app(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["spec", "implemented", spec_id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == spec_id
        assert data["status"] == "implemented"


class TestSpecArchiveCommand:
    """Tests for `mship spec archive <id>` manual lifecycle command."""

    def _app(self, tmp_path: Path):
        from mship.cli.spec import register
        import typer

        class FakeContainer:
            def config_path(self):
                return str(tmp_path / "mothership.yaml")

        app = typer.Typer()
        register(app, lambda: FakeContainer())
        return app

    def _make_implemented_spec(self, specs_dir: Path) -> str:
        specs_dir.mkdir(parents=True, exist_ok=True)
        store = SpecStore(specs_dir)
        spec = new_spec("Feature", now=_NOW, task_slug="t")
        spec.status = "implemented"
        store.save(spec)
        return spec.id

    def test_spec_archive_advances_implemented_to_archived(self, tmp_path):
        specs_dir = tmp_path / SPECS_DIRNAME
        spec_id = self._make_implemented_spec(specs_dir)

        from typer.testing import CliRunner
        app = self._app(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["spec", "archive", spec_id])
        assert result.exit_code == 0, result.output

        store = SpecStore(specs_dir)
        updated = store.find_by_id(spec_id)
        assert updated is not None
        assert updated.status == "archived"

    def test_spec_archive_unknown_id_exits_nonzero(self, tmp_path):
        (tmp_path / SPECS_DIRNAME).mkdir()
        from typer.testing import CliRunner
        app = self._app(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["spec", "archive", "ghost"])
        assert result.exit_code != 0

    def test_spec_archive_already_archived_exits_nonzero(self, tmp_path):
        """Cannot re-archive an already archived spec."""
        specs_dir = tmp_path / SPECS_DIRNAME
        specs_dir.mkdir(parents=True)
        store = SpecStore(specs_dir)
        spec = new_spec("Feature", now=_NOW, task_slug="t")
        spec.status = "archived"
        store.save(spec)

        from typer.testing import CliRunner
        app = self._app(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["spec", "archive", spec.id])
        assert result.exit_code != 0

    def test_spec_archive_non_tty_returns_json(self, tmp_path):
        import json
        specs_dir = tmp_path / SPECS_DIRNAME
        spec_id = self._make_implemented_spec(specs_dir)

        from typer.testing import CliRunner
        app = self._app(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["spec", "archive", spec_id])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == spec_id
        assert data["status"] == "archived"
