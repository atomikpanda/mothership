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
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from mship.cli import app, container
from mship.core import remote_client
from mship.core.remote_exec import ARTIFACT_MARKER, EXIT_MARKER
from mship.core.run_host import RunHostConnection, RunHostError, RunHostStore
from mship.core.spec import AcceptanceCriterion, Spec
from mship.core.spec_store import SpecStore
from mship.core.state import StateManager, Task, WorkspaceState
from mship.util.shell import ShellResult, ShellRunner

runner = CliRunner()


# --- wire-framing helpers (mirror core/remote_exec.py's contract) ----------

# A per-request anti-spoof nonce (server generates a fresh one; here it's fixed
# so the framed body and the response header agree). Control records are only
# recognized when tagged with the nonce — see core/remote_exec.py / remote_client.
NONCE = "nonceabcdef012345"
NONCE_HEADER = "X-Mship-Exec-Nonce"


def _frame(lines, exit_code: int, artifact_tar: bytes | None = None, *, nonce: str = NONCE) -> bytes:
    body = b"".join(l.encode() if isinstance(l, str) else l for l in lines)
    if artifact_tar is not None:
        body += f"{ARTIFACT_MARKER}:{nonce} {len(artifact_tar)}\n".encode() + artifact_tar
    body += f"{EXIT_MARKER}:{nonce} {exit_code}\n".encode()
    return body


def _chunked(data: bytes, size: int = 7):
    """Split into small, arbitrary-sized pieces — proves the reader doesn't
    depend on a line or the artifact block landing inside one network chunk."""
    for i in range(0, len(data), size):
        yield data[i : i + size]


def _make_tar(files: dict[str, bytes], *, mode: str = "w") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _recording_handler(recorder: dict, body: bytes, status: int = 200, *, nonce: str | None = NONCE):
    def handler(request: httpx.Request) -> httpx.Response:
        recorder["url"] = str(request.url)
        recorder["headers"] = dict(request.headers)
        recorder["json"] = json.loads(request.content)
        headers = {NONCE_HEADER: nonce} if nonce is not None else {}
        return httpx.Response(status, content=_chunked(body), headers=headers)

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


def test_exec_remote_survives_quiet_build_then_returns_remote_result():
    """A real socket must survive more than HTTPX's default five idle seconds."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header(NONCE_HEADER, NONCE)
            self.end_headers()
            self.wfile.write(b"building\n")
            self.wfile.flush()
            time.sleep(6)
            try:
                self.wfile.write(_frame(["build failed\n"], exit_code=7))
                self.wfile.flush()
            except BrokenPipeError:
                # The unfixed client disconnects before the build finishes.
                pass

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.handle_request, daemon=True)
        worker.start()
        printed = []
        try:
            code = remote_client.exec_remote(
                verb="run",
                conn=RunHostConnection(
                    url=f"http://127.0.0.1:{server.server_port}", token="test",
                ),
                task="quiet-build", repos=["app"], print_fn=printed.append,
            )
        finally:
            worker.join(timeout=10)
    assert code == 7
    assert printed == ["building", "build failed"]


def test_exec_remote_bounds_stalled_error_body():
    """An HTTP error body is not a live execution stream and must time out."""
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(503)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.flush()
            release.wait(timeout=7)

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.handle_request, daemon=True)
        worker.start()
        try:
            with pytest.raises(remote_client.RemoteExecError) as exc_info:
                remote_client.exec_remote(
                    verb="run",
                    conn=RunHostConnection(
                        url=f"http://127.0.0.1:{server.server_port}", token="test",
                    ),
                    task="stalled-error", repos=["app"], print_fn=lambda _: None,
                )
        finally:
            release.set()
            worker.join(timeout=10)
    assert isinstance(exc_info.value.__cause__, httpx.ReadTimeout)


def test_exec_remote_stream_without_exit_sentinel_raises():
    """A dropped connection mid-stream (no trailing __MSHIP_EXIT__) must fail
    loudly rather than silently reporting success. Nonce header IS present so
    this exercises the no-sentinel path specifically (not missing-nonce)."""
    conn = RunHostConnection(url="http://h", token="t")

    def handler(request):
        return httpx.Response(
            200, content=_chunked(b"partial output\n"),
            headers={NONCE_HEADER: NONCE},
        )

    with pytest.raises(remote_client.RemoteExecError):
        remote_client.exec_remote(
            verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
            transport=_mock_transport(handler),
        )


def test_exec_remote_missing_nonce_header_raises():
    """FIX 2: without the X-Mship-Exec-Nonce response header the client can't
    tell a real control record from spoofed task output — it must refuse
    (RemoteExecError), not proceed."""
    body = _frame(["ok\n"], exit_code=0)
    conn = RunHostConnection(url="http://h", token="t")
    with pytest.raises(remote_client.RemoteExecError) as exc:
        remote_client.exec_remote(
            verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
            transport=_mock_transport(_recording_handler({}, body, nonce=None)),
        )
    assert "nonce" in str(exc.value).lower()


def test_exec_remote_task_stdout_cannot_spoof_exit_code():
    """FIX 2 (anti-spoof): a task whose stdout literally contains a bare
    `__MSHIP_EXIT__ 0` (WITHOUT the per-request nonce) is treated as ordinary
    output — the real, nonce-tagged exit code (7 here) still governs. Without
    the nonce tag, a compromised/failing remote branch could otherwise
    green-wash a failed build by printing `__MSHIP_EXIT__ 0`."""
    spoof = f"{EXIT_MARKER} 0\n"  # note: NO ":<nonce>" — this is the spoof
    body = _frame([spoof, "real work\n"], exit_code=7)
    conn = RunHostConnection(url="http://h", token="t")
    printed: list[str] = []

    code = remote_client.exec_remote(
        verb="run", conn=conn, task="t1", repos=["api"], print_fn=printed.append,
        transport=_mock_transport(_recording_handler({}, body)),
    )
    assert code == 7  # the real nonce-tagged exit governs, not the spoof
    assert f"{EXIT_MARKER} 0" in printed  # the spoof was just printed as output


def test_exec_remote_over_cap_artifact_count_errors_without_reading(tmp_path):
    """FIX 4: an advertised artifact count over MAX_ARTIFACT_BYTES is rejected
    BEFORE the bytes are read (no unbounded allocation). We advertise a huge
    count but supply none of the bytes — proof the cap check short-circuits
    ahead of read_exact."""
    huge = remote_client.MAX_ARTIFACT_BYTES + 1
    body = (
        b"working\n"
        + f"{ARTIFACT_MARKER}:{NONCE} {huge}\n".encode()
        + f"{EXIT_MARKER}:{NONCE} 0\n".encode()
    )
    conn = RunHostConnection(url="http://h", token="t")
    out_dir = tmp_path / "captures"
    with pytest.raises(remote_client.RemoteExecError) as exc:
        remote_client.exec_remote(
            verb="capture", conn=conn, task="t1", repos=["app"],
            captures_dir_for=out_dir, print_fn=lambda _l: None,
            transport=_mock_transport(_recording_handler({}, body)),
        )
    assert "cap" in str(exc.value).lower()
    assert not out_dir.exists()  # nothing was extracted


def test_exec_remote_negative_artifact_count_errors_without_reading(tmp_path):
    """FIX B: a remote advertising a NEGATIVE artifact byte count must be
    rejected BEFORE any read — otherwise `read_exact(-1)` desyncs the stream
    (a negative slice length reads nothing yet advances past nothing, leaving
    the exit sentinel unparsed / the buffer corrupt). We advertise -1 and
    supply none of the bytes; nothing is read or extracted."""
    body = (
        b"working\n"
        + f"{ARTIFACT_MARKER}:{NONCE} -1\n".encode()
        + f"{EXIT_MARKER}:{NONCE} 0\n".encode()
    )
    conn = RunHostConnection(url="http://h", token="t")
    out_dir = tmp_path / "captures"
    with pytest.raises(remote_client.RemoteExecError) as exc:
        remote_client.exec_remote(
            verb="capture", conn=conn, task="t1", repos=["app"],
            captures_dir_for=out_dir, print_fn=lambda _l: None,
            transport=_mock_transport(_recording_handler({}, body)),
        )
    assert "negative" in str(exc.value).lower()
    assert not out_dir.exists()  # nothing was extracted


def test_exec_remote_compressed_tar_is_rejected(tmp_path):
    """FIX 4: the server only ever writes an UNCOMPRESSED tar (mode="w"); the
    client opens mode="r:" so a gzip/xz "tar bomb" raises tarfile.ReadError,
    wrapped as a clean RemoteExecError rather than being transparently
    decompressed."""
    gz_tar = _make_tar({"screen.png": b"PNGDATA"}, mode="w:gz")
    body = _frame(["captured\n"], exit_code=0, artifact_tar=gz_tar)
    conn = RunHostConnection(url="http://h", token="t")
    out_dir = tmp_path / "captures"
    with pytest.raises(remote_client.RemoteExecError) as exc:
        remote_client.exec_remote(
            verb="capture", conn=conn, task="t1", repos=["app"],
            captures_dir_for=out_dir, print_fn=lambda _l: None,
            transport=_mock_transport(_recording_handler({}, body)),
        )
    assert "tar" in str(exc.value).lower()


def test_exec_remote_malformed_control_count_raises_clean_error():
    """FIX 5: a nonce-tagged control record with a non-numeric count must
    surface as a clean RemoteExecError ("malformed control record"), not an
    uncaught ValueError traceback."""
    body = f"{EXIT_MARKER}:{NONCE} notanumber\n".encode()
    conn = RunHostConnection(url="http://h", token="t")
    with pytest.raises(remote_client.RemoteExecError) as exc:
        remote_client.exec_remote(
            verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
            transport=_mock_transport(_recording_handler({}, body)),
        )
    assert "malformed" in str(exc.value).lower()


# =============================================================================
# Layer 2: CLI wiring — `mship run/build/capture --remote[=role]`
# =============================================================================


def _write_run_workspace(
    ws: Path, *, run_hosts: list[str], repo_run_host: str | None = None,
    repos: list[str] = ["api"],
) -> None:
    run_host_line = f"    run_host: {repo_run_host}\n" if repo_run_host else ""
    blocks = ""
    for name in repos:
        repo_dir = ws / name
        repo_dir.mkdir(exist_ok=True)
        (repo_dir / "Taskfile.yml").write_text(
            "version: '3'\ntasks:\n  run:\n    cmds:\n      - echo run\n"
            "  build:\n    cmds:\n      - echo build\n"
        )
        blocks += f"  {name}:\n    path: ./{name}\n    type: service\n{run_host_line}"
    (ws / "mothership.yaml").write_text(
        "workspace: t\n"
        f"run_hosts: [{', '.join(run_hosts)}]\n"
        "repos:\n"
        f"{blocks}"
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
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git())
    container.shell.override(shell)
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
        shell.run_streaming.assert_not_called()
        shell.run_task.assert_not_called()
    finally:
        container.shell.reset_override()
        _reset()


def test_cli_run_bare_remote_auto_resolves_sole_run_host(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    container.shell.override(_git_shell(_repo_git()))
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
        container.shell.reset_override()
        _reset()


def test_cli_run_remote_nonzero_exit_conveyed_as_local_exit(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    container.shell.override(_git_shell(_repo_git()))
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
        container.shell.reset_override()
        _reset()


def test_cli_build_remote_dispatches_to_run_host(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git())
    container.shell.override(shell)
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
        shell.run_task.assert_not_called()
    finally:
        container.shell.reset_override()
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

        # FIX 9: a successful remote capture prints the SAME success
        # confirmation a local capture does (JSON mode here, since CliRunner
        # captures a pipe), pointing at the local landing path — not a silent
        # exit. Mirrors the local path's JSON payload shape.
        payload_marker = '"artifacts"'
        assert payload_marker in result.output
        assert str(out_dir / "screen.png") in result.output
        assert '"resolved_task"' in result.output

        # The local capture target never ran.
        mock_shell.run_task.assert_not_called()
    finally:
        _reset()


def test_cli_capture_remote_with_evidence_attaches_artifact_indistinguishably_from_local(
    tmp_path, monkeypatch,
):
    """ac15 (specs/2026-07-26-artifact-evidence-on-phone.md): `mship capture --remote
    --evidence` must attach evidence from artifacts produced on the mapped run
    host indistinguishably from a local capture. `_attach_evidence`
    (cli/capture.py) is wired into the `--remote` branch AFTER `exec_remote`
    returns, once the extracted artifacts are re-discovered locally as
    `landed` — this proves that wiring actually runs and produces the same
    criterion.evidence shape (kind=artifact, content-hashed `.png` ref, a
    provenance note) that
    test_capture_evidence.py::test_evidence_attaches_to_the_named_criterion
    asserts for the LOCAL path."""
    wt = _write_capture_workspace(tmp_path, run_hosts=["role-x"], platforms=["android"])
    _seed_task(tmp_path, slug="t1", repos=["app"], worktrees={"app": str(wt)})
    mock_shell = _configure(tmp_path)

    # provenance_note() shells out through the same container.shell() the
    # remote path already uses — always against the LOCAL, task-bound
    # worktree `wt` (the remote only supplies the artifact bytes).
    def _fake_run(command, cwd=None, **kwargs):
        assert isinstance(command, str), "ShellRunner.run takes a command string"
        if command.startswith("git rev-parse"):
            return ShellResult(returncode=0, stdout="abc1234\n", stderr="")
        if command.startswith("git branch"):
            return ShellResult(returncode=0, stdout="* main\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")  # git status: clean

    mock_shell.run.side_effect = _fake_run

    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    SpecStore(tmp_path / "specs").save(Spec(
        id="dq", title="Dequeue", status="approved",
        created_at=now, updated_at=now, affected_repos=["app"],
        acceptance_criteria=[AcceptanceCriterion(id="ac1", text="The card clears.")],
    ))

    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    tar_bytes = _make_tar({"screen.png": b"PNGDATA"})
    body = _frame(["captured\n"], exit_code=0, artifact_tar=tar_bytes)

    try:
        with _ClientPatch(monkeypatch, _recording_handler({}, body)):
            result = runner.invoke(
                app, [
                    "capture", "--task", "t1", "--repo", "app", "--remote=role-x",
                    "--evidence", "dq:ac1",
                ]
            )
        assert result.exit_code == 0, result.output

        spec = SpecStore(tmp_path / "specs").find_by_id("dq")
        crit = spec.acceptance_criteria[0]
        assert len(crit.evidence) == 1
        ev = crit.evidence[0]
        # Same shape a LOCAL --evidence capture produces.
        assert ev.kind == "artifact"
        assert ev.ref.endswith(".png")
        assert "at " in (ev.note or "")

        stored = tmp_path / ".mothership" / "evidence" / "dq" / ev.ref
        assert stored.read_bytes() == b"PNGDATA"

        mock_shell.run_task.assert_not_called()  # the local capture target never ran
    finally:
        _reset()


def test_cli_capture_remote_exit0_but_no_artifact_is_hard_error(tmp_path, monkeypatch):
    """FIX C (defense-in-depth): a stale/older remote may return exit 0 with
    NO artifact block. Local capture treats "success with no recognized
    artifact" as a HARD error; the client must enforce the same INDEPENDENTLY
    (on top of the server-side check) rather than exiting success. So a code-0
    remote capture whose landing dir has no artifacts fails non-zero with the
    same no-artifact message."""
    wt = _write_capture_workspace(tmp_path, run_hosts=["role-x"], platforms=["android"])
    _seed_task(tmp_path, slug="t1", repos=["app"], worktrees={"app": str(wt)})
    mock_shell = _configure(tmp_path)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    # Exit 0 with NO artifact tar block — the failure mode this guards.
    body = _frame(["captured\n"], exit_code=0)
    try:
        with _ClientPatch(monkeypatch, _recording_handler({}, body)):
            result = runner.invoke(
                app, ["capture", "--task", "t1", "--repo", "app", "--remote=role-x"]
            )
        assert result.exit_code != 0, result.output
        assert "no recognized artifact" in result.output.lower()
        assert "Traceback" not in (result.output or "")
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
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    container.shell.override(_git_shell(_repo_git()))
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
        container.shell.reset_override()
        _reset()


def test_cli_run_remote_not_bootstrapped_is_clean_error_not_traceback(tmp_path, monkeypatch):
    """A remote serve with no workspace config wired in 503s — the CLI must
    show a specific "not bootstrapped" message, not a generic HTTP error."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    container.shell.override(_git_shell(_repo_git()))
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
        container.shell.reset_override()
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


# --- preflight: never dispatch a run that would execute stale code -----------

def _seed_task_with_worktree(ws: Path, slug: str, *repos: str) -> dict[str, Path]:
    wts = {}
    for repo in repos:
        wt = ws / ".worktrees" / slug / repo
        wt.mkdir(parents=True, exist_ok=True)
        wts[repo] = wt
    _seed_task(ws, slug=slug, repos=list(repos),
               worktrees={r: str(p) for r, p in wts.items()})
    return wts


def _repo_git(porcelain: str = "", *, origin: str | None = "headsha",
              head: str = "headsha", contains: list[str] = [],
              status_rc: int = 0, status_err: str = "",
              head_ref: str = "refs/heads/feat/t1",
              pair_output: str | None = None) -> dict:
    """One repo's scripted git answers. Defaults to clean, on the task's branch,
    and in sync with origin (`origin` None = the branch is not on origin at
    all; `head_ref` "" = a detached HEAD)."""
    return {
        "status": porcelain, "status_rc": status_rc, "status_err": status_err,
        "origin": origin, "head": head, "contains": contains,
        "head_ref": head_ref, "pair_output": pair_output,
    }


def _git_shell(
    spec: dict | dict[str, dict], *, push_rc: int = 0,
    git_dirs: dict[str, Path] | None = None,
):
    """A shell whose git answers are scripted per repo (keyed by the worktree
    directory name), with everything else passing. A bare spec applies to `api`."""
    if "status" in spec:
        spec = {"api": spec}
    shell = MagicMock(spec=ShellRunner)
    pushes: list[str] = []
    push_envs: list[dict] = []
    touched: set[str] = set()
    git_dirs = git_dirs or {}

    def _run(cmd, cwd=None, env=None, **kw):
        touched.add(Path(cwd).name)
        s = spec[Path(cwd).name]
        if "status --porcelain" in cmd:
            return ShellResult(
                returncode=s["status_rc"], stdout=s["status"], stderr=s["status_err"],
            )
        if "rev-parse --git-dir" in cmd:
            # `_inspect_repo` looks here for MERGE_HEAD / rebase-merge — a test
            # that wants the in-progress refusal creates the marker itself.
            git_dir = git_dirs.get(Path(cwd).name)
            return ShellResult(
                returncode=0, stdout=str(git_dir) if git_dir is not None else ".git",
                stderr="",
            )
        if "symbolic-ref" in cmd:
            return ShellResult(
                returncode=0 if s["head_ref"] else 1, stdout=s["head_ref"], stderr="",
            )
        if "ls-remote" in cmd:
            out = "" if s["origin"] is None else f"{s['origin']}\trefs/heads/feat/t1\n"
            return ShellResult(returncode=0, stdout=out, stderr="")
        if "rev-parse HEAD" in cmd and "refs/heads/" in cmd:
            # `_inspect_repo` separately verifies symbolic attachment, then
            # compares HEAD and the task ref in one combined read. `head_ref`
            # decides whether the branch-ref half agrees with `head`; a test
            # can override that pair to model a torn read or detached HEAD at
            # the branch tip.
            target_ref = cmd.rsplit("refs/heads/", 1)[-1].strip("'\"")
            if s["pair_output"] is not None:
                return ShellResult(returncode=0, stdout=s["pair_output"], stderr="")
            branch_sha = (
                s["head"] if s["head_ref"] == f"refs/heads/{target_ref}"
                else "otherbranchsha"
            )
            return ShellResult(returncode=0, stdout=f"{s['head']}\n{branch_sha}",
                               stderr="")
        if "merge-base --is-ancestor" in cmd:
            tip = cmd.split()[-2].strip("'")
            return ShellResult(returncode=0 if tip in s["contains"] else 1,
                               stdout="", stderr="")
        # --- commit synthesis (core/run_transfer.py) -------------------------
        if "write-tree" in cmd:
            return ShellResult(returncode=0, stdout="tree1111\n", stderr="")
        if "commit-tree" in cmd:
            return ShellResult(returncode=0, stdout="synth2222\n", stderr="")
        if cmd.startswith("git push"):
            pushes.append(cmd)
            push_envs.append(dict(env or {}))
            return ShellResult(returncode=push_rc, stdout="", stderr="denied\n")
        return ShellResult(returncode=0, stdout="", stderr="")

    shell.run.side_effect = _run
    shell.run_task.return_value = ShellResult(returncode=0, stdout="ok\n", stderr="")
    shell.pushes = pushes
    shell.push_envs = push_envs
    shell.touched = touched
    return shell


def test_a_dirty_worktree_is_sent_to_the_run_host_not_to_origin(tmp_path, monkeypatch):
    """ac1 + ac3 + ac7 through the CLI: the operator's uncommitted content is
    synthesized into a commit and pushed straight to the run host, and origin
    sees nothing at all. Under PR #419 this exact repo shape was a refusal."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git(" M src/app.py\n?? scratch.txt\n"))
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame(["ok\n"], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 0, result.output

        assert len(shell.pushes) == 1
        assert "synth2222:refs/mship/run/t1/api" in shell.pushes[0]
        assert "http://remote.example/git/api" in shell.pushes[0]
        assert "origin" not in shell.pushes[0]              # ac3
        assert recorder["json"]["run_ref_repos"] == ["api"]
        # The snapshot is rooted on the sha the preflight CERTIFIED HEAD to be
        # at, not on a re-resolved `HEAD`: a subagent committing in this
        # worktree between inspection and synthesis would otherwise re-parent it
        # on a commit nothing verified — the same bypass `remote_preflight.push`
        # closes for the origin path. Passing the literal "HEAD" here reads
        # identically in every other assertion, so it is asserted directly.
        commands = [c.args[0] for c in shell.run.call_args_list]
        assert any(c.startswith("git commit-tree tree1111 -p headsha ")
                   for c in commands), commands
    finally:
        container.shell.reset_override()
        _reset()


def test_a_git_root_child_is_transferred_under_its_parents_name(tmp_path, monkeypatch):
    """ac7 through the CLI: a `git_root` child has no git directory of its own —
    its tree IS the parent's — so the ref, the receive URL and `run_ref_repos`
    all name the PARENT, which is what the run host materializes and the only
    name the receive endpoint accepts. This is the one thing the `config=`
    argument on the preflight buys: without it every repo is its own git repo
    and this run would push `pkg` to an endpoint that has no such repository."""
    (tmp_path / "mono" / "pkg").mkdir(parents=True)
    taskfile = "version: '3'\ntasks:\n  run:\n    cmds:\n      - echo run\n"
    (tmp_path / "mono" / "Taskfile.yml").write_text(taskfile)
    (tmp_path / "mono" / "pkg" / "Taskfile.yml").write_text(taskfile)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "run_hosts: [role-x]\n"
        "repos:\n"
        "  mono:\n    path: ./mono\n    type: service\n"
        "  pkg:\n    path: pkg\n    type: service\n    git_root: mono\n"
    )
    wt_mono = tmp_path / ".worktrees" / "t1" / "mono"
    wt_pkg = wt_mono / "pkg"
    wt_pkg.mkdir(parents=True)
    _seed_task(tmp_path, slug="t1", repos=["mono", "pkg"],
               worktrees={"mono": str(wt_mono), "pkg": str(wt_pkg)})
    _configure(tmp_path)
    shell = _git_shell({"mono": _repo_git(), "pkg": _repo_git(" M pkg/a.py\n")})
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame(["ok\n"], exit_code=0))):
            result = runner.invoke(
                app, ["run", "--task", "t1", "--repos", "pkg", "--remote=role-x"]
            )
        assert result.exit_code == 0, result.output
        assert len(shell.pushes) == 1
        assert "synth2222:refs/mship/run/t1/mono" in shell.pushes[0]
        assert "http://remote.example/git/mono" in shell.pushes[0]
        assert recorder["json"]["run_ref_repos"] == ["mono"]
        assert recorder["json"]["repos"] == ["pkg"]   # the RUN's scope is unchanged
    finally:
        container.shell.reset_override()
        _reset()


def test_the_bearer_never_reaches_the_push_command_line(tmp_path, monkeypatch):
    """ac4, end to end through the CLI: argv is world-readable via /proc."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git(" M src/app.py\n"))
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    try:
        with _ClientPatch(monkeypatch, _recording_handler({}, _frame(["ok\n"], exit_code=0))):
            runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert "tok-abc" not in shell.pushes[0]
        assert "Authorization: Bearer tok-abc" in shell.push_envs[0].values()
    finally:
        container.shell.reset_override()
        _reset()


def test_the_output_names_the_revision_as_a_throwaway_run_ref(tmp_path, monkeypatch):
    """ac13: nobody should `git show` it and try to build on it.

    `MSHIP_JSON=0` is required, not decorative: `Output.breadcrumb` is gated on
    `human_mode`, and `json_mode` defaults to `not is_tty` — so under CliRunner
    breadcrumbs are suppressed and `result.output` would never contain the line.
    """
    monkeypatch.setenv("MSHIP_JSON", "0")
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    container.shell.override(_git_shell(_repo_git(" M src/app.py\n")))
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    try:
        with _ClientPatch(monkeypatch, _recording_handler({}, _frame(["ok\n"], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert "throwaway" in result.output
        assert "refs/mship/run/t1/api" in result.output
        assert "synth2222"[:12] in result.output
    finally:
        container.shell.reset_override()
        _reset()


def test_a_failed_transfer_aborts_before_dispatch(tmp_path, monkeypatch):
    """Dispatching after a failed transfer would run the previous ref's tree —
    the same silent-stale-code failure, one layer along."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    container.shell.override(_git_shell(_repo_git(" M src/app.py\n"), push_rc=1))
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert "run host" in result.output and "denied" in result.output
        assert recorder == {}                       # never contacted
    finally:
        container.shell.reset_override()
        _reset()


def test_a_mid_rebase_repo_is_refused_before_anything_is_sent(tmp_path, monkeypatch):
    """ac12 through the CLI."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    wts = _seed_task_with_worktree(tmp_path, "t1", "api")
    (wts["api"] / ".git" / "rebase-merge").mkdir(parents=True)
    _configure(tmp_path)
    shell = _git_shell(_repo_git("UU src/app.py\n"))
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert "merge or rebase in progress in api" in result.output
        assert "--abort" in result.output
        assert shell.pushes == []
        assert recorder == {}
    finally:
        container.shell.reset_override()
        _reset()


@pytest.mark.parametrize(
    ("marker", "description", "recovery_commands"),
    [
        (
            "rebase-merge",
            "a rebase is in progress",
            ("rebase --continue", "rebase --abort"),
        ),
        (
            "rebase-apply",
            "a rebase or `git am` is in progress",
            (
                "rebase --continue",
                "am --continue",
                "rebase --abort",
                "am --abort",
            ),
        ),
        (
            "MERGE_HEAD",
            "a merge is in progress",
            ("merge --continue", "merge --abort"),
        ),
        (
            "CHERRY_PICK_HEAD",
            "a cherry-pick is in progress",
            ("cherry-pick --continue", "cherry-pick --abort"),
        ),
        (
            "REVERT_HEAD",
            "a revert is in progress",
            ("revert --continue", "revert --abort"),
        ),
        (
            "sequencer",
            "a cherry-pick or revert sequence is in progress",
            (
                "cherry-pick --continue",
                "revert --continue",
                "cherry-pick --abort",
                "revert --abort",
            ),
        ),
        (
            "BISECT_START",
            "a bisect is in progress",
            ("bisect skip", "bisect reset"),
        ),
    ],
)
@pytest.mark.parametrize("linked_worktree", [False, True], ids=["normal", "linked"])
@pytest.mark.parametrize(
    "head_ref",
    ["refs/heads/other", ""],
    ids=["wrong-branch", "no-checkout"],
)
def test_clean_operation_marker_outranks_detached_or_wrong_branch(
    tmp_path, monkeypatch, marker, description, recovery_commands,
    linked_worktree, head_ref,
):
    """A suspended operation wins over a detached/wrong-branch HEAD, even clean."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    wts = _seed_task_with_worktree(tmp_path, "t1", "api")
    git_dir = (
        tmp_path / ".git-worktrees" / "api"
        if linked_worktree
        else wts["api"] / ".git"
    )
    marker_path = git_dir / marker
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    if marker in {"rebase-merge", "rebase-apply", "sequencer"}:
        marker_path.mkdir()
    elif marker == "BISECT_START":
        marker_path.write_text("start\n")
    else:
        marker_path.touch()
    if marker == "BISECT_START":
        assert not (git_dir / "BISECT_LOG").exists()
    _configure(tmp_path)
    shell = _git_shell(
        _repo_git(head_ref=head_ref),
        git_dirs={"api": git_dir} if linked_worktree else None,
    )
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert description in result.output
        for command in recovery_commands:
            assert f'git -C "{wts["api"]}" {command}' in result.output
        if marker == "BISECT_START":
            assert f'git -C "{wts["api"]}" bisect good' not in result.output
            assert f'git -C "{wts["api"]}" bisect bad' not in result.output
        commands = [call.args[0] for call in shell.run.call_args_list]
        assert not any(
            command.startswith(("git rev-parse HEAD", "git symbolic-ref", "git ls-remote", "git merge-base"))
            for command in commands
        ), commands
        assert "# or git" not in result.output
        assert "checkout" not in result.output
        assert shell.pushes == []
        assert recorder == {}
    finally:
        container.shell.reset_override()
        _reset()


def test_remote_run_pushes_a_clean_unpushed_branch_then_dispatches(tmp_path, monkeypatch):
    """The case that fails outright today — nothing pushes before `mship finish`."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git(origin=None))
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame(["ok\n"], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 0, result.output
        # The sha `inspect` resolved HEAD to, not a re-resolved `HEAD` — see
        # test_remote_preflight.py's push-refspec tests for why that distinction
        # matters.
        assert any("push -u origin headsha:refs/heads/feat/t1" in c for c in shell.pushes)
        assert recorder["url"] == "http://remote.example/exec/run"   # dispatched after
    finally:
        container.shell.reset_override()
        _reset()


def test_a_failed_push_aborts_before_dispatch(tmp_path, monkeypatch):
    """Running after a failed push is exactly the stale-code case."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    container.shell.override(_git_shell(_repo_git(origin=None), push_rc=1))
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert "could not push api" in result.output
        assert recorder == {}
    finally:
        container.shell.reset_override()
        _reset()


def test_an_up_to_date_repo_dispatches_without_pushing(tmp_path, monkeypatch):
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git())
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame(["ok\n"], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 0, result.output
        assert shell.pushes == []
        assert recorder["url"].endswith("/exec/run")
    finally:
        container.shell.reset_override()
        _reset()


def test_a_repo_whose_git_state_is_unreadable_is_not_dispatched(tmp_path, monkeypatch):
    """A failed `git status` has EMPTY stdout, exactly like a clean tree. Reading
    it as clean dispatches a run over a repo nothing was verified about."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git(
        status_rc=128, status_err="fatal: not a git repository\n",
    ))
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert "unreadable git state in api" in result.output
        assert "not a git repository" in result.output
        assert "mship commit" not in result.output   # a broken repo is not a dirty one
        assert shell.pushes == []
        assert recorder == {}                        # the remote was never contacted
    finally:
        container.shell.reset_override()
        _reset()


def test_a_newer_commit_on_origin_is_refused_not_pushed(tmp_path, monkeypatch):
    """The run host resets to origin's tip, so it would execute a commit the
    operator has never seen. No push can fix that."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git(origin="abcdef0123456789", head="oldsha"))
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert "unpulled commits on origin in api" in result.output
        assert "pull --ff-only" in result.output
        assert shell.pushes == []                    # a push would only confuse
        assert recorder == {}
    finally:
        container.shell.reset_override()
        _reset()


def test_a_task_repo_whose_worktree_vanished_is_not_dispatched(tmp_path, monkeypatch):
    """Skipping it leaves the run host materializing that repo's branch anyway —
    from an older pushed revision, or failing outright."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    wts = _seed_task_with_worktree(tmp_path, "t1", "api")
    wts["api"].rmdir()
    _configure(tmp_path)
    shell = _git_shell(_repo_git())
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert "missing worktree in api" in result.output
        assert recorder == {}
    finally:
        container.shell.reset_override()
        _reset()


def test_a_worktree_not_on_the_task_branch_is_not_dispatched(tmp_path, monkeypatch):
    """The preflight reads HEAD; the run host materializes the task's BRANCH.
    Dispatching would run a commit nothing here inspected."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git(head_ref="refs/heads/main"))
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert "worktree is not on the task's branch in api" in result.output
        assert "HEAD is main, not feat/t1" in result.output
        assert shell.pushes == []        # nothing published for an unverified HEAD
        assert recorder == {}
    finally:
        container.shell.reset_override()
        _reset()


def test_a_detached_worktree_at_the_task_tip_is_not_dispatched(tmp_path, monkeypatch):
    """A matching SHA does not establish that the worktree is on task t1's branch."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    shell = _git_shell(_repo_git(
        head_ref="",
        pair_output="headsha\nheadsha\n",
    ))
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert "worktree is not on the task's branch in api" in result.output
        assert "HEAD is detached, not feat/t1" in result.output
        commands = [call.args[0] for call in shell.run.call_args_list]
        assert not any("ls-remote" in command for command in commands)
        assert shell.pushes == []
        assert recorder == {}
    finally:
        container.shell.reset_override()
        _reset()


class _TaskVanishesAfterFirstRead:
    """A state manager whose tasks are gone from every read after the first —
    the shape of another process closing the task mid-command. Everything else
    passes through to the real manager."""

    def __init__(self, real):
        self._real = real
        self._reads = 0

    def load(self):
        self._reads += 1
        state = self._real.load()
        if self._reads > 1:
            state.tasks.clear()
        return state

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_task_missing_from_a_later_state_read_does_not_skip_the_preflight(
    tmp_path, monkeypatch,
):
    """The preflight is mandatory. Looking the resolved slug up in a SECOND
    state read made it conditional on that read succeeding: a task gone by then
    dispatched with no check at all, over a repo (here, one whose git state
    cannot be read at all) that the first read had every fact needed to refuse.
    The remedy is not to report the race but to remove it — the caller already
    holds the resolved task."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"])
    _seed_task_with_worktree(tmp_path, "t1", "api")
    _configure(tmp_path)
    container.shell.override(_git_shell(_repo_git(
        "", status_rc=128, status_err="fatal: not a git repository\n",
    )))
    container.state_manager.override(
        _TaskVanishesAfterFirstRead(StateManager(tmp_path / ".mothership"))
    )
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(app, ["run", "--task", "t1", "--remote=role-x"])
        assert result.exit_code == 1, result.output
        assert "unreadable git state in api" in result.output
        assert recorder == {}                    # the remote was never contacted
    finally:
        container.shell.reset_override()
        _reset()


def test_repos_scope_keeps_an_unrelated_dirty_repo_from_blocking(tmp_path, monkeypatch):
    """`--repos api` never touches web, so work in progress there cannot make
    api's run execute stale code — aborting over it is pure obstruction."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"], repos=["api", "web"])
    _seed_task_with_worktree(tmp_path, "t1", "api", "web")
    _configure(tmp_path)
    shell = _git_shell({"api": _repo_git(), "web": _repo_git(" M b.ts\n")})
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame(["ok\n"], exit_code=0))):
            result = runner.invoke(
                app, ["run", "--task", "t1", "--repos", "api", "--remote=role-x"]
            )
        assert result.exit_code == 0, result.output
        assert recorder["json"]["repos"] == ["api"]
        assert "run_ref_repos" not in recorder["json"]   # web was never transferred
    finally:
        container.shell.reset_override()
        _reset()


def test_a_repos_selection_outside_the_tasks_worktrees_is_not_dispatched(tmp_path, monkeypatch):
    """`--repos web` where `web` is a real repo in this workspace but not one of
    task t1's repos at all (no worktree entry) must refuse rather than silently
    drop web from the check: `_resolve_repos` only validates the selection
    against the WORKSPACE's repos, never against the task's, so nothing else
    stands between this selection and a remote dispatch that materializes
    `feat/t1` for a repo the preflight never looked at."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"], repos=["api", "web"])
    _seed_task_with_worktree(tmp_path, "t1", "api")   # web is NOT one of t1's repos
    _configure(tmp_path)
    shell = _git_shell(_repo_git())
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    recorder: dict = {}
    try:
        with _ClientPatch(monkeypatch, _recording_handler(recorder, _frame([], exit_code=0))):
            result = runner.invoke(
                app, ["run", "--task", "t1", "--repos", "api,web", "--remote=role-x"]
            )
        assert result.exit_code == 1, result.output
        assert "missing worktree in web" in result.output
        assert shell.touched == {"api"}          # web was never even looked at
        assert shell.pushes == []
        assert recorder == {}
    finally:
        container.shell.reset_override()
        _reset()


def test_repos_scope_does_not_push_a_repo_the_operator_did_not_name(tmp_path, monkeypatch):
    """A narrowly scoped command must not push a repo outside its scope — web's
    branch is not on origin, so an unscoped preflight would push it. The check
    is that web was never so much as LOOKED at: no inspection, hence no push."""
    _write_run_workspace(tmp_path, run_hosts=["role-x"], repos=["api", "web"])
    _seed_task_with_worktree(tmp_path, "t1", "api", "web")
    _configure(tmp_path)
    shell = _git_shell({"api": _repo_git(), "web": _repo_git(origin=None)})
    container.shell.override(shell)
    RunHostStore(tmp_path / ".mothership").set(
        "role-x", RunHostConnection(url="http://remote.example", token="tok-abc"),
    )
    try:
        with _ClientPatch(monkeypatch, _recording_handler({}, _frame(["ok\n"], exit_code=0))):
            result = runner.invoke(
                app, ["run", "--task", "t1", "--repos", "api", "--remote=role-x"]
            )
        assert result.exit_code == 0, result.output
        assert shell.pushes == []
        assert shell.touched == {"api"}
    finally:
        container.shell.reset_override()
        _reset()


# --- exact copy: which repos come from a scratch ref -------------------------

def test_exec_remote_sends_run_ref_repos_when_there_are_any():
    recorder: dict = {}
    conn = RunHostConnection(url="http://remote.example", token="tok")

    remote_client.exec_remote(
        verb="run", conn=conn, task="t1", repos=["api", "web"],
        run_ref_repos=["api"], print_fn=lambda _l: None,
        transport=_mock_transport(_recording_handler(recorder, _frame(["ok\n"], exit_code=0))),
    )

    assert recorder["json"] == {
        "task": "t1", "repos": ["api", "web"], "kind": "all", "run_ref_repos": ["api"],
    }


def test_exec_remote_omits_the_key_entirely_when_nothing_was_transferred():
    """ac9: a clean run must be byte-identical on the wire to today's, so a run
    host on an older mship keeps working."""
    recorder: dict = {}
    conn = RunHostConnection(url="http://remote.example", token="tok")

    remote_client.exec_remote(
        verb="run", conn=conn, task="t1", repos=["api"], print_fn=lambda _l: None,
        transport=_mock_transport(_recording_handler(recorder, _frame(["ok\n"], exit_code=0))),
    )

    assert recorder["json"] == {"task": "t1", "repos": ["api"], "kind": "all"}
