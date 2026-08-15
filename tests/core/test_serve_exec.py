"""Tests for `POST /exec/{verb}` — the serve side of `mship run/capture/build
--remote` (see `mship.core.remote_exec`, specs/2026-07-11-remote-run-machine.md).

Two layers are covered:
  - `run_verb_stream` (core/remote_exec.py) exercised directly, including a
    deterministic proof that it yields lines AS THEY'RE PRODUCED rather than
    buffering until the subprocess exits.
  - `POST /exec/{verb}` (core/serve.py) via FastAPI's TestClient: auth,
    unknown-verb 404, the branch-materialize git commands, the capture
    env-var contract, MOS-203's base-freshness check, and the wire framing
    (line-per-chunk task output + a trailing `__MSHIP_EXIT__ <code>` line —
    a non-zero task exit is conveyed as data, never an HTTP error).
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import io
import multiprocessing
import os
import signal
import shlex
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from mship.core import remote_exec
from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.serve import ExecBody, create_app
from mship.core.state import StateManager
from mship.util import shell as shell_module
from mship.util.shell import ShellResult


# --- shared fakes -----------------------------------------------------------


class _GatedStdout:
    """Feeds canned lines one at a time. If `gate`/`gate_before` are set, the
    line at index `gate_before` blocks (bounded by a 2s timeout, so a test
    can never hang forever) until the test calls `gate.set()` — used to prove
    a consumer received earlier lines before this one was even produced."""

    def __init__(self, lines, gate: threading.Event | None = None, gate_before: int | None = None):
        self._lines = list(lines)
        self._i = 0
        self._gate = gate
        self._gate_before = gate_before

    def readline(self):
        if self._i >= len(self._lines):
            return ""
        if self._gate is not None and self._i == self._gate_before:
            self._gate.wait(timeout=2)
        line = self._lines[self._i]
        self._i += 1
        return line

    def close(self):
        pass


class _FakeProc:
    """Popen-shaped fake: canned stdout lines + stderr text + a canned
    returncode, standing in for `ShellRunner.run_streaming`'s real Popen."""

    def __init__(self, stdout_lines=(), stderr_text="", returncode=0, gate=None, gate_before=None):
        self.stdout = _GatedStdout(stdout_lines, gate=gate, gate_before=gate_before)
        self.stderr = io.StringIO(stderr_text)
        self._returncode = returncode

    def wait(self):
        return self._returncode


class _FakeShellRunner:
    """Stands in for `mship.util.shell.ShellRunner`. `.run_argv()` records
    each git command as display text plus cwd and returns a canned
    `ShellResult` looked up by that text (`rev_responses` supports successive
    responses for repeated commands). `.run_streaming()` returns the canned
    `_FakeProc` and records what it was invoked with."""

    def __init__(self, *, streaming_proc=None, rev_responses=None):
        self.run_calls: list[tuple[str, Path]] = []
        self.streaming_calls: list[dict] = []
        self._rev_responses = {
            k: list(v) for k, v in (rev_responses or {}).items()
        }
        self._streaming_proc = streaming_proc

    def build_command(self, command, env_runner=None):
        if env_runner:
            return f"{env_runner} {command}"
        return command

    def run(self, command, cwd, env=None, cancel_event=None):
        self.run_calls.append((command, Path(cwd)))
        seq = self._rev_responses.get(command)
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return ShellResult(returncode=0, stdout="", stderr="")

    def run_argv(self, args, cwd, env=None, cancel_event=None):
        return self.run(
            shlex.join(args),
            cwd,
            env=env,
            cancel_event=cancel_event,
        )

    def run_streaming(self, command, cwd, env=None):
        self.streaming_calls.append({"command": command, "cwd": Path(cwd), "env": env})
        return self._streaming_proc


def _config(tmp_path: Path, *, base_branch: str | None = None) -> WorkspaceConfig:
    repo_dir = tmp_path / "api"
    repo_dir.mkdir(exist_ok=True)
    return WorkspaceConfig(
        workspace="t",
        repos={
            "api": RepoConfig(
                path=repo_dir, type="service",
                tasks={"run": "start", "capture": "capture", "build": "build"},
                base_branch=base_branch,
            ),
        },
    )


def _config_with_child(tmp_path: Path) -> WorkspaceConfig:
    """A parent service ('app') plus a `git_root` subdirectory child
    ('server') nested under it — the shape FIX A guards: a child's worktree
    is its parent's worktree, so the parent MUST be materialized (parent-
    first, mirroring WorktreeManager.spawn) before the child's path is
    resolved, even when only the child is requested or it's listed first."""
    parent_dir = tmp_path / "app"
    (parent_dir / "server").mkdir(parents=True, exist_ok=True)
    return WorkspaceConfig(
        workspace="t",
        repos={
            "app": RepoConfig(
                path=parent_dir, type="service",
                tasks={"run": "start", "capture": "capture", "build": "build"},
            ),
            "server": RepoConfig(
                path=Path("server"), type="service", git_root="app",
                tasks={"run": "start", "capture": "capture", "build": "build"},
            ),
        },
    )


def _app(tmp_path: Path, *, auth_token: str | None = None, config: WorkspaceConfig | None = None):
    return create_app(
        specs_dir=tmp_path / "specs",
        state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None,
        workspace_root=tmp_path,
        workspace_name="test-ws",
        auth_token=auth_token,
        config=config if config is not None else _config(tmp_path),
    )


def _patch_shell(monkeypatch, fake: _FakeShellRunner):
    monkeypatch.setattr("mship.core.serve.ShellRunner", lambda: fake)


class _ArtifactWritingShellRunner(_FakeShellRunner):
    """Like `_FakeShellRunner`, but `.run_streaming()` first writes canned
    files into `env["MSHIP_CAPTURE_DIR"]` before returning the canned proc —
    standing in for the real go-task `capture:` target (adb/simctl/etc.)
    actually producing `screen.png`/`layout.*` there."""

    def __init__(self, *, streaming_proc, artifacts: dict[str, bytes] | None = None, **kw):
        super().__init__(streaming_proc=streaming_proc, **kw)
        self._artifacts = artifacts or {}

    def run_streaming(self, command, cwd, env=None):
        if env and "MSHIP_CAPTURE_DIR" in env:
            out_dir = Path(env["MSHIP_CAPTURE_DIR"])
            out_dir.mkdir(parents=True, exist_ok=True)
            for name, content in self._artifacts.items():
                (out_dir / name).write_bytes(content)
        return super().run_streaming(command, cwd, env=env)


# A fixed nonce for direct `run_verb_stream` calls (the HTTP layer generates a
# fresh one per request and hands it back in the X-Mship-Exec-Nonce header — see
# `_nonce_of`). Control records are only recognized when tagged with the nonce.
_TEST_NONCE = "testnonce0123"


def _nonce_of(resp) -> str:
    """The per-request anti-spoof nonce from the response header (case-
    insensitive). Every `POST /exec/{verb}` sets it; control records in the
    body are tagged with it."""
    return resp.headers["x-mship-exec-nonce"]


def _exit_line(nonce: str, code: int) -> str:
    return f"{remote_exec.EXIT_MARKER}:{nonce} {code}"


def _hold_remote_exec_stream(
    workspace_root: str,
    ready,
    release,
) -> None:
    root = Path(workspace_root)
    fake = _FakeShellRunner()
    deps = remote_exec.RemoteExecDeps(
        config=_config(root), shell=fake, workspace_root=root,
    )
    stream = remote_exec.run_verb_stream(
        "run", "t1", ["unknown"], None, deps=deps, nonce="processholder",
    )
    try:
        first = next(stream)
        if b"unknown repo" not in first:
            raise AssertionError(first)
        ready.set()
        if not release.wait(timeout=10):
            raise TimeoutError("parent did not release the held remote execution stream")
    finally:
        stream.close()


def _task_lock_path(workspace_root: Path, task: str) -> Path:
    digest = hashlib.sha256(task.encode("utf-8")).hexdigest()
    return workspace_root / ".mothership" / "remote-exec-locks" / f"{digest}.lock"


def _assert_task_lock_failure(
    workspace_root: Path,
    fake: _FakeShellRunner,
    nonce: str,
) -> None:
    deps = remote_exec.RemoteExecDeps(
        config=_config(workspace_root), shell=fake, workspace_root=workspace_root,
    )
    lines = list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None, deps=deps, nonce=nonce,
    ))
    assert b"remote-task locking" in lines[0]
    assert lines[-1] == f"{remote_exec.EXIT_MARKER}:{nonce} 1\n".encode()
    assert not fake.run_calls
    assert not fake.streaming_calls


def _exec_asgi_scope() -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/exec/run",
        "raw_path": b"/exec/run",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }


def _disconnect_exec_request(app, started, stopped, cleanup) -> list[dict]:
    response_messages: list[dict] = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {
                "type": "http.request",
                "body": b'{"task":"t1","repos":["api"]}',
                "more_body": False,
            }
        assert await asyncio.to_thread(started.wait, 10)
        return {"type": "http.disconnect"}

    async def send(message):
        response_messages.append(message)

    async def disconnect_request() -> bool:
        app_task = asyncio.create_task(
            app(_exec_asgi_scope(), receive, send),
        )
        stopped_by_disconnect = False
        try:
            assert await asyncio.to_thread(started.wait, 10)
            stopped_by_disconnect = await asyncio.to_thread(stopped.wait, 5)
        finally:
            if not stopped_by_disconnect:
                cleanup()
            await asyncio.wait_for(app_task, timeout=10)
        return stopped_by_disconnect

    assert asyncio.run(disconnect_request())
    assert response_messages[0]["type"] == "http.response.start"
    return response_messages


def _assert_same_task_replacement(workspace_root: Path) -> None:
    replacement_fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["replacement\n"]),
    )
    replacement_deps = remote_exec.RemoteExecDeps(
        config=_config(workspace_root),
        shell=replacement_fake,
        workspace_root=workspace_root,
    )
    assert list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None,
        deps=replacement_deps, nonce="replacementnonce",
    ))[-2:] == [
        b"replacement\n",
        b"__MSHIP_EXIT__:replacementnonce 0\n",
    ]


def _parse_exec_stream(content: bytes, nonce: str):
    """Mirror the client-side parse of the `/exec/{verb}` wire framing: text
    lines, optionally one `__MSHIP_ARTIFACTS__:<nonce> <n>` marker followed by
    exactly `n` raw tar bytes, then the trailing `__MSHIP_EXIT__:<nonce> <code>`
    line. A control record counts ONLY when tagged with `nonce`. Returns
    `(text_lines, tar_bytes_or_None, exit_line)`."""
    lines: list[str] = []
    tar_bytes: bytes | None = None
    idx = 0
    art_prefix = f"{remote_exec.ARTIFACT_MARKER}:{nonce} ".encode()
    exit_prefix = f"{remote_exec.EXIT_MARKER}:{nonce} ".encode()
    while True:
        nl = content.index(b"\n", idx)
        line = content[idx:nl]
        idx = nl + 1
        if line.startswith(art_prefix):
            n = int(line.split(b" ", 1)[1])
            tar_bytes = content[idx : idx + n]
            idx += n
            continue
        if line.startswith(exit_prefix):
            return lines, tar_bytes, line.decode()
        lines.append(line.decode())


# --- run_verb_stream (direct, no HTTP layer) --------------------------------


def test_run_verb_stream_yields_lines_as_produced_not_buffered(tmp_path):
    """Deterministic proof that `run_verb_stream` is a genuine incremental
    generator: line 2 is gated behind a threading.Event the test only sets
    AFTER observing line 1 arrive. If the generator instead buffered
    everything before yielding (e.g. `list(...)`'d internally), the first
    `next()` below would itself block on the gate — which isn't set yet —
    and this test would hang (bounded to ~2s by the gate's own timeout)."""
    gate = threading.Event()
    proc = _FakeProc(stdout_lines=["first\n", "second\n"], returncode=0, gate=gate, gate_before=1)
    fake = _FakeShellRunner(streaming_proc=proc)
    deps = remote_exec.RemoteExecDeps(config=_config(tmp_path), shell=fake, workspace_root=tmp_path)

    gen = remote_exec.run_verb_stream("run", "t1", ["api"], None, deps=deps, nonce=_TEST_NONCE)
    first = next(gen)
    assert first == b"first\n"
    # Not released yet — proves the generator didn't need "second" to produce "first".
    assert not gate.is_set()
    gate.set()
    rest = list(gen)
    assert rest[0] == b"second\n"
    assert rest[-1] == f"__MSHIP_EXIT__:{_TEST_NONCE} 0\n".encode()


def test_run_verb_stream_client_disconnect_cleans_up_tempdir_and_proc(tmp_path):
    """FIX 8: a client disconnect (the generator being `.close()`d while
    suspended at a yield mid-stream) must not leak the capture temp dir OR the
    still-running task subprocess. The per-repo `finally` removes the temp dir
    and terminates the proc."""
    gate = threading.Event()  # never set -> line 2 blocks; generator stays suspended

    class _TerminableProc(_FakeProc):
        pid = None  # not a real OS pid -> _terminate_proc falls back to .terminate()

        def __init__(self, **kw):
            super().__init__(**kw)
            self.terminated = False

        def poll(self):
            return None  # still running

        def terminate(self):
            self.terminated = True

    proc = _TerminableProc(stdout_lines=["line1\n", "line2\n"], returncode=0, gate=gate, gate_before=1)
    fake = _FakeShellRunner(streaming_proc=proc)
    deps = remote_exec.RemoteExecDeps(config=_config(tmp_path), shell=fake, workspace_root=tmp_path)

    gen = remote_exec.run_verb_stream("capture", "t1", ["api"], None, deps=deps, nonce=_TEST_NONCE)
    assert next(gen) == b"line1\n"  # first line delivered; generator now suspended at the yield
    capture_dir = Path(fake.streaming_calls[0]["env"]["MSHIP_CAPTURE_DIR"])
    assert capture_dir.exists()

    gen.close()  # simulate the client hanging up -> GeneratorExit at the suspended yield

    assert proc.terminated, "the running task subprocess must be terminated on disconnect"
    assert not capture_dir.exists(), "the capture temp dir must be removed on disconnect"


def test_exec_response_does_not_require_create_collapsing_task_group(
    tmp_path,
    monkeypatch,
):
    from starlette import _utils as starlette_utils

    monkeypatch.delattr(
        starlette_utils,
        "create_collapsing_task_group",
        raising=False,
    )
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["ok\n"]),
    )
    _patch_shell(monkeypatch, fake)

    response = TestClient(_app(tmp_path)).post(
        "/exec/run",
        json={"task": "t1", "repos": ["api"]},
    )

    assert response.status_code == 200
    nonce = _nonce_of(response)
    lines, artifacts, exit_line = _parse_exec_stream(response.content, nonce)
    assert lines == ["ok"]
    assert artifacts is None
    assert exit_line == _exit_line(nonce, 0)


def test_exec_http_disconnect_terminates_quiet_proc_and_releases_task_lock(
    tmp_path,
    monkeypatch,
):
    started = threading.Event()
    terminated = threading.Event()
    unblock = threading.Event()

    class _BlockingStream:
        def readline(self):
            unblock.wait(timeout=5)
            return ""

        def close(self):
            pass

    class _QuietProc:
        pid = None
        stdout = _BlockingStream()
        stderr = _BlockingStream()

        def wait(self):
            unblock.wait(timeout=5)
            return 0

        def poll(self):
            return 0 if unblock.is_set() else None

        def terminate(self):
            terminated.set()
            unblock.set()

    class _SignalingShellRunner(_FakeShellRunner):
        def run_streaming(self, command, cwd, env=None):
            proc = super().run_streaming(command, cwd, env=env)
            started.set()
            return proc

    fake = _SignalingShellRunner(streaming_proc=_QuietProc())
    _patch_shell(monkeypatch, fake)
    app = _app(tmp_path)
    _disconnect_exec_request(app, started, terminated, unblock.set)
    _assert_same_task_replacement(tmp_path)


def test_exec_asgi24_send_disconnect_reaps_proc_and_releases_task_lock(
    tmp_path,
    monkeypatch,
):
    started = threading.Event()
    terminated = threading.Event()
    reaped = threading.Event()
    unblock = threading.Event()

    class _OneLineAliveProc:
        pid = None
        stdout = _GatedStdout(
            ["owned\n", ""],
            gate=unblock,
            gate_before=1,
        )
        stderr = io.StringIO("")

        def wait(self):
            unblock.wait(timeout=5)
            reaped.set()
            return 0

        def poll(self):
            return 0 if unblock.is_set() else None

        def terminate(self):
            terminated.set()
            unblock.set()

    class _SignalingShellRunner(_FakeShellRunner):
        def run_streaming(self, command, cwd, env=None):
            proc = super().run_streaming(command, cwd, env=env)
            started.set()
            return proc

    fake = _SignalingShellRunner(streaming_proc=_OneLineAliveProc())
    _patch_shell(monkeypatch, fake)
    app = _app(tmp_path)
    scope = _exec_asgi_scope()
    scope["asgi"] = {"version": "3.0", "spec_version": "2.4"}
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {
                "type": "http.request",
                "body": b'{"task":"t1","repos":["api"]}',
                "more_body": False,
            }
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message):
        if message["type"] == "http.response.body" and message["body"]:
            assert started.is_set()
            raise OSError("client disconnected")

    try:
        with pytest.raises(ClientDisconnect):
            asyncio.run(app(scope, receive, send))
    finally:
        unblock.set()

    assert terminated.is_set()
    assert reaped.is_set()
    assert receive_calls == 1
    _assert_same_task_replacement(tmp_path)


def test_exec_http_disconnect_cancels_blocked_materialization(
    tmp_path,
    monkeypatch,
):
    started = threading.Event()
    operation_finished = threading.Event()
    processes = []
    real_popen = shell_module.subprocess.Popen

    def blocking_popen(_command, **kwargs):
        proc = real_popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            cwd=kwargs.get("cwd"),
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
            text=kwargs.get("text"),
            env=kwargs.get("env"),
            start_new_session=True,
        )
        processes.append(proc)
        started.set()
        return proc

    class _ObservedShellRunner(shell_module.ShellRunner):
        def run_argv(self, *args, **kwargs):
            try:
                return super().run_argv(*args, **kwargs)
            finally:
                operation_finished.set()

    monkeypatch.setattr(shell_module.subprocess, "Popen", blocking_popen)
    monkeypatch.setattr(
        "mship.core.serve.ShellRunner",
        lambda: _ObservedShellRunner(),
    )
    app = _app(tmp_path)

    _disconnect_exec_request(
        app,
        started,
        operation_finished,
        lambda: processes[0].terminate(),
    )

    assert processes[0].returncode is not None
    _assert_same_task_replacement(tmp_path)


def test_exec_http_disconnect_terminates_proc_after_pipe_eof(
    tmp_path,
    monkeypatch,
):
    started = threading.Event()
    terminated = threading.Event()
    reaped = threading.Event()
    unblock = threading.Event()

    class _EofAliveProc:
        pid = None
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def wait(self):
            unblock.wait(timeout=5)
            reaped.set()
            return 0

        def poll(self):
            return 0 if unblock.is_set() else None

        def terminate(self):
            terminated.set()
            unblock.set()

    class _SignalingShellRunner(_FakeShellRunner):
        def run_streaming(self, command, cwd, env=None):
            proc = super().run_streaming(command, cwd, env=env)
            started.set()
            return proc

    fake = _SignalingShellRunner(streaming_proc=_EofAliveProc())
    _patch_shell(monkeypatch, fake)
    app = _app(tmp_path)

    _disconnect_exec_request(app, started, terminated, unblock.set)
    assert reaped.is_set()
    _assert_same_task_replacement(tmp_path)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux /proc process-state contract",
)
def test_cancelled_stream_kills_descendant_before_task_lock_release(
    tmp_path,
    monkeypatch,
):
    ready_path = tmp_path / "stream-descendant-ready"
    child_code = """
import os
import signal
import sys
from pathlib import Path

read_fd, write_fd = os.pipe()
child_pid = os.fork()
if child_pid == 0:
    os.close(read_fd)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(1)
    os.close(2)
    os.write(write_fd, b"ready")
    os.close(write_fd)
    while True:
        signal.pause()

os.close(write_fd)
os.read(read_fd, 5)
os.close(read_fd)
Path(sys.argv[1]).write_text(str(child_pid))
while True:
    signal.pause()
"""
    processes = []

    class _ProcessTreeShellRunner(_FakeShellRunner):
        def run_streaming(self, command, cwd, env=None):
            proc = shell_module.subprocess.Popen(
                [sys.executable, "-c", child_code, str(ready_path)],
                cwd=tmp_path,
                stdout=shell_module.subprocess.PIPE,
                stderr=shell_module.subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            processes.append(proc)
            return proc
    signals: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def observed_killpg(process_group, signum):
        signals.append((process_group, signum))
        return real_killpg(process_group, signum)

    monkeypatch.setattr(shell_module.os, "killpg", observed_killpg)

    cancel_event = threading.Event()
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path),
        shell=_ProcessTreeShellRunner(),
        workspace_root=tmp_path,
        cancel_event=cancel_event,
    )
    errors: list[BaseException] = []

    def consume_stream() -> None:
        try:
            list(remote_exec.run_verb_stream(
                "run",
                "t1",
                ["api"],
                None,
                deps=deps,
                nonce="descendantnonce",
            ))
        except BaseException as exc:
            errors.append(exc)

    stream_thread = threading.Thread(target=consume_stream, daemon=True)
    stream_thread.start()
    deadline = time.monotonic() + 5
    while not ready_path.exists() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert ready_path.exists(), "descendant did not report readiness"

    descendant_pid = int(ready_path.read_text())
    process_group = os.getpgid(descendant_pid)
    proc_stat = Path(f"/proc/{descendant_pid}/stat")
    assert process_group == processes[0].pid
    try:
        cancel_event.set()
        stream_thread.join(timeout=5)
        assert not stream_thread.is_alive(), "cancelled stream did not return"
        assert not errors
        assert (process_group, signal.SIGKILL) in signals

        try:
            descendant_state = proc_stat.read_text().split()[2]
        except FileNotFoundError:
            descendant_state = None
        assert descendant_state in {None, "X", "Z"}, (
            "descendant was executable when the public task lock was released: "
            f"{descendant_state}"
        )
        _assert_same_task_replacement(tmp_path)
    finally:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_normal_stream_does_not_signal_reaped_process_group(
    tmp_path,
    monkeypatch,
):
    signals = []

    def record_killpg(process_group, signum):
        signals.append((process_group, signum))

    monkeypatch.setattr(shell_module.os, "killpg", record_killpg)

    class _CompletedProc(_FakeProc):
        pid = 424242

        def poll(self):
            return 0

    fake = _FakeShellRunner(
        streaming_proc=_CompletedProc(stdout_lines=["done\n"]),
    )
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path),
        shell=fake,
        workspace_root=tmp_path,
    )

    assert list(remote_exec.run_verb_stream(
        "run",
        "t1",
        ["api"],
        None,
        deps=deps,
        nonce="normalnonce",
    ))[-1] == b"__MSHIP_EXIT__:normalnonce 0\n"
    assert not signals


def test_concurrent_same_task_is_refused_while_different_task_runs(tmp_path):
    gate = threading.Event()
    first_fake = _FakeShellRunner(streaming_proc=_FakeProc(
        stdout_lines=["first\n", "second\n"], gate=gate, gate_before=1,
    ))
    first_deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=first_fake, workspace_root=tmp_path,
    )
    first = remote_exec.run_verb_stream(
        "run", "t1", ["api"], None, deps=first_deps, nonce="firstnonce",
    )
    assert next(first) == b"first\n"

    different_fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["different\n"]),
    )
    different_deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=different_fake, workspace_root=tmp_path,
    )
    assert list(remote_exec.run_verb_stream(
        "run", "t2", ["api"], None, deps=different_deps, nonce="differentnonce",
    )) == [
        b"different\n",
        b"__MSHIP_EXIT__:differentnonce 0\n",
    ]

    contender_fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["must not run\n"]),
    )
    contender_deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=contender_fake, workspace_root=tmp_path,
    )
    contender = list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None,
        deps=contender_deps, nonce="contendernonce",
    ))
    assert contender == [
        b"error: remote task 't1' is already running; try again after it finishes\n",
        b"__MSHIP_EXIT__:contendernonce 2\n",
    ]
    assert not contender_fake.run_calls
    assert not contender_fake.streaming_calls

    first.close()
    assert _task_lock_path(tmp_path, "t1").is_file()

    replacement_fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["replacement\n"]),
    )
    replacement_deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=replacement_fake, workspace_root=tmp_path,
    )
    assert list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None,
        deps=replacement_deps, nonce="replacementnonce",
    ))[-2:] == [
        b"replacement\n",
        b"__MSHIP_EXIT__:replacementnonce 0\n",
    ]


@pytest.mark.parametrize("returncode", [0, 7])
def test_task_lock_releases_after_stream_exhaustion(tmp_path, returncode):
    first_fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["finished\n"], returncode=returncode),
    )
    first_deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=first_fake, workspace_root=tmp_path,
    )
    assert list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None, deps=first_deps, nonce="firstnonce",
    ))[-1] == f"__MSHIP_EXIT__:firstnonce {returncode}\n".encode()
    assert _task_lock_path(tmp_path, "t1").is_file()

    next_fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["next\n"]),
    )
    next_deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=next_fake, workspace_root=tmp_path,
    )
    assert list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None, deps=next_deps, nonce="nextnonce",
    ))[-2:] == [
        b"next\n",
        b"__MSHIP_EXIT__:nextnonce 0\n",
    ]


def test_task_lock_releases_when_execution_raises(tmp_path):
    class _RaisingShellRunner(_FakeShellRunner):
        def run_streaming(self, command, cwd, env=None):
            self.streaming_calls.append(
                {"command": command, "cwd": Path(cwd), "env": env},
            )
            raise RuntimeError("streaming exploded")

    failing_deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path),
        shell=_RaisingShellRunner(),
        workspace_root=tmp_path,
    )
    with pytest.raises(RuntimeError, match="streaming exploded"):
        list(remote_exec.run_verb_stream(
            "run", "t1", ["api"], None, deps=failing_deps, nonce="failingnonce",
        ))
    assert _task_lock_path(tmp_path, "t1").is_file()

    next_fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["recovered\n"]),
    )
    next_deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=next_fake, workspace_root=tmp_path,
    )
    assert list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None, deps=next_deps, nonce="nextnonce",
    ))[-2:] == [
        b"recovered\n",
        b"__MSHIP_EXIT__:nextnonce 0\n",
    ]


def test_concurrent_process_contends_for_same_task_lock(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    proc = ctx.Process(
        target=_hold_remote_exec_stream,
        args=(str(tmp_path), ready, release),
    )
    proc.start()
    try:
        assert ready.wait(timeout=10)
        fake = _FakeShellRunner()
        deps = remote_exec.RemoteExecDeps(
            config=_config(tmp_path), shell=fake, workspace_root=tmp_path,
        )
        contender = list(remote_exec.run_verb_stream(
            "run", "t1", ["unknown"], None,
            deps=deps, nonce="processcontender",
        ))
        assert contender == [
            b"error: remote task 't1' is already running; try again after it finishes\n",
            b"__MSHIP_EXIT__:processcontender 2\n",
        ]
    finally:
        release.set()
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=10)
    assert proc.exitcode == 0



def _make_linux_proc_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(shell_module.os, "name", "posix")
    monkeypatch.setattr(shell_module.sys, "platform", "linux")
    monkeypatch.setattr(
        shell_module,
        "_PROC_ROOT",
        tmp_path / "unavailable-proc",
        raising=False,
    )


def test_cancellable_remote_preflight_fails_before_lock_or_shell_work(
    tmp_path,
    monkeypatch,
):
    _make_linux_proc_unavailable(tmp_path, monkeypatch)
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["must not run\n"]),
    )
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path),
        shell=fake,
        workspace_root=tmp_path,
        cancel_event=threading.Event(),
    )

    lines = list(remote_exec.run_verb_stream(
        "run",
        "t1",
        ["api"],
        None,
        deps=deps,
        nonce="unsupportednonce",
    ))

    assert b"remote execution unavailable" in lines[0]
    assert b"process-status" in lines[0]
    assert lines[-1] == b"__MSHIP_EXIT__:unsupportednonce 1\n"
    assert not (tmp_path / ".mothership" / "remote-exec-locks").exists()
    assert not fake.run_calls
    assert not fake.streaming_calls


def test_exec_cancellable_preflight_failure_is_framed_before_work(
    tmp_path,
    monkeypatch,
):
    _make_linux_proc_unavailable(tmp_path, monkeypatch)
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["must not run\n"]),
    )
    _patch_shell(monkeypatch, fake)

    response = TestClient(_app(tmp_path)).post(
        "/exec/run",
        json={"task": "t1", "repos": ["api"]},
    )

    nonce = _nonce_of(response)
    lines = response.content.splitlines()
    assert response.status_code == 200
    assert b"remote execution unavailable" in lines[0]
    assert b"process-status" in lines[0]
    assert lines[-1] == _exit_line(nonce, 1).encode()
    assert not (tmp_path / ".mothership" / "remote-exec-locks").exists()
    assert not fake.run_calls
    assert not fake.streaming_calls


def test_direct_remote_without_cancellation_does_not_require_linux_proc(
    tmp_path,
    monkeypatch,
):
    _make_linux_proc_unavailable(tmp_path, monkeypatch)
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["ran\n"]),
    )
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path),
        shell=fake,
        workspace_root=tmp_path,
    )

    lines = list(remote_exec.run_verb_stream(
        "run",
        "t1",
        ["api"],
        None,
        deps=deps,
        nonce="directnonce",
    ))

    assert lines[-2:] == [
        b"ran\n",
        b"__MSHIP_EXIT__:directnonce 0\n",
    ]
    assert fake.streaming_calls


def test_remote_modules_import_when_fcntl_is_unavailable():
    script = """
import builtins

real_import = builtins.__import__

def import_without_fcntl(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("fcntl unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = import_without_fcntl
from mship.core import remote_client, remote_exec
print(remote_client.EXIT_MARKER, remote_client.ARTIFACT_MARKER)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "__MSHIP_EXIT__ __MSHIP_ARTIFACTS__"


def test_remote_without_fcntl_fails_locking_before_shell_work(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(remote_exec, "fcntl", None)
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["must not run\n"]),
    )
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path),
        shell=fake,
        workspace_root=tmp_path,
    )

    lines = list(remote_exec.run_verb_stream(
        "run",
        "t1",
        ["api"],
        None,
        deps=deps,
        nonce="nofcntlnonce",
    ))

    assert lines == [
        b"error: remote-task locking failed; execution was not started\n",
        b"__MSHIP_EXIT__:nofcntlnonce 1\n",
    ]
    assert not (tmp_path / ".mothership" / "remote-exec-locks").exists()
    assert not fake.run_calls
    assert not fake.streaming_calls

def test_task_lock_failure_to_create_directory_fails_closed(tmp_path):
    (tmp_path / ".mothership").write_text("not a directory")
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["must not run\n"]),
    )
    _assert_task_lock_failure(tmp_path, fake, "mkdirnonce")


def test_task_lock_failure_to_open_file_fails_closed(tmp_path):
    lock_file = _task_lock_path(tmp_path, "t1")
    lock_file.mkdir(parents=True)
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["must not run\n"]),
    )
    _assert_task_lock_failure(tmp_path, fake, "opennonce")


def test_task_lock_failure_to_flock_fails_closed(tmp_path, monkeypatch):
    def fail_flock(_fd, _operation):
        raise OSError("injected lock failure")

    monkeypatch.setattr(fcntl, "flock", fail_flock)
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["must not run\n"]),
    )
    _assert_task_lock_failure(tmp_path, fake, "flocknonce")


def test_task_lock_eacces_from_flock_is_contention(tmp_path, monkeypatch):
    def deny_flock(_fd, _operation):
        raise PermissionError(errno.EACCES, "injected contention")

    monkeypatch.setattr(fcntl, "flock", deny_flock)
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["must not run\n"]),
    )
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=fake, workspace_root=tmp_path,
    )
    assert list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None, deps=deps, nonce="eaccesnonce",
    )) == [
        b"error: remote task 't1' is already running; try again after it finishes\n",
        b"__MSHIP_EXIT__:eaccesnonce 2\n",
    ]
    assert not fake.run_calls
    assert not fake.streaming_calls


def test_run_verb_stream_unknown_verb_raises(tmp_path):
    deps = remote_exec.RemoteExecDeps(config=_config(tmp_path), shell=_FakeShellRunner(), workspace_root=tmp_path)
    try:
        list(remote_exec.run_verb_stream("frobnicate", "t1", ["api"], None, deps=deps, nonce=_TEST_NONCE))
        assert False, "expected UnknownVerbError"
    except remote_exec.UnknownVerbError:
        pass


# --- Task 6 hardening: unknown repo must not raise a raw KeyError mid-stream ---


def test_run_verb_stream_unknown_repo_does_not_raise_keyerror(tmp_path):
    """Task 3 left `config.repos[repo_name]` an unguarded dict index — an
    unknown repo name used to raise a raw KeyError mid-generator. It must
    instead fail cleanly: a clear error line + a non-zero __MSHIP_EXIT__,
    with NO task ever executed (checked upfront, before the per-repo loop)."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["should not run\n"], returncode=0))
    deps = remote_exec.RemoteExecDeps(config=_config(tmp_path), shell=fake, workspace_root=tmp_path)

    lines = list(remote_exec.run_verb_stream("run", "t1", ["ghost-repo"], None, deps=deps, nonce=_TEST_NONCE))
    text = [l.decode() for l in lines]
    assert text[-1].startswith(f"{remote_exec.EXIT_MARKER}:{_TEST_NONCE} ")
    exit_code = int(text[-1].split(" ", 1)[1].strip())
    assert exit_code != 0
    assert any("ghost-repo" in l for l in text[:-1])
    assert not fake.streaming_calls, "no task should run once an unknown repo is detected"


def test_run_verb_stream_unknown_repo_among_known_ones_rejects_before_any_task_runs(tmp_path):
    """A mix of a known + unknown repo must fail before the known repo's task
    ever executes (fail fast on the whole request), not partway through."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0))
    deps = remote_exec.RemoteExecDeps(config=_config(tmp_path), shell=fake, workspace_root=tmp_path)

    lines = list(remote_exec.run_verb_stream("run", "t1", ["api", "ghost-repo"], None, deps=deps, nonce=_TEST_NONCE))
    text = [l.decode() for l in lines]
    exit_code = int(text[-1].split(" ", 1)[1].strip())
    assert exit_code != 0
    assert not fake.streaming_calls


def test_generator_selects_server_owned_repo_names_preserving_request_order(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    server_api = next(iter(config.repos))
    server_web = "".join(["w", "eb"])
    web_path = tmp_path / "web"
    web_path.mkdir()
    config.repos[server_web] = RepoConfig(path=web_path, type="service")

    external_api = (" " + server_api)[1:]
    external_web = (" " + server_web)[1:]
    assert external_api == server_api and external_api is not server_api
    assert external_web == server_web and external_web is not server_web

    materialized_names = []
    run_ref_names = []

    def observe_materialize(
        shell,
        repo_path,
        worktree_path,
        branch,
        *,
        repo_name,
        run_ref=None,
        cancel_event=None,
    ):
        materialized_names.append(repo_name)

    def observe_run_ref(task, repo):
        run_ref_names.append(repo)
        return f"refs/mship/run/{task}/{repo}"

    monkeypatch.setattr(remote_exec, "materialize_worktree", observe_materialize)
    monkeypatch.setattr(remote_exec, "build_run_ref", observe_run_ref)
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["ok\n"]),
    )
    deps = remote_exec.RemoteExecDeps(
        config=config,
        shell=fake,
        workspace_root=tmp_path,
    )

    lines = list(remote_exec.run_verb_stream(
        "run",
        "t1",
        [external_web, external_api, external_web],
        None,
        deps=deps,
        nonce="canonicalrepos",
        run_ref_repos=[external_web, external_api, external_web],
    ))

    assert lines[-1] == b"__MSHIP_EXIT__:canonicalrepos 0\n"
    assert len(materialized_names) == 2
    assert materialized_names[0] is server_web
    assert materialized_names[1] is server_api
    assert len(run_ref_names) == 2
    assert any(name is server_api for name in run_ref_names)
    assert any(name is server_web for name in run_ref_names)
    assert [call["cwd"].name for call in fake.streaming_calls] == [
        server_web,
        server_api,
        server_web,
    ]


def test_exec_unknown_repo_in_request_fails_cleanly_not_500(tmp_path, monkeypatch):
    """Through the HTTP layer: an unknown repo name is conveyed as DATA (200
    + an error line + non-zero exit sentinel), exactly like a failing task —
    never a 500 and never a truncated/broken stream."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/run", json={"task": "t1", "repos": ["ghost-repo"]})
    assert r.status_code == 200
    nonce = _nonce_of(r)
    lines = r.content.decode().splitlines()
    assert lines[-1].startswith(f"{remote_exec.EXIT_MARKER}:{nonce} ")
    assert lines[-1] != _exit_line(nonce, 0)
    assert any("ghost-repo" in ln for ln in lines[:-1])
    assert not fake.streaming_calls


# --- Task 6 hardening: a branch-materialize failure surfaces cleanly, named with the repo ---


def test_run_verb_stream_materialize_failure_surfaces_repo_and_stops(tmp_path, monkeypatch):
    """If `git fetch`/`git worktree add` fails while materializing a repo's
    task branch, run_verb_stream must not silently continue on to run the
    task against a missing/stale worktree — it should fail cleanly, naming
    the repo, via the same data-conveyed error-line + non-zero-exit pattern."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["should not run\n"], returncode=0))

    def _failing_run(command, cwd, env=None, cancel_event=None):
        fake.run_calls.append((command, Path(cwd)))
        if command.startswith("git worktree add"):
            return ShellResult(returncode=128, stdout="", stderr="fatal: could not create worktree")
        return ShellResult(returncode=0, stdout="", stderr="")

    fake.run = _failing_run
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/run", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200
    nonce = _nonce_of(r)
    lines = r.content.decode().splitlines()
    assert lines[-1].startswith(f"{remote_exec.EXIT_MARKER}:{nonce} ")
    assert lines[-1] != _exit_line(nonce, 0)
    assert any("api" in ln and "fatal: could not create worktree" in ln for ln in lines[:-1])
    assert not fake.streaming_calls, "the task must not run once materialize fails"


# --- FIX A: git_root child must materialize its PARENT first (parent-first) ---


class _SnapshotShellRunner(_FakeShellRunner):
    """Records a snapshot of the git commands issued SO FAR at the moment
    each task launch (`run_streaming`) happens — lets a test prove ordering
    (e.g. the parent's `git worktree add` ran BEFORE the child's task) from a
    single synchronous generator drain, without wall-clock timing."""

    def run_streaming(self, command, cwd, env=None):
        self.streaming_calls.append({
            "command": command, "cwd": Path(cwd), "env": env,
            "git_at_launch": [c for c, _ in self.run_calls],
        })
        return self._streaming_proc


def test_run_verb_stream_git_root_child_only_materializes_parent(tmp_path):
    """FIX A: a request naming ONLY a `git_root` child must still materialize
    the PARENT top-level repo (fetch + worktree-add) — the child's git tree
    IS the parent's worktree. Without parent-first materialization the parent
    hub worktree was never fetched/created, so the task ran against the serve
    host's stale/source tree."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ran\n"], returncode=0))
    deps = remote_exec.RemoteExecDeps(
        config=_config_with_child(tmp_path), shell=fake, workspace_root=tmp_path
    )

    lines = list(remote_exec.run_verb_stream("run", "t1", ["server"], None, deps=deps, nonce=_TEST_NONCE))
    text = [l.decode() for l in lines]

    commands = [c for c, _ in fake.run_calls]
    # The PARENT ('app') was materialized: its branch fetched and its hub
    # worktree added — none of which happens for a git_root child on its own.
    assert "git fetch origin feat/t1" in commands
    parent_hub = str(tmp_path / ".worktrees" / "t1" / "app")
    assert any(
        c.startswith("git worktree add -B feat/t1 ")
        and parent_hub in c
        and c.endswith(" origin/feat/t1")
        for c in commands
    ), commands

    # The child task ran UNDER the materialized parent hub worktree
    # (<hub>/app/server), not the serve host's source checkout (<tmp>/app/server).
    assert len(fake.streaming_calls) == 1
    assert fake.streaming_calls[0]["cwd"] == tmp_path / ".worktrees" / "t1" / "app" / "server"
    assert text[-1] == f"__MSHIP_EXIT__:{_TEST_NONCE} 0\n"


def test_run_verb_stream_git_root_child_before_parent_materializes_parent_first(tmp_path):
    """FIX A: even when the child is listed BEFORE its parent, the parent is
    materialized before the child's task runs (parent-first), and the parent
    is not re-fetched when the loop later reaches it (idempotent)."""
    fake = _SnapshotShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0))
    deps = remote_exec.RemoteExecDeps(
        config=_config_with_child(tmp_path), shell=fake, workspace_root=tmp_path
    )

    list(remote_exec.run_verb_stream("run", "t1", ["server", "app"], None, deps=deps, nonce=_TEST_NONCE))

    # Child ran first; at that instant the parent's worktree-add had already run.
    child_call = fake.streaming_calls[0]
    assert child_call["cwd"] == tmp_path / ".worktrees" / "t1" / "app" / "server"
    assert any(
        c.startswith("git worktree add -B feat/t1 ") for c in child_call["git_at_launch"]
    ), child_call["git_at_launch"]

    # Parent materialized exactly once (idempotent): a single worktree-add total.
    all_cmds = [c for c, _ in fake.run_calls]
    assert sum(c.startswith("git worktree add") for c in all_cmds) == 1, all_cmds


def test_run_verb_stream_git_root_child_parent_materialize_failure_stops_cleanly(tmp_path):
    """FIX A: if materializing the PARENT (while resolving a git_root child)
    fails, the request fails the same clean way a top-level materialize
    failure does — an error line naming the parent + a non-zero exit — and no
    task runs."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["should not run\n"], returncode=0))

    def _failing_run(command, cwd, env=None, cancel_event=None):
        fake.run_calls.append((command, Path(cwd)))
        if command.startswith("git worktree add"):
            return ShellResult(returncode=128, stdout="", stderr="fatal: could not create worktree")
        return ShellResult(returncode=0, stdout="", stderr="")

    fake.run = _failing_run
    deps = remote_exec.RemoteExecDeps(
        config=_config_with_child(tmp_path), shell=fake, workspace_root=tmp_path
    )

    lines = list(remote_exec.run_verb_stream("run", "t1", ["server"], None, deps=deps, nonce=_TEST_NONCE))
    text = [l.decode() for l in lines]

    assert text[-1].startswith(f"{remote_exec.EXIT_MARKER}:{_TEST_NONCE} ")
    exit_code = int(text[-1].split(" ", 1)[1].strip())
    assert exit_code != 0
    # Error names the PARENT repo (the one whose materialize failed).
    assert any("app" in l and "fatal: could not create worktree" in l for l in text[:-1])
    assert not fake.streaming_calls, "no task must run once the parent materialize fails"


# --- POST /exec/{verb} -------------------------------------------------------


def test_exec_requires_bearer(tmp_path):
    client = TestClient(_app(tmp_path, auth_token="secret"))
    r = client.post("/exec/run", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 401


def test_exec_unknown_verb_is_404(tmp_path, monkeypatch):
    _patch_shell(monkeypatch, _FakeShellRunner(streaming_proc=_FakeProc()))
    client = TestClient(_app(tmp_path))
    r = client.post("/exec/frobnicate", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 404


def test_exec_without_config_is_503(tmp_path, monkeypatch):
    _patch_shell(monkeypatch, _FakeShellRunner(streaming_proc=_FakeProc()))
    app = create_app(
        specs_dir=tmp_path / "specs", state_manager=StateManager(tmp_path / ".mothership"),
        log_manager=None, workspace_root=tmp_path, workspace_name="test-ws",
        # config omitted entirely
    )
    r = TestClient(app).post("/exec/run", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 503


def test_exec_rejects_unsafe_task_name_before_any_command(tmp_path, monkeypatch):
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["pwned\n"], returncode=0)
    )
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    for task in (
        "../escape",
        "a/b",
        ".",
        "..",
        "",
        "api\n",
        "x; touch /tmp/pwned; #",
    ):
        response = client.post(
            "/exec/run",
            json={"task": task, "repos": ["api"]},
        )
        assert response.status_code == 400, task
        assert response.json()["detail"] == "invalid task name"

    assert not fake.run_calls
    assert not fake.streaming_calls


def test_exec_accepts_safe_segment_task_name(tmp_path, monkeypatch):
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0)
    )
    _patch_shell(monkeypatch, fake)

    response = TestClient(_app(tmp_path)).post(
        "/exec/run",
        json={"task": "release.v2_build-1", "repos": ["api"]},
    )

    assert response.status_code == 200
    assert fake.streaming_calls


def test_exec_passes_canonical_task_to_execution(tmp_path, monkeypatch):
    from mship.core import run_ref as run_ref_module

    received_tasks = []

    def canonicalize(task):
        assert task == "external-task"
        return "canonical-task"

    def observe_stream(verb, task, repos, platform, *, nonce, **kwargs):
        received_tasks.append(task)
        yield f"{remote_exec.EXIT_MARKER}:{nonce} 0\n".encode()

    monkeypatch.setattr(
        run_ref_module,
        "canonical_run_ref_segment",
        canonicalize,
        raising=False,
    )
    monkeypatch.setattr(remote_exec, "run_verb_stream", observe_stream)

    response = TestClient(_app(tmp_path)).post(
        "/exec/run",
        json={"task": "external-task", "repos": ["api"]},
    )

    assert response.status_code == 200
    assert received_tasks == ["canonical-task"]


def test_exec_run_materializes_new_worktree_and_streams_output(tmp_path, monkeypatch):
    proc = _FakeProc(stdout_lines=["hello\n", "world\n"], returncode=0)
    fake = _FakeShellRunner(streaming_proc=proc)
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/run", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200
    lines = r.content.decode().splitlines()
    assert "hello" in lines
    assert "world" in lines
    assert lines[-1] == _exit_line(_nonce_of(r), 0)  # trailing exit-code sentinel, conveyed as data

    commands = [c for c, _ in fake.run_calls]
    # Branch materialize: fetch the task's branch, then (no prior worktree at
    # .worktrees/t1/api) create it with `git worktree add -B`, never `-b`
    # (which would fail/duplicate if the branch already existed locally).
    assert "git fetch origin feat/t1" in commands
    assert any(
        c.startswith("git worktree add -B feat/t1 ") and c.endswith(" origin/feat/t1")
        for c in commands
    )

    # The go-task run target ran in the freshly materialized worktree.
    assert len(fake.streaming_calls) == 1
    call = fake.streaming_calls[0]
    assert call["command"] == "task start"
    assert call["cwd"] == tmp_path / ".worktrees" / "t1" / "api"


def test_exec_run_resets_existing_worktree_to_latest_branch(tmp_path, monkeypatch):
    """A second remote run against a worktree that already exists must fetch
    + hard-reset it to the branch's new tip, not try to `worktree add` again."""
    wt = tmp_path / ".worktrees" / "t1" / "api"
    (wt / ".git").mkdir(parents=True)  # simulate a worktree already materialized here
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/run", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200
    commands = [c for c, _ in fake.run_calls]
    assert "git checkout feat/t1" in commands
    assert "git reset --hard origin/feat/t1" in commands
    assert not any(c.startswith("git worktree add") for c in commands)


def test_exec_nonzero_task_exit_conveyed_not_500(tmp_path, monkeypatch):
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["oops\n"], returncode=2))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))
    r = client.post("/exec/run", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200  # a failing remote task is data, not a 5xx
    lines = r.content.decode().splitlines()
    assert "oops" in lines
    assert lines[-1] == _exit_line(_nonce_of(r), 2)


def test_exec_capture_sets_capture_env_contract(tmp_path, monkeypatch):
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["captured\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post(
        "/exec/capture",
        json={"task": "t1", "repos": ["api"], "platform": "ios"},
    )
    assert r.status_code == 200
    call = fake.streaming_calls[0]
    assert call["command"] == "task capture"
    env = call["env"]
    assert env["MSHIP_CAPTURE_PLATFORM"] == "ios"
    assert env["MSHIP_CAPTURE_KINDS"] == "image,layout"
    assert "MSHIP_CAPTURE_DIR" in env
    assert Path(env["MSHIP_CAPTURE_DIR"]).is_absolute()


def test_exec_run_has_no_capture_env(tmp_path, monkeypatch):
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))
    client.post("/exec/run", json={"task": "t1", "repos": ["api"]})
    assert fake.streaming_calls[0]["env"] is None


def test_exec_mos203_warns_when_base_behind_origin(tmp_path, monkeypatch):
    """MOS-203: before materializing, the remote auto-fetches the task's base
    branch; if origin had moved, a warning line surfaces into the stream."""
    rev_responses = {
        "git rev-parse origin/main": [
            ShellResult(returncode=0, stdout="a" * 40 + "\n", stderr=""),  # before the fetch
            ShellResult(returncode=0, stdout="b" * 40 + "\n", stderr=""),  # after the fetch
        ],
    }
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0),
        rev_responses=rev_responses,
    )
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path, config=_config(tmp_path, base_branch="main")))

    r = client.post("/exec/run", json={"task": "t1", "repos": ["api"]})
    lines = r.content.decode().splitlines()
    assert any("base 'main' was behind origin" in ln for ln in lines)
    commands = [c for c, _ in fake.run_calls]
    assert "git fetch origin main" in commands  # auto-fetch happened


def test_exec_mos203_silent_when_base_already_current(tmp_path, monkeypatch):
    rev_responses = {
        "git rev-parse origin/main": [
            ShellResult(returncode=0, stdout="a" * 40 + "\n", stderr=""),
            ShellResult(returncode=0, stdout="a" * 40 + "\n", stderr=""),
        ],
    }
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0),
        rev_responses=rev_responses,
    )
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path, config=_config(tmp_path, base_branch="main")))

    r = client.post("/exec/run", json={"task": "t1", "repos": ["api"]})
    lines = r.content.decode().splitlines()
    assert not any("was behind origin" in ln for ln in lines)


def test_exec_response_streams_over_http_in_multiple_chunks(tmp_path, monkeypatch):
    """HTTP-level sanity: the client sees the output as a sequence of
    chunks/lines (not required to inspect one opaque blob), ending in the
    exit-code sentinel — the true incremental-yield proof lives in
    test_run_verb_stream_yields_lines_as_produced_not_buffered above, which
    exercises the same generator without depending on TestClient/ASGI timing."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["a\n", "b\n", "c\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    with client.stream("POST", "/exec/run", json={"task": "t1", "repos": ["api"]}) as r:
        assert r.status_code == 200
        nonce = _nonce_of(r)  # header arrives before the streamed body
        lines = list(r.iter_lines())
    assert lines == ["a", "b", "c", _exit_line(nonce, 0)]


# --- capture artifact round-trip (Task 4) -----------------------------------


def test_exec_capture_streams_artifact_tar_before_exit_sentinel(tmp_path, monkeypatch):
    """A capture task that writes screen.png + layout.json into
    MSHIP_CAPTURE_DIR produces a stream with a `__MSHIP_ARTIFACTS__ <n>`
    marker, exactly n tar bytes containing both files (by basename, with
    their contents intact), and the exit sentinel still last."""
    fake = _ArtifactWritingShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["captured\n"], returncode=0),
        artifacts={"screen.png": b"\x89PNGfakebytes", "layout.json": b'{"a": 1}'},
    )
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/capture", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200

    nonce = _nonce_of(r)
    lines, tar_bytes, exit_line = _parse_exec_stream(r.content, nonce)
    assert "captured" in lines
    assert exit_line == _exit_line(nonce, 0)
    assert tar_bytes is not None

    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        names = tar.getnames()
        assert "screen.png" in names
        assert "layout.json" in names
        assert tar.extractfile("screen.png").read() == b"\x89PNGfakebytes"
        assert tar.extractfile("layout.json").read() == b'{"a": 1}'


def test_exec_capture_no_artifacts_is_an_error_not_silent_success(tmp_path, monkeypatch):
    """FIX 7 (parity with local `capture.run_capture`): a capture whose task
    exits 0 but produces NO recognized artifact (empty MSHIP_CAPTURE_DIR) is a
    HARD error, not a silent success — the remote emits a "no recognized
    artifact" error line and a NON-ZERO exit sentinel, and never an artifact
    block."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["nothing here\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/capture", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200
    assert b"__MSHIP_ARTIFACTS__" not in r.content
    nonce = _nonce_of(r)
    lines = r.content.decode().splitlines()
    assert lines[-1] != _exit_line(nonce, 0)  # non-zero effective exit
    assert lines[-1].startswith(f"{remote_exec.EXIT_MARKER}:{nonce} ")
    assert any("no recognized artifact" in ln for ln in lines[:-1])
    # The plain task output still streamed before the error line.
    assert "nothing here" in lines


def test_exec_capture_task_failure_emits_no_artifact_block(tmp_path, monkeypatch):
    """Even if files happen to exist in MSHIP_CAPTURE_DIR, a non-zero task
    exit must not produce an artifact block — a failed capture never claims
    to have artifacts."""
    fake = _ArtifactWritingShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["boom\n"], returncode=1),
        artifacts={"screen.png": b"stray-bytes"},
    )
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/capture", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200
    assert b"__MSHIP_ARTIFACTS__" not in r.content
    lines = r.content.decode().splitlines()
    assert lines == ["boom", _exit_line(_nonce_of(r), 1)]


def test_exec_run_and_build_never_emit_artifact_block(tmp_path, monkeypatch):
    """run/build stay stream-only regardless of what MSHIP_CAPTURE_DIR would
    hold — that env var isn't even set for them (see
    test_exec_run_has_no_capture_env), so there's nothing to discover."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    for verb in ("run", "build"):
        r = client.post(f"/exec/{verb}", json={"task": "t1", "repos": ["api"]})
        assert r.status_code == 200
        assert b"__MSHIP_ARTIFACTS__" not in r.content


def test_exec_body_accepts_run_ref_repos(tmp_path, monkeypatch):
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))
    r = client.post(
        "/exec/run", json={"task": "t1", "repos": ["api"], "run_ref_repos": ["api"]},
    )
    assert r.status_code == 200
    # A 200 alone doesn't prove the field was captured — pydantic silently
    # drops unrecognized keys by default, so a body without the field would
    # also 200. Assert directly on the model to catch that.
    assert ExecBody(task="t1", repos=["api"], run_ref_repos=["api"]).run_ref_repos == ["api"]


def test_exec_body_defaults_run_ref_repos_to_empty(tmp_path, monkeypatch):
    """An older client omits the key; the host must behave exactly as before."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    _patch_shell(monkeypatch, fake)
    r = TestClient(_app(tmp_path)).post("/exec/run", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200
    assert any("fetch origin feat/t1" in cmd for cmd, _cwd in fake.run_calls)


# --- exact copy: materializing from a pushed scratch ref ---------------------

import os
import subprocess

from mship.util.shell import ShellRunner

_REAL_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _real_git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env=_REAL_GIT_ENV,
    ).stdout.strip()


def _host_repo_with_run_ref(tmp_path: Path) -> tuple[Path, str, str]:
    """A run-host-shaped repo: a branch tip, plus a scratch ref holding
    DIFFERENT content, as a push from the operator would have left it.

    Deliberately has NO `origin` remote. That is load-bearing, not incidental:
    every git command on the run-ref path runs through `_run_checked`, so if a
    fetch is ever reintroduced there it cannot fail silently — it raises
    `MaterializeError` and every test using this fixture goes red.
    """
    repo = tmp_path / "hostrepo"
    repo.mkdir()
    _real_git("init", "-q", "-b", "main", ".", cwd=repo)
    (repo / "a.txt").write_text("branch tip\n")
    _real_git("add", "-A", cwd=repo)
    _real_git("commit", "-qm", "tip", cwd=repo)
    _real_git("branch", "-f", "feat/t1", "HEAD", cwd=repo)
    tip = _real_git("rev-parse", "HEAD", cwd=repo)

    (repo / "a.txt").write_text("what the operator is editing\n")
    (repo / "untracked.txt").write_text("scratch\n")
    _real_git("add", "-A", cwd=repo)
    _real_git("commit", "-qm", "synthesized", cwd=repo)
    scratch = _real_git("rev-parse", "HEAD", cwd=repo)
    _real_git("update-ref", "refs/mship/run/t1/api", scratch, cwd=repo)

    _real_git("reset", "-q", "--hard", tip, cwd=repo)
    assert _real_git("remote", cwd=repo) == ""      # nothing to fetch from
    return repo, tip, scratch


def test_materialization_passes_dynamic_values_as_single_argv_elements(tmp_path):
    class _ArgvOnlyShell:
        def __init__(self):
            self.calls = []

        def run(self, *args, **kwargs):
            pytest.fail("materialization used the string shell boundary")

        def run_argv(self, args, cwd, env=None, cancel_event=None):
            self.calls.append((list(args), Path(cwd), cancel_event))
            return ShellResult(returncode=0, stdout="", stderr="")

    shell = _ArgvOnlyShell()
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "worktree;literal"
    run_ref = "refs/mship/run/task;literal/api"

    remote_exec.materialize_worktree(
        shell,
        repo,
        worktree,
        "feat/task",
        repo_name="api",
        run_ref=run_ref,
    )

    assert shell.calls == [
        (
            ["git", "worktree", "add", "--detach", str(worktree), run_ref],
            repo,
            None,
        ),
    ]


def test_materialize_from_a_run_ref_creates_a_detached_worktree(tmp_path):
    """ac10, first materialization: no fetch at all, and HEAD is left detached
    because the scratch commit is throwaway state, not a branch."""
    repo, _tip, scratch = _host_repo_with_run_ref(tmp_path)
    worktree = tmp_path / "wt" / "api"

    remote_exec.materialize_worktree(
        ShellRunner(), repo, worktree, "feat/t1",
        repo_name="api", run_ref="refs/mship/run/t1/api",
    )

    assert _real_git("rev-parse", "HEAD", cwd=worktree) == scratch
    assert (worktree / "a.txt").read_text() == "what the operator is editing\n"
    assert (worktree / "untracked.txt").exists()


def test_the_run_ref_worktree_is_detached_not_a_branch(tmp_path):
    """ac14 on the run host: a branch pointing at a synthesized commit would
    dress throwaway state up as history."""
    repo, _tip, _scratch = _host_repo_with_run_ref(tmp_path)
    worktree = tmp_path / "wt" / "api"
    remote_exec.materialize_worktree(
        ShellRunner(), repo, worktree, "feat/t1",
        repo_name="api", run_ref="refs/mship/run/t1/api",
    )
    head_ref = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=worktree,
        capture_output=True, text=True, env=_REAL_GIT_ENV,
    )
    assert head_ref.returncode != 0          # detached: no symbolic HEAD


def test_a_stale_worktree_lands_on_the_pushed_tree_not_the_branch_tip(tmp_path):
    """ac10, the decisive case: an existing worktree sitting on the branch, with
    leftovers from a previous run, ends up at the pushed ref's tree."""
    repo, tip, scratch = _host_repo_with_run_ref(tmp_path)
    worktree = tmp_path / "wt" / "api"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "feat/t1"],
        cwd=repo, capture_output=True, check=True, env=_REAL_GIT_ENV,
    )
    (worktree / "a.txt").write_text("stale local edit\n")
    (worktree / "leftover.txt").write_text("from the last run\n")

    remote_exec.materialize_worktree(
        ShellRunner(), repo, worktree, "feat/t1",
        repo_name="api", run_ref="refs/mship/run/t1/api",
    )

    assert _real_git("rev-parse", "HEAD", cwd=worktree) == scratch != tip
    assert (worktree / "a.txt").read_text() == "what the operator is editing\n"
    assert not (worktree / "leftover.txt").exists()   # cleaned, so the copy is exact
    assert _real_git("status", "--porcelain", cwd=worktree) == ""


def test_re_materializing_never_moves_the_task_branch(tmp_path):
    """ac14 on the re-materialize path, which is where it is easiest to lose:
    checking the BRANCH out before the reset would drag `feat/t1` — a real ref,
    shared with the run host — onto the synthesized commit."""
    repo, tip, scratch = _host_repo_with_run_ref(tmp_path)
    worktree = tmp_path / "wt" / "api"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "feat/t1"],
        cwd=repo, capture_output=True, check=True, env=_REAL_GIT_ENV,
    )

    remote_exec.materialize_worktree(
        ShellRunner(), repo, worktree, "feat/t1",
        repo_name="api", run_ref="refs/mship/run/t1/api",
    )

    assert _real_git("rev-parse", "feat/t1", cwd=repo) == tip != scratch
    assert _real_git("rev-parse", "HEAD", cwd=repo) == tip
    head_ref = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=worktree,
        capture_output=True, text=True, env=_REAL_GIT_ENV,
    )
    assert head_ref.returncode != 0          # detached, so nothing to drag


def test_materializing_leaves_the_run_hosts_own_checkout_alone(tmp_path):
    """The run host's repository is not a scratch pad: materializing must not
    move its HEAD, its branches, or the scratch ref, nor dirty its work tree."""
    repo, tip, scratch = _host_repo_with_run_ref(tmp_path)
    branches_before = _real_git("branch", "--format=%(refname) %(objectname)", cwd=repo)

    remote_exec.materialize_worktree(
        ShellRunner(), repo, tmp_path / "wt" / "api", "feat/t1",
        repo_name="api", run_ref="refs/mship/run/t1/api",
    )

    assert _real_git("rev-parse", "HEAD", cwd=repo) == tip
    assert _real_git("rev-parse", "feat/t1", cwd=repo) == tip
    assert _real_git("rev-parse", "refs/mship/run/t1/api", cwd=repo) == scratch
    assert _real_git("status", "--porcelain", cwd=repo) == ""
    # ac14: no branch was created pointing at the synthesized commit.
    assert _real_git("branch", "--format=%(refname) %(objectname)", cwd=repo) == branches_before


def test_materializing_from_a_run_ref_issues_no_fetch(tmp_path):
    """ac10 as a command-level invariant: origin is not consulted, at all."""
    fake = _FakeShellRunner()
    remote_exec.materialize_worktree(
        fake, tmp_path / "api", tmp_path / "wt" / "api", "feat/t1",
        repo_name="api", run_ref="refs/mship/run/t1/api",
    )
    assert not any("fetch" in cmd for cmd, _cwd in fake.run_calls)
    assert not any("origin" in cmd for cmd, _cwd in fake.run_calls)


def test_re_materializing_an_existing_worktree_issues_no_fetch(tmp_path):
    """The same invariant on the OTHER branch of the function — the path a
    second remote run takes, where a worktree is already there."""
    fake = _FakeShellRunner()
    worktree = tmp_path / "wt" / "api"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n")

    remote_exec.materialize_worktree(
        fake, tmp_path / "api", worktree, "feat/t1",
        repo_name="api", run_ref="refs/mship/run/t1/api",
    )

    assert fake.run_calls                            # it did do something
    assert not any("fetch" in cmd for cmd, _cwd in fake.run_calls)
    assert not any("origin" in cmd for cmd, _cwd in fake.run_calls)


def test_without_a_run_ref_the_branch_path_is_unchanged(tmp_path):
    """ac9: nothing about today's behaviour moves."""
    fake = _FakeShellRunner()
    worktree = tmp_path / "wt" / "api"
    remote_exec.materialize_worktree(
        fake, tmp_path / "api", worktree, "feat/t1", repo_name="api",
    )
    assert [cmd for cmd, _cwd in fake.run_calls] == [
        "git fetch origin feat/t1",
        f"git worktree add -B feat/t1 {worktree} origin/feat/t1",
    ]


# --- exact copy: routing run_ref_repos through the streaming run -------------


def test_run_verb_stream_uses_the_scratch_ref_for_named_repos(tmp_path):
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=fake, workspace_root=tmp_path,
    )

    list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None, deps=deps, nonce=_TEST_NONCE, run_ref_repos=["api"],
    ))

    commands = [cmd for cmd, _cwd in fake.run_calls]
    assert any("refs/mship/run/t1/api" in c for c in commands)
    # ac10: no fetch, not even the MOS-203 base-freshness probe, which exists
    # only to keep this host's view of ORIGIN current.
    assert not any("fetch" in c for c in commands)


def test_a_repo_not_named_still_comes_from_origin(tmp_path):
    """The mixed case: one repo transferred, another clean."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    deps = remote_exec.RemoteExecDeps(
        config=_config_with_child(tmp_path), shell=fake, workspace_root=tmp_path,
    )

    list(remote_exec.run_verb_stream(
        "run", "t1", ["app"], None, deps=deps, nonce=_TEST_NONCE, run_ref_repos=[],
    ))

    commands = [cmd for cmd, _cwd in fake.run_calls]
    assert any("fetch origin feat/t1" in c for c in commands)
    assert not any("refs/mship/run" in c for c in commands)


def test_a_git_root_child_is_materialized_from_its_parents_scratch_ref(tmp_path):
    """ac7 on the run host: one git repository, one ref. The client sends the
    PARENT's name even when only the child was requested."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    deps = remote_exec.RemoteExecDeps(
        config=_config_with_child(tmp_path), shell=fake, workspace_root=tmp_path,
    )

    list(remote_exec.run_verb_stream(
        "run", "t1", ["server"], None, deps=deps, nonce=_TEST_NONCE, run_ref_repos=["app"],
    ))

    commands = [cmd for cmd, _cwd in fake.run_calls]
    assert any("refs/mship/run/t1/app" in c for c in commands)


def test_a_task_name_that_cannot_form_a_ref_fails_cleanly(tmp_path):
    """This direct `remote_exec` unit path deliberately bypasses `/exec` and
    still rejects an invalid ref before commands run. The failure is stream data
    (an error line plus a non-zero exit sentinel), not a raised exception
    mid-generator, matching the unknown-repo guard.
    """
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path), shell=fake, workspace_root=tmp_path,
    )

    lines = [
        l.decode() for l in remote_exec.run_verb_stream(
            "run", "a/b", ["api"], None, deps=deps, nonce=_TEST_NONCE,
            run_ref_repos=["api"],
        )
    ]

    assert lines[-1].startswith(f"{remote_exec.EXIT_MARKER}:{_TEST_NONCE} ")
    assert int(lines[-1].split(" ", 1)[1].strip()) != 0
    assert any("run ref" in l for l in lines[:-1])
    assert fake.streaming_calls == []       # the task never started
    assert fake.run_calls == []             # nor did any git command


def test_the_base_freshness_probe_is_skipped_for_a_run_ref_repo(tmp_path):
    """The hole the plan's own no-fetch assertion cannot see: `_config()` has no
    `base_branch`, so `check_base_freshness` short-circuits and issues no fetch
    even when it IS called. With a base branch configured it fetches, and calling
    it unconditionally reopens exactly what ac10 closes — Task 6 lets a dirty
    repo skip the BEHIND_ORIGIN preflight check ONLY because this path never
    consults origin. So: no fetch, and no origin probe either."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    deps = remote_exec.RemoteExecDeps(
        config=_config(tmp_path, base_branch="main"), shell=fake, workspace_root=tmp_path,
    )

    list(remote_exec.run_verb_stream(
        "run", "t1", ["api"], None, deps=deps, nonce=_TEST_NONCE, run_ref_repos=["api"],
    ))

    commands = [cmd for cmd, _cwd in fake.run_calls]
    assert any("refs/mship/run/t1/api" in c for c in commands)
    assert not any("fetch" in c for c in commands)
    assert not any("origin" in c for c in commands)


def _config_two_repos(tmp_path: Path) -> WorkspaceConfig:
    """Two independent top-level repos, both with a base branch — the mixed
    request's shape: one repo the operator transferred, one still from origin."""
    repos = {}
    for name in ("api", "web"):
        repo_dir = tmp_path / name
        repo_dir.mkdir(exist_ok=True)
        repos[name] = RepoConfig(
            path=repo_dir, type="service", base_branch="main",
            tasks={"run": "start", "capture": "capture", "build": "build"},
        )
    return WorkspaceConfig(workspace="t", repos=repos)


def test_a_mixed_run_routes_each_repo_by_its_own_source(tmp_path):
    """One request, both sources. Each repo's git commands are partitioned by
    the repo they ran in, so neither repo's routing can be inferred from the
    other's — the scratch repo must see no origin traffic at all, and the clean
    repo must still get the full branch path including the MOS-203 probe."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    deps = remote_exec.RemoteExecDeps(
        config=_config_two_repos(tmp_path), shell=fake, workspace_root=tmp_path,
    )

    list(remote_exec.run_verb_stream(
        "run", "t1", ["api", "web"], None, deps=deps, nonce=_TEST_NONCE,
        run_ref_repos=["api"],
    ))

    in_api = [cmd for cmd, cwd in fake.run_calls if cwd == tmp_path / "api"]
    in_web = [cmd for cmd, cwd in fake.run_calls if cwd == tmp_path / "web"]

    assert any("refs/mship/run/t1/api" in c for c in in_api)
    assert not any("fetch" in c for c in in_api)
    assert not any("origin" in c for c in in_api)

    assert "git fetch origin main" in in_web           # MOS-203 probe, still there
    assert "git fetch origin feat/t1" in in_web        # branch path, unchanged
    assert not any("refs/mship/run" in c for c in in_web)

    # Both tasks ran, each in its own worktree.
    assert [c["cwd"] for c in fake.streaming_calls] == [
        tmp_path / ".worktrees" / "t1" / "api",
        tmp_path / ".worktrees" / "t1" / "web",
    ]


def test_exec_endpoint_forwards_run_ref_repos_to_the_run(tmp_path, monkeypatch):
    """End to end over HTTP: the field `ExecBody` already accepts has to reach
    `run_verb_stream`, or the whole transfer is pushed and then ignored."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path, config=_config(tmp_path, base_branch="main")))

    r = client.post(
        "/exec/run", json={"task": "t1", "repos": ["api"], "run_ref_repos": ["api"]},
    )

    assert r.status_code == 200
    commands = [c for c, _cwd in fake.run_calls]
    assert any("refs/mship/run/t1/api" in c for c in commands)
    assert not any("fetch" in c for c in commands)
    assert not any("origin" in c for c in commands)


# --- setup on the run host, keyed --------------------------------------------

def _config_with_setup(tmp_path: Path, *, setup_inputs=None) -> WorkspaceConfig:
    """A repo whose MATERIALIZED WORKTREE really has a Taskfile declaring
    `setup`. The Taskfile has to exist on disk because `taskfile_has_target`
    reads it — the fake shell cannot answer for it."""
    repo_dir = tmp_path / "api"
    repo_dir.mkdir(exist_ok=True)
    worktree = tmp_path / ".worktrees" / "t1" / "api"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  setup:\n    cmds: [echo setup]\n"
        "  start:\n    cmds: [echo run]\n"
    )
    (worktree / "package.json").write_text('{"deps": 1}\n')
    return WorkspaceConfig(
        workspace="t",
        repos={
            "api": RepoConfig(
                path=repo_dir, type="service",
                tasks={"run": "start"},
                setup_inputs=setup_inputs or [],
            ),
        },
    )


def _stream_run(deps, **kw) -> list[str]:
    return [
        l.decode() for l in remote_exec.run_verb_stream(
            "run", "t1", ["api"], None, deps=deps, nonce=_TEST_NONCE, **kw
        )
    ]


def test_setup_runs_the_first_time_a_worktree_is_materialized(tmp_path):
    """ac15: a fresh host builds its dependencies from the delivered source
    instead of failing on missing ones."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    deps = remote_exec.RemoteExecDeps(
        config=_config_with_setup(tmp_path), shell=fake, workspace_root=tmp_path,
    )

    _stream_run(deps)

    assert [c["command"] for c in fake.streaming_calls] == ["task setup", "task start"]
    # WHERE setup ran, not just that it ran: "from the delivered source" is the
    # whole of ac15. Run in the repo's own checkout instead of the materialized
    # worktree, setup would derive dependencies for the run host's stale tree
    # while the operator's exact source executes against them — the
    # exact-source-with-stale-deps trap this feature exists to close. Every
    # other setup test here asserts only the command, which cannot see that.
    worktree = tmp_path / ".worktrees" / "t1" / "api"
    assert [c["cwd"] for c in fake.streaming_calls] == [worktree, worktree]


def test_setup_is_skipped_on_the_next_run(tmp_path):
    """ac16: a source-only iteration pays no setup cost."""
    config = _config_with_setup(tmp_path)
    first = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=first, workspace_root=tmp_path))

    second = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=second, workspace_root=tmp_path))

    assert [c["command"] for c in second.streaming_calls] == ["task start"]


def test_setup_re_runs_when_a_declared_input_changes(tmp_path):
    """ac16: a dependency change pays once."""
    config = _config_with_setup(tmp_path, setup_inputs=["package.json"])
    first = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=first, workspace_root=tmp_path))

    (tmp_path / ".worktrees" / "t1" / "api" / "package.json").write_text('{"deps": 2}\n')
    second = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=second, workspace_root=tmp_path))

    assert [c["command"] for c in second.streaming_calls] == ["task setup", "task start"]


def test_declaring_no_inputs_means_setup_runs_once_only(tmp_path):
    """ac17: nothing to invalidate against, even when a manifest changes."""
    config = _config_with_setup(tmp_path)
    first = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=first, workspace_root=tmp_path))

    (tmp_path / ".worktrees" / "t1" / "api" / "package.json").write_text('{"deps": 9}\n')
    second = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=second, workspace_root=tmp_path))

    assert [c["command"] for c in second.streaming_calls] == ["task start"]


def test_a_failing_setup_fails_the_run_with_its_own_output(tmp_path):
    """ac18: not a downstream error that does not name the real cause."""
    fake = _FakeShellRunner(
        streaming_proc=_FakeProc(stdout_lines=["npm ERR! ENOENT\n"], returncode=3),
    )
    deps = remote_exec.RemoteExecDeps(
        config=_config_with_setup(tmp_path), shell=fake, workspace_root=tmp_path,
    )

    lines = _stream_run(deps)

    assert "npm ERR! ENOENT\n" in lines                       # setup's own output
    assert any(l.startswith("error:") and "setup" in l and "api" in l for l in lines)
    assert lines[-1] == f"{remote_exec.EXIT_MARKER}:{_TEST_NONCE} 3\n"
    assert [c["command"] for c in fake.streaming_calls] == ["task setup"]  # run never started


def test_a_failed_setup_is_not_recorded_as_done(tmp_path):
    """Caching a failure would skip the retry."""
    config = _config_with_setup(tmp_path)
    failing = _FakeShellRunner(streaming_proc=_FakeProc(returncode=1))
    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=failing, workspace_root=tmp_path))

    second = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=second, workspace_root=tmp_path))

    assert [c["command"] for c in second.streaming_calls][0] == "task setup"


def test_a_repo_with_no_setup_target_is_not_failed_over_it(tmp_path):
    """ac15's second half: `task setup` in a repo that never declared one exits
    non-zero, and that must not turn every remote run into a failure."""
    config = _config_with_setup(tmp_path)
    (tmp_path / ".worktrees" / "t1" / "api" / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  start:\n    cmds: [echo run]\n"
    )
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))

    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=fake, workspace_root=tmp_path))

    assert [c["command"] for c in fake.streaming_calls] == ["task start"]


def test_a_repo_declaring_setup_not_applicable_skips_it(tmp_path):
    config = _config_with_setup(tmp_path)
    config.repos["api"].not_applicable = ["setup"]
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))

    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=fake, workspace_root=tmp_path))

    assert [c["command"] for c in fake.streaming_calls] == ["task start"]


def test_setup_honours_an_aliased_target_name(tmp_path):
    """A repo may spell its setup target something else via `tasks:`."""
    config = _config_with_setup(tmp_path)
    config.repos["api"].tasks = {"run": "start", "setup": "bootstrap"}
    (tmp_path / ".worktrees" / "t1" / "api" / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  bootstrap:\n    cmds: [echo boot]\n"
        "  start:\n    cmds: [echo run]\n"
    )
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))

    _stream_run(remote_exec.RemoteExecDeps(
        config=config, shell=fake, workspace_root=tmp_path))

    assert [c["command"] for c in fake.streaming_calls] == ["task bootstrap", "task start"]


class _MaterializingShellRunner(_FakeShellRunner):
    """Like `_FakeShellRunner`, but `.run()` actually creates the worktree
    directory + a `setup`-declaring Taskfile as a side effect of the real
    `git worktree add` command — standing in for what a real git checkout
    does to disk.

    `_config_with_setup`'s fixture pre-creates the Taskfile at the
    destination path regardless of call order, so a test built on it can't
    tell "setup ran before materialization" from "setup ran after" — the
    file is there either way. This fake makes the file's existence
    CONDITIONAL on materialization having actually run, which is the only
    way to pin that ordering.
    """

    def run(self, command, cwd, env=None, cancel_event=None):
        result = super().run(
            command,
            cwd,
            env=env,
            cancel_event=cancel_event,
        )
        if command.startswith("git worktree add"):
            # `git worktree add -B <branch> <worktree_path> origin/<branch>`
            worktree_path = Path(command.split()[5])
            worktree_path.mkdir(parents=True, exist_ok=True)
            (worktree_path / "Taskfile.yml").write_text(
                "version: '3'\ntasks:\n  setup:\n    cmds: [echo setup]\n"
                "  start:\n    cmds: [echo run]\n"
            )
        return result


def test_setup_runs_only_after_materialization_creates_the_worktree(tmp_path):
    """Pins the ordering the plan calls out explicitly: setup must run AFTER
    the worktree is materialized (the files don't exist before that), never
    before. A shell fake that only produces the Taskfile as a side effect of
    the real `git worktree add` command proves it — unlike
    `_config_with_setup`'s pre-created fixture, this one distinguishes "ran
    before materialize" (no Taskfile yet, so setup is skipped as
    not-applicable and only `task start` streams) from "ran after" (Taskfile
    exists, `task setup` streams first)."""
    repo_dir = tmp_path / "api"
    repo_dir.mkdir(exist_ok=True)
    config = WorkspaceConfig(
        workspace="t",
        repos={
            "api": RepoConfig(
                path=repo_dir, type="service",
                tasks={"run": "start"},
            ),
        },
    )
    fake = _MaterializingShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"]))
    deps = remote_exec.RemoteExecDeps(config=config, shell=fake, workspace_root=tmp_path)

    _stream_run(deps)

    assert [c["command"] for c in fake.streaming_calls] == ["task setup", "task start"]
