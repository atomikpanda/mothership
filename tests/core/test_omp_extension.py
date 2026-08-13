from __future__ import annotations

import json
import os
import shutil
import subprocess

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


def test_generated_extension_executes_native_lifecycle_contract_under_bun(tmp_path: Path):
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("bun is unavailable")

    install_omp_extension(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_mship = fake_bin / "mship"
    fake_mship.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

event_name = sys.argv[2]
event = json.load(sys.stdin)
with Path("forwarded-events.jsonl").open("a") as stream:
    stream.write(json.dumps(event, separators=(",", ":")) + "\\n")

if event_name == "session_start":
    assert event == {"type": "session_start", "source": "startup"}
    print(json.dumps({"kind": "context", "message": "context"}))
elif event_name == "tool_call":
    assert event["type"] == "tool_call"
    assert event["toolName"] in {"edit", "write"}
    path = event["input"]["path"]
    if path == "src/denied.py":
        print(json.dumps({"kind": "deny", "message": "denied"}))
    elif path == "src/allowed.py":
        print(json.dumps({"kind": "allow"}))
    elif path == "src/invalid-json.py":
        print("not json")
    elif path == "src/nonzero.py":
        raise SystemExit(7)
    else:
        raise AssertionError(path)
elif event_name == "session_stop":
    assert event["type"] == "session_stop"
    if event["stop_hook_active"]:
        print(json.dumps({"kind": "stop"}))
    else:
        print(json.dumps({"kind": "continue", "message": "answer inbox"}))
else:
    raise AssertionError(event_name)
"""
    )
    fake_mship.chmod(0o755)
    harness = tmp_path / "harness.ts"
    harness.write_text(
        """import extension from \"./.omp/extensions/mship.ts\";

const handlers = new Map<string, Function>();
const registrations: string[] = [];
const messages: unknown[] = [];
const warnings: string[] = [];
const pi = {
  on(name: string, handler: Function) {
    registrations.push(name);
    handlers.set(name, handler);
  },
  sendMessage(...args: unknown[]) {
    messages.push(args);
  },
  logger: {
    warn(message: string) {
      warnings.push(message);
    },
  },
};
extension(pi);
const ctx = {cwd: process.cwd()};

await handlers.get("session_start")!({type: "session_start", source: "startup"}, ctx);
const deny = await handlers.get("tool_call")!(
  {type: "tool_call", toolName: "edit", input: {path: "src/denied.py"}},
  ctx,
);
const allowed = await handlers.get("tool_call")!(
  {type: "tool_call", toolName: "write", input: {path: "src/allowed.py"}},
  ctx,
);
const continued = await handlers.get("session_stop")!(
  {type: "session_stop", stop_hook_active: false},
  ctx,
);
const stopped = await handlers.get("session_stop")!(
  {type: "session_stop", stop_hook_active: true},
  ctx,
);
const invalid = await handlers.get("tool_call")!(
  {type: "tool_call", toolName: "write", input: {path: "src/invalid-json.py"}},
  ctx,
);
const nonzero = await handlers.get("tool_call")!(
  {type: "tool_call", toolName: "edit", input: {path: "src/nonzero.py"}},
  ctx,
);

console.log(JSON.stringify({
  registrations,
  messages,
  warnings,
  deny: deny ?? null,
  allowed: allowed ?? null,
  continued: continued ?? null,
  stopped: stopped ?? null,
  invalid: invalid ?? null,
  nonzero: nonzero ?? null,
}));
"""
    )

    result = subprocess.run(
        [bun, str(harness)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    forwarded = [
        json.loads(line)
        for line in (tmp_path / "forwarded-events.jsonl").read_text().splitlines()
    ]
    payload = json.loads(result.stdout)
    assert payload["registrations"] == ["session_start", "tool_call", "session_stop"]
    assert payload["messages"] == [[
        {"customType": "mship-session-context", "content": "context", "display": False},
        {"deliverAs": "nextTurn"},
    ]]
    assert forwarded == [
        {"type": "session_start", "source": "startup"},
        {"type": "tool_call", "toolName": "edit", "input": {"path": "src/denied.py"}},
        {"type": "tool_call", "toolName": "write", "input": {"path": "src/allowed.py"}},
        {"type": "session_stop", "stop_hook_active": False},
        {"type": "session_stop", "stop_hook_active": True},
        {
            "type": "tool_call",
            "toolName": "write",
            "input": {"path": "src/invalid-json.py"},
        },
        {"type": "tool_call", "toolName": "edit", "input": {"path": "src/nonzero.py"}},
    ]
    assert payload["deny"] == {"block": True, "reason": "denied"}
    assert payload["allowed"] is None
    assert payload["continued"] == {
        "continue": True,
        "additionalContext": "answer inbox",
    }
    assert payload["stopped"] is None
    assert payload["invalid"] is None
    assert payload["nonzero"] is None
    assert payload["warnings"] == [
        "Mothership tool_call hook failed open",
        "Mothership tool_call hook failed open",
    ]
