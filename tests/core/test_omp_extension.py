from __future__ import annotations

from pathlib import Path

import pytest

from mship.core.omp_extension import (
    OMP_EXTENSION_MARKER,
    extract_omp_edit_targets,
    install_omp_extension,
)


def test_extracts_omp_write_target():
    event = {"type": "tool_call", "toolName": "write", "input": {"path": "src/a.py"}}
    assert extract_omp_edit_targets(event) == ("src/a.py",)


def test_extracts_all_omp_hashline_edit_targets():
    event = {
        "type": "tool_call",
        "toolName": "edit",
        "input": {"path": "src/a.py", "paths": ["src/a.py", "src/b.py"]},
    }
    assert extract_omp_edit_targets(event) == ("src/a.py", "src/b.py")


def test_unrelated_omp_tool_has_no_targets():
    event = {"type": "tool_call", "toolName": "read", "input": {"path": "src/a.py"}}
    assert extract_omp_edit_targets(event) == ()


@pytest.mark.parametrize(
    "event",
    [
        {"type": "tool_call", "toolName": "edit", "input": {}},
        {"type": "tool_call", "toolName": "write", "input": "bad"},
    ],
)
def test_recognized_omp_edit_without_targets_is_an_adapter_error(event: dict):
    with pytest.raises(ValueError, match="target"):
        extract_omp_edit_targets(event)


def test_install_creates_native_omp_extension(tmp_path: Path):
    result = install_omp_extension(tmp_path)

    source = (tmp_path / ".omp" / "extensions" / "mship.ts").read_text()
    assert result.status == "installed"
    assert OMP_EXTENSION_MARKER in source
    assert 'pi.on("session_start"' in source
    assert 'pi.on("tool_call"' in source
    assert 'pi.on("session_stop"' in source
    assert 'deliverAs: "nextTurn"' in source
    assert "block: true" in source
    assert "continue: true" in source
    assert "additionalContext" in source


def test_install_is_byte_idempotent(tmp_path: Path):
    install_omp_extension(tmp_path)
    path = tmp_path / ".omp" / "extensions" / "mship.ts"
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    result = install_omp_extension(tmp_path)

    assert result.status == "up to date"
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_install_replaces_only_mothership_extension(tmp_path: Path):
    extensions = tmp_path / ".omp" / "extensions"
    extensions.mkdir(parents=True)
    sibling = extensions / "user.ts"
    sibling.write_text("export default () => {};\n")
    target = extensions / "mship.ts"
    target.write_text("// stale mship extension\n")

    result = install_omp_extension(tmp_path)

    assert result.status == "updated"
    assert OMP_EXTENSION_MARKER in target.read_text()
    assert sibling.read_text() == "export default () => {};\n"


def test_atomic_write_failure_preserves_existing_extension(tmp_path: Path, monkeypatch):
    target = tmp_path / ".omp" / "extensions" / "mship.ts"
    target.parent.mkdir(parents=True)
    target.write_text("// existing\n")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("mship.core.omp_extension._atomic_write", fail)

    with pytest.raises(OSError, match="disk full"):
        install_omp_extension(tmp_path)
    assert target.read_text() == "// existing\n"
