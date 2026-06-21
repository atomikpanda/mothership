"""Tests for mship.core.capture."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from mship.core.capture import (
    Artifact, CaptureError, resolve_kinds, discover_artifacts, run_capture,
)


def test_resolve_kinds_all_and_single():
    assert resolve_kinds("all") == ["image", "layout"]
    assert resolve_kinds("image") == ["image"]
    assert resolve_kinds("layout") == ["layout"]


def test_resolve_kinds_unknown_raises():
    with pytest.raises(CaptureError, match="unknown kind"):
        resolve_kinds("video")


def test_discover_artifacts_finds_image_and_layout(tmp_path):
    (tmp_path / "screen.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "layout.xml").write_text("<hierarchy/>")
    arts = discover_artifacts(tmp_path, ["image", "layout"])
    kinds = {a.kind: a.path for a in arts}
    assert kinds["image"] == tmp_path / "screen.png"
    assert kinds["layout"] == tmp_path / "layout.xml"


def test_discover_artifacts_skips_empty_and_missing(tmp_path):
    (tmp_path / "screen.png").write_bytes(b"")
    assert discover_artifacts(tmp_path, ["image", "layout"]) == []


@dataclass
class _FakeResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class _FakeShell:
    def __init__(self, returncode=0, writes: dict[str, bytes] | None = None, stderr=""):
        self.returncode = returncode
        self.writes = writes or {}
        self.stderr = stderr
        self.calls = []

    def run_task(self, task_name, actual_task_name, cwd, env_runner=None, env=None):
        self.calls.append(dict(task_name=task_name, actual=actual_task_name, cwd=cwd, env_runner=env_runner, env=env))
        out = Path(env["MSHIP_CAPTURE_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        for name, data in self.writes.items():
            (out / name).write_bytes(data)
        return _FakeResult(returncode=self.returncode, stderr=self.stderr)


def test_run_capture_success_returns_artifacts(tmp_path):
    shell = _FakeShell(returncode=0, writes={"screen.png": b"PNGDATA"})
    arts = run_capture(
        shell=shell, worktree=tmp_path / "wt", actual_task_name="capture",
        env_runner=None, platform="android", kinds=["image"], out_dir=tmp_path / "out",
    )
    assert [a.kind for a in arts] == ["image"]
    env = shell.calls[0]["env"]
    assert env["MSHIP_CAPTURE_PLATFORM"] == "android"
    assert env["MSHIP_CAPTURE_KINDS"] == "image"
    assert env["MSHIP_CAPTURE_DIR"] == str(tmp_path / "out")
    assert shell.calls[0]["actual"] == "capture"


def test_run_capture_target_failure_raises_with_stderr(tmp_path):
    shell = _FakeShell(returncode=1, stderr="adb: no devices")
    with pytest.raises(CaptureError, match="adb: no devices"):
        run_capture(
            shell=shell, worktree=tmp_path, actual_task_name="capture",
            env_runner=None, platform="android", kinds=["image"], out_dir=tmp_path / "o",
        )


def test_run_capture_no_artifact_raises(tmp_path):
    shell = _FakeShell(returncode=0, writes={})
    with pytest.raises(CaptureError, match="no recognized artifact"):
        run_capture(
            shell=shell, worktree=tmp_path, actual_task_name="capture",
            env_runner=None, platform=None, kinds=["image"], out_dir=tmp_path / "o",
        )


def test_run_capture_omits_platform_env_when_none(tmp_path):
    shell = _FakeShell(returncode=0, writes={"screen.png": b"x"})
    run_capture(
        shell=shell, worktree=tmp_path, actual_task_name="capture",
        env_runner=None, platform=None, kinds=["image"], out_dir=tmp_path / "o",
    )
    assert "MSHIP_CAPTURE_PLATFORM" not in shell.calls[0]["env"]
