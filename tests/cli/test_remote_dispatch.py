"""`mship run/capture/build --remote[=role]` (Task 5,
specs/2026-07-11-remote-run-machine.md, MOS-191/MOS-203) — the client side
that resolves a run-host role, POSTs to the remote's `/exec/{verb}`, renders
the streamed output live, and (for capture) pulls artifacts home.

Two layers:
  - `core.remote_client.exec_remote` / `_ChunkReader` exercised directly
    against `httpx.MockTransport` (readline/read_exact across chunk
    boundaries, the exit-code hand-off, the capture artifact round-trip,
    connection-failure -> RemoteExecError).
  - The CLI wiring (`mship run/build/capture --remote[=role]`) via
    `typer.testing.CliRunner`, with `httpx.Client` monkeypatched to a
    MockTransport-backed client so no real network/relay is involved. This
    proves: role resolution -> POST with the bearer token, live stdout
    rendering, exit-code mirroring, the capture artifact landing at the
    EXACT local path `cli/capture.py` already uses, a clean (non-traceback)
    error for an unresolvable role, and — the critical regression guard —
    that OMITTING --remote never touches `remote_client`/httpx at all and
    runs the untouched local path.
"""
from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core import remote_client
from mship.core.remote_exec import ARTIFACT_MARKER, EXIT_MARKER
from mship.core.run_host import RunHostConnection, RunHostError, RunHostStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


# --- wire-framing helpers (mirror core/remote_exec.py's contract) ----------


def _frame(lines, exit_code: int, artifact_tar: bytes | None = None) -> bytes:
    body = b"".join(l.encode() if isinstance(l, str) else l for l in lines)
    if artifact_tar is not None:
        body += f"{ARTIFACT_MARKER} {len(artifact_tar)}\n".encode() + artifact_tar
    body += f"{EXIT_MARKER} {exit_code}\n".encode()
    return body


def _chunked(data: bytes, size: int = 7):
    """Split into small, arbitrary-sized pieces — proves the reader doesn't
    depend on a line or the artifact block landing inside one network chunk."""
    for i in range(0, len(data), size):
        yield data[i : i + size]


def _make_tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _recording_handler(recorder: dict, body: bytes, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        recorder["url"] = str(request.url)
        recorder["headers"] = dict(request.headers)
        recorder["json"] = json.loads(request.content)
        return httpx.Response(status, content=_chunked(body))

    return handler


# =============================================================================
# Layer 1: core.remote_client.exec_remote / _ChunkReader, direct
# =============================================================================


def test_chunk_reader_readline_spans_chunk_boundaries():
    body = b"first line\nsecond line\nthird\n"
    reader = remote_client._ChunkReader(iter(_chunked(body, size=3)))
    assert reader.readline() == b"first line\n"
    assert reader.readline() == b"second line\n"
    assert reader.readline() == b"third\n"
    assert reader.readline() is None


def test_chunk_reader_read_exact_spans_chunk_boundaries_and_is_binary_safe():
    # Deliberately includes raw newline bytes inside the "artifact" payload —
    # read_exact must not stop at them the way readline would.
    payload = b"\x89PNG\nnot-a-line-break\nmore\x00bytes"
    body = payload + b"TRAILER"
    reader = remote_client._ChunkReader(iter(_chunked(body, size=4)))
    assert reader.read_exact(len(payload)) == payload
    assert reader.readline() == b"TRAILER"


def test_chunk_reader_read_exact_past_eof_raises():
    reader = remote_client._ChunkReader(iter([b"short"]))
    with pytest.raises(remote_client.RemoteExecError):
        reader.read_exact(100)


def test_exec_remote_posts_expected_url_headers_and_body():
    recorder: dict = {}
    body = _frame(["ok\n"], exit_code=0)
    conn = RunHostConnection(url="http://remote.example", token="tok-xyz")
    printed = []

    code = remote_client.exec_remote(
        verb="run", conn=conn, task="t1", repos=["api"],
        print_fn=printed.append,
        transport=_mock_transport(_recording_handler(recorder, body)),
    )

    assert code == 0
    assert recorder["url"] == "http://remote.example/exec/run"
    assert recorder["headers"]["authorization"] == "Bearer tok-xyz"
    assert recorder["json"] == {"task": "t1", "repos": ["api"], "kind": "all"}
    assert printed == ["ok"]


def test_exec_remote_includes_platform_when_given():
    recorder: dict = {}
    body = _frame(["ok\n"], exit_code=0)
    conn = RunHostConnection(url="http://remote.example", token="tok")

    remote_client.exec_remote(
        verb="capture", conn=conn, task="t1", repos=["app"], platform="ios",
        transport=_mock_transport(_recording_handler(recorder, body)),
        print_fn=lambda _l: None,
    )
    assert recorder["json"]["platform"] == "ios"


def test_exec_remote_renders_lines_live_in_order():
    body = _frame(["one\n", "two\n", "three\n"], exit_code=0)
    conn = RunHostConnection(url="http://h", token="t")
    printed = []

    code = remote_client.exec_remote(
        verb="run", conn=conn, task="t1", repos=["api"], print_fn=printed.append,
        transport=_mock_transport(_recording_handler({}, body)),
    )
    assert code == 0
    assert printed == ["one", "two", "three"]


def test_exec_remote_returns_nonzero_remote_exit_code_not_a_raise():
    body = _frame(["boom\n"], exit_code=7)
    conn = RunHostConnection(url="http://h", token="t")
    code = remote_client.exec_remote(
        verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
        transport=_mock_transport(_recording_handler({}, body)),
    )
    assert code == 7


def test_exec_remote_extracts_artifact_tar_into_captures_dir(tmp_path):
    tar_bytes = _make_tar({"screen.png": b"PNGDATA", "layout.json": b'{"a": 1}'})
    body = _frame(["captured\n"], exit_code=0, artifact_tar=tar_bytes)
    conn = RunHostConnection(url="http://h", token="t")
    out_dir = tmp_path / "captures" / "t1" / "20260711T000000Z-android"

    code = remote_client.exec_remote(
        verb="capture", conn=conn, task="t1", repos=["app"], platform="android",
        captures_dir_for=out_dir, print_fn=lambda _l: None,
        transport=_mock_transport(_recording_handler({}, body)),
    )

    assert code == 0
    assert (out_dir / "screen.png").read_bytes() == b"PNGDATA"
    assert (out_dir / "layout.json").read_bytes() == b'{"a": 1}'


def test_exec_remote_no_artifact_block_when_captures_dir_for_absent():
    """run/build never pass captures_dir_for — nothing to extract, no error."""
    body = _frame(["ok\n"], exit_code=0)
    conn = RunHostConnection(url="http://h", token="t")
    code = remote_client.exec_remote(
        verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
        transport=_mock_transport(_recording_handler({}, body)),
    )
    assert code == 0


def test_exec_remote_connection_failure_raises_remote_exec_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    conn = RunHostConnection(url="http://unreachable.example", token="t")
    with pytest.raises(remote_client.RemoteExecError) as exc_info:
        remote_client.exec_remote(
            verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
            transport=_mock_transport(handler),
        )
    # Task 6: a connection-level failure gets a specific, actionable message
    # ("unreachable via relay"), distinct from a non-2xx HTTP status.
    msg = str(exc_info.value)
    assert "unreachable" in msg
    assert "http://unreachable.example" in msg


def test_exec_remote_non_2xx_raises_remote_exec_error():
    conn = RunHostConnection(url="http://h", token="bad-token")

    def handler(request):
        return httpx.Response(401, content=b"missing or invalid bearer token")

    with pytest.raises(remote_client.RemoteExecError):
        remote_client.exec_remote(
            verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
            transport=_mock_transport(handler),
        )


def test_exec_remote_503_surfaces_not_bootstrapped_message():
    """Task 3's `POST /exec/{verb}` 503s when the remote serve has no
    workspace config wired in — the client must turn that into a specific
    "remote workspace not bootstrapped" message, not a generic HTTP-status
    error, so an operator immediately knows the fix is on the REMOTE side."""
    conn = RunHostConnection(url="http://remote.example", token="t")

    def handler(request):
        return httpx.Response(
            503,
            json={"detail": "remote workspace not bootstrapped: no config wired in"},
        )

    with pytest.raises(remote_client.RemoteExecError) as exc_info:
        remote_client.exec_remote(
            verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
            transport=_mock_transport(handler),
        )
    msg = str(exc_info.value)
    assert "not bootstrapped" in msg


def test_exec_remote_stream_without_exit_sentinel_raises():
    """A dropped connection mid-stream (no trailing __MSHIP_EXIT__) must fail
    loudly rather than silently reporting success."""
    conn = RunHostConnection(url="http://h", token="t")

    def handler(request):
        return httpx.Response(200, content=_chunked(b"partial output\n"))

    with pytest.raises(remote_client.RemoteExecError):
        remote_client.exec_remote(
            verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
            transport=_mock_transport(handler),
        )


# =============================================================================
# Layer 2: CLI wiring — `mship run/build/capture --remote[=role]`
# =============================================================================


def _write_run_workspace(ws: Path, *, run_hosts: list[str], repo_run_host: str | None = None) -> None:
    repo_dir = ws / "api"
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  run:\n    cmds:\n      - echo run\n  build:\n    cmds:\n      - echo build\n"
    )
    run_host_line = f"    run_host: {repo_run_host}\n" if repo_run_host else ""
    (ws / "mothership.yaml").write_text(
        "workspace: t\n"
        f"run_hosts: [{', '.join(run_hosts)}]\n"
        "repos:\n"
        "  api:\n"
        "    path: ./api\n"
        "    type: service\n"
        f"{run_host_line}"
    )


def _write_capture_workspace(ws: Path, *, run_hosts: list[str], platforms: list[str]) -> Path:
    repo_dir = ws / "app"
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks:\n  capture:\n    cmds:\n      - echo ok\n")
    plat = "[" + ", ".join(platforms) + "]"
    (ws / "mothership.yaml").write_text(
        "workspace: t\n"
        f"run_hosts: [{', '.join(run_hosts)}]\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        f"    capture:\n      platforms: {plat}\n"
    )
    wt = ws / "wt"
    wt.mkdir(exist_ok=True)
    return wt


def _seed_task(ws: Path, *, slug: str, repos: list[str], worktrees: dict[str, str] | None = None) -> None:
    StateManager(ws / ".mothership").save(WorkspaceState(tasks={
        slug: Task(
            slug=slug, description="d", phase="dev",
            created_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
            affected_repos=repos, branch=f"feat/{slug}",
            worktrees=worktrees or {}, base_branch="main",
            active_repo=repos[0] if worktrees else None,
        )
    }))


def _configure(ws: Path) -> MagicMock:
    state_dir = ws / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(ws / "mothership.yaml")
    container.state_dir.override(state_dir)
    mock_shell = MagicMock(spec=ShellRunner)
    mock_shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    popen_mock = MagicMock()
    popen_mock.stdout = None
    popen_mock.stderr = None
    popen_mock.wait.return_value = 0
    mock_shell.run_streaming.return_value = popen_mock
    container.shell.override(mock_shell)
    return mock_shell


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override()
    container.config.reset()
    container.state_manager.reset_override()
    container.state_manager.reset()
    container.shell.reset_override()


class _ClientPatch:
    """Monkeypatches `httpx.Client` (as seen through `core.remote_client`) to
    hand back a MockTransport-backed client, standing in for the real
    network/relay hop for CLI-level tests. Restores the real class on exit."""

    def __init__(self, monkeypatch, handler):
        self._monkeypatch = monkeypatch
        self._handler = handler

    def __enter__(self):
        real_client = httpx.Client
        handler = self._handler

        def fake_client(*, transport=None, **kwargs):
            return real_client(transport=httpx.MockTransport(handler))

        self._monkeypatch.setattr(remote_client.httpx, "Client", fake_client)
        return self

    def __exit__(self, *exc):
        return False


def test_cli_run_remote_dispatches_posts_and_streams_live(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    mock_shell = _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    body = _frame(["hello\n", "world\n"], exit_code=0)

    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, body)):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 0, result.output
        assert "hello" in result.output
        assert "world" in result.output
        assert recorder["url"] == "http://remote.example/exec/run"
        assert recorder["headers"]["authorization"] == "Bearer tok-abc"
        assert recorder["json"] == {"task": "t1", "repos": ["api"], "kind": "all"}
        # The local executor was never touched.
        mock_shell.run_streaming.assert_not_called()
        mock_shell.run_task.assert_not_called()
    finally:
        _reset()


def test_cli_run_bare_remote_auto_resolves_sole_run_host(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    body = _frame(["ok\n"], exit_code=0)
    try:
        with _ClientPatch(monkeypatch, _recording_handler({}, body)):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote"])
        assert result.exit_code == 0, result.output
        assert "ok" in result.output
    finally:
        _reset()


def test_cli_run_remote_nonzero_exit_conveyed_as_local_exit(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    body = _frame(["oops\n"], exit_code=3)
    try:
        with _ClientPatch(monkeypatch, _recording_handler({}, body)):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 3
        assert "oops" in result.output
    finally:
        _reset()


def test_cli_build_remote_dispatches_to_run_host(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    mock_shell = _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    body = _frame(["built\n"], exit_code=0)
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, body)):
            result = runner.invoke(app, ["build", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 0, result.output
        assert "built" in result.output
        assert recorder["url"] == "http://remote.example/exec/build"
        mock_shell.run_task.assert_not_called()
    finally:
        _reset()


def test_cli_run_remote_without_resolvable_task_is_clean_error(tmp_path, monkeypatch):
    """Remote run/build always materializes a task's branch — no ad-hoc
    remote run — so --remote with no active/resolvable task must error
    cleanly rather than attempt a remote call with no task."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    try:
        result = runner.invoke(app, ["run", "--remote=role-x"])
        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit) or result.exception is None
        assert "Traceback" not in (result.output or "")
        assert "task" in result.output.lower()
    finally:
        _reset()


def test_cli_capture_remote_extracts_artifacts_into_exact_local_captures_path(tmp_path, monkeypatch):
    """The capture path this test asserts against (.mothership/captures/
    <task>/<UTCts>-<platform>/) must be identical in shape to the LOCAL
    capture path computed at cli/capture.py:104-110 — see
    test_capture_single_platform_implicit in test_capture.py for the local
    counterpart's `/captures/t/` assertion."""
    wt = _write_capture_workspace(tmp_path, run_hosts=["role-x"], platforms=["android"])
    _seed_task(tmp_path, slug="t1", repos=["app"], worktrees={"app": str(wt)})
    mock_shell = _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    tar_bytes = _make_tar({"screen.png": b"PNGDATA", "layout.json": b'{"a": 1}'})
    body = _frame(["captured\n"], exit_code=0, artifact_tar=tar_bytes)

    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, body)):
            result = runner.invoke(
                app, ["capture", "--task", "t1", "--repo", "app", "--remote=role-x"]
            )
        assert result.exit_code == 0, result.output
        assert recorder["url"] == "http://remote.example/exec/capture"
        assert recorder["json"] == {
            "task": "t1", "repos": ["app"], "kind": "all", "platform": "android",
        }

        captures_root = tmp_path / ".mothership" / "captures" / "t1"
        dirs = list(captures_root.glob("*-android"))
        assert len(dirs) == 1, f"expected exactly one <ts>-android dir, got {dirs}"
        out_dir = dirs[0]
        assert (out_dir / "screen.png").read_bytes() == b"PNGDATA"
        assert (out_dir / "layout.json").read_bytes() == b'{"a": 1}'

        # The local capture target never ran.
        mock_shell.run_task.assert_not_called()
    finally:
        _reset()


def test_cli_capture_remote_without_active_task_is_clean_error(tmp_path, monkeypatch):
    """No ad-hoc remote capture: --remote with no active task must be a
    clean, actionable error rather than attempting a taskless remote call."""
    wt = _write_capture_workspace(tmp_path, run_hosts=["role-x"], platforms=["android"])
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir(exist_ok=True)
    container.config.reset()
    container.state_manager.reset()
    container.config_path.override(tmp_path / "mothership.yaml")
    container.state_dir.override(state_dir)
    StateManager(state_dir).save(WorkspaceState(tasks={}))
    container.shell.override(MagicMock(spec=ShellRunner))
    RunHostStore(state_dir).set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    try:
        result = runner.invoke(app, ["capture", "--repo", "app", "--remote=role-x"])
        assert result.exit_code != 0
        assert "Traceback" not in (result.output or "")
        assert "task" in result.output.lower()
    finally:
        _reset()


# --- RunHostError surfaces as a clean CLI error, never a traceback --------


@pytest.mark.parametrize("cli_args_role", [
    ("run", "--remote=role-x"),   # role declared but never mapped locally
])
def test_cli_run_remote_unmapped_role_is_clean_error_not_traceback(tmp_path, monkeypatch, cli_args_role):
    verb, remote_flag = cli_args_role
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    _configure(tmp_path)
    # Deliberately do NOT add "role-x" to the RunHostStore.
    try:
        result = runner.invoke(app, [verb, "--task", "t1", remote_flag])
        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit) or result.exception is None
        assert "Traceback" not in (result.output or "")
        assert "role-x" in result.output
        assert "run-host add role-x" in result.output
    finally:
        _reset()


def test_cli_run_remote_unknown_role_is_clean_error_not_traceback(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    _configure(tmp_path)
    try:
        result = runner.invoke(app, ["run", "--task", "t1", "--remote=ghost-role"])
        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit) or result.exception is None
        assert "Traceback" not in (result.output or "")
        assert "ghost-role" in result.output
    finally:
        _reset()


def test_cli_run_remote_unreachable_host_is_clean_error_not_traceback(tmp_path, monkeypatch):
    """A relay/network-level connect failure must surface as a clean CLI
    error (naming "unreachable") + non-zero exit, never a raw traceback."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    try:
        with _ClientPatch(monkeypatch, handler):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit) or result.exception is None
        assert "Traceback" not in (result.output or "")
        assert "unreachable" in result.output.lower()
    finally:
        _reset()


def test_cli_run_remote_not_bootstrapped_is_clean_error_not_traceback(tmp_path, monkeypatch):
    """A remote serve with no workspace config wired in 503s — the CLI must
    show a specific "not bootstrapped" message, not a generic HTTP error."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )

    def handler(request):
        return httpx.Response(503, json={"detail": "remote workspace not bootstrapped"})

    try:
        with _ClientPatch(monkeypatch, handler):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit) or result.exception is None
        assert "Traceback" not in (result.output or "")
        assert "not bootstrapped" in result.output.lower()
    finally:
        _reset()


def test_cli_capture_remote_unmapped_role_is_clean_error_not_traceback(tmp_path, monkeypatch):
    """The same RunHostError surfacing exercised for run/build above must
    also hold for capture, which resolves the role via its own inline block
    in cli/capture.py rather than the shared `_run_remote` helper."""
    wt = _write_capture_workspace(tmp_path, run_hosts=["role-x"], platforms=["android"])
    _seed_task(tmp_path, slug="t1", repos=["app"], worktrees={"app": str(wt)})
    _configure(tmp_path)
    # Deliberately do NOT add "role-x" to the RunHostStore.
    try:
        result = runner.invoke(
            app, ["capture", "--task", "t1", "--repo", "app", "--remote=role-x"]
        )
        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit) or result.exception is None
        assert "Traceback" not in (result.output or "")
        assert "role-x" in result.output
        assert "run-host add role-x" in result.output
    finally:
        _reset()


def test_cli_run_bare_remote_ambiguous_roles_is_clean_error_not_traceback(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x", "role-y"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://h", token="t"),
    )
    RunHostStore(tmp_path / ".mothership").set(
        "role-y", RunHostConnection(url="http://h2", token="t2"),
    )
    try:
        result = runner.invoke(app, ["run", "--task", "t1", "--remote"])
        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit) or result.exception is None
        assert "Traceback" not in (result.output or "")
        assert "role-x" in result.output and "role-y" in result.output
    finally:
        _reset()


# =============================================================================
# CRITICAL: without --remote, the local path is byte-for-byte unchanged.
# =============================================================================


def test_cli_run_without_remote_never_touches_remote_client_or_httpx(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    mock_shell = _configure(tmp_path)

    exec_remote_spy = MagicMock(side_effect=AssertionError("exec_remote must not be called"))
    monkeypatch.setattr(remote_client, "exec_remote", exec_remote_spy)

    def httpx_client_guard(*a, **kw):
        raise AssertionError("httpx.Client must not be constructed without --remote")

    monkeypatch.setattr(remote_client.httpx, "Client", httpx_client_guard)

    try:
        result = runner.invoke(app, ["run", "--task", "t1"])
        assert result.exit_code == 0, result.output
        assert mock_shell.run_streaming.called
        exec_remote_spy.assert_not_called()
    finally:
        _reset()


def test_cli_build_without_remote_never_touches_remote_client_or_httpx(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task(tmp_path, slug="t1", repos=["api"])
    mock_shell = _configure(tmp_path)

    exec_remote_spy = MagicMock(side_effect=AssertionError("exec_remote must not be called"))
    monkeypatch.setattr(remote_client, "exec_remote", exec_remote_spy)

    def httpx_client_guard(*a, **kw):
        raise AssertionError("httpx.Client must not be constructed without --remote")

    monkeypatch.setattr(remote_client.httpx, "Client", httpx_client_guard)

    try:
        result = runner.invoke(app, ["build", "--task", "t1"])
        assert result.exit_code == 0, result.output
        assert mock_shell.run_task.called
        exec_remote_spy.assert_not_called()
    finally:
        _reset()


def test_cli_capture_without_remote_never_touches_remote_client_or_httpx(tmp_path, monkeypatch):
    wt = _write_capture_workspace(tmp_path, run_hosts=["role-x"], platforms=["android"])
    _seed_task(tmp_path, slug="t1", repos=["app"], worktrees={"app": str(wt)})
    mock_shell = _configure(tmp_path)

    def _run_task_writes_screenshot(task_name, actual_task_name, cwd, env_runner=None, env=None):
        out = Path(env["MSHIP_CAPTURE_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "screen.png").write_bytes(b"PNGDATA")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run_task.side_effect = _run_task_writes_screenshot

    exec_remote_spy = MagicMock(side_effect=AssertionError("exec_remote must not be called"))
    monkeypatch.setattr(remote_client, "exec_remote", exec_remote_spy)

    def httpx_client_guard(*a, **kw):
        raise AssertionError("httpx.Client must not be constructed without --remote")

    monkeypatch.setattr(remote_client.httpx, "Client", httpx_client_guard)

    try:
        result = runner.invoke(app, ["capture", "--task", "t1", "--repo", "app"])
        assert result.exit_code == 0, result.output
        assert mock_shell.run_task.called
        exec_remote_spy.assert_not_called()
        payload = json.loads(result.stdout)
        assert "/captures/t1/" in payload["artifacts"][0]["path"]
    finally:
        _reset()
