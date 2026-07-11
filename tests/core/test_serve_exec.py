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

import io
import tarfile
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from mship.core import remote_exec
from mship.core.config import RepoConfig, WorkspaceConfig
from mship.core.serve import create_app
from mship.core.state import StateManager
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
    """Stands in for `mship.util.shell.ShellRunner`. `.run()` records every
    git command issued (command, cwd) and returns a canned `ShellResult`
    looked up by exact command string (`rev_responses` supports a list per
    command so successive calls to the SAME command — e.g. a base-freshness
    probe before and after a fetch — can return different results, modeling
    origin having moved). `.run_streaming()` returns the canned `_FakeProc`
    and records what it was invoked with."""

    def __init__(self, *, streaming_proc=None, rev_responses=None):
        self.run_calls: list[tuple[str, Path]] = []
        self.streaming_calls: list[dict] = []
        self._rev_responses = {k: list(v) for k, v in (rev_responses or {}).items()}
        self._streaming_proc = streaming_proc

    def build_command(self, command, env_runner=None):
        if env_runner:
            return f"{env_runner} {command}"
        return command

    def run(self, command, cwd, env=None):
        self.run_calls.append((command, Path(cwd)))
        seq = self._rev_responses.get(command)
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return ShellResult(returncode=0, stdout="", stderr="")

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


def _parse_exec_stream(content: bytes):
    """Mirror the client-side parse of the `/exec/{verb}` wire framing: text
    lines, optionally one `__MSHIP_ARTIFACTS__ <n>` marker followed by exactly
    `n` raw tar bytes, then the trailing `__MSHIP_EXIT__ <code>` line. Returns
    `(text_lines, tar_bytes_or_None, exit_line)`."""
    lines: list[str] = []
    tar_bytes: bytes | None = None
    idx = 0
    while True:
        nl = content.index(b"\n", idx)
        line = content[idx:nl]
        idx = nl + 1
        if line.startswith(b"__MSHIP_ARTIFACTS__ "):
            n = int(line.split(b" ", 1)[1])
            tar_bytes = content[idx : idx + n]
            idx += n
            continue
        if line.startswith(b"__MSHIP_EXIT__ "):
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

    gen = remote_exec.run_verb_stream("run", "t1", ["api"], None, deps=deps)
    first = next(gen)
    assert first == b"first\n"
    # Not released yet — proves the generator didn't need "second" to produce "first".
    assert not gate.is_set()
    gate.set()
    rest = list(gen)
    assert rest[0] == b"second\n"
    assert rest[-1] == b"__MSHIP_EXIT__ 0\n"


def test_run_verb_stream_unknown_verb_raises(tmp_path):
    deps = remote_exec.RemoteExecDeps(config=_config(tmp_path), shell=_FakeShellRunner(), workspace_root=tmp_path)
    try:
        list(remote_exec.run_verb_stream("frobnicate", "t1", ["api"], None, deps=deps))
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

    lines = list(remote_exec.run_verb_stream("run", "t1", ["ghost-repo"], None, deps=deps))
    text = [l.decode() for l in lines]
    assert text[-1].startswith(f"{remote_exec.EXIT_MARKER} ")
    exit_code = int(text[-1].split(" ", 1)[1].strip())
    assert exit_code != 0
    assert any("ghost-repo" in l for l in text[:-1])
    assert not fake.streaming_calls, "no task should run once an unknown repo is detected"


def test_run_verb_stream_unknown_repo_among_known_ones_rejects_before_any_task_runs(tmp_path):
    """A mix of a known + unknown repo must fail before the known repo's task
    ever executes (fail fast on the whole request), not partway through."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0))
    deps = remote_exec.RemoteExecDeps(config=_config(tmp_path), shell=fake, workspace_root=tmp_path)

    lines = list(remote_exec.run_verb_stream("run", "t1", ["api", "ghost-repo"], None, deps=deps))
    text = [l.decode() for l in lines]
    exit_code = int(text[-1].split(" ", 1)[1].strip())
    assert exit_code != 0
    assert not fake.streaming_calls


def test_exec_unknown_repo_in_request_fails_cleanly_not_500(tmp_path, monkeypatch):
    """Through the HTTP layer: an unknown repo name is conveyed as DATA (200
    + an error line + non-zero exit sentinel), exactly like a failing task —
    never a 500 and never a truncated/broken stream."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["ok\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/run", json={"task": "t1", "repos": ["ghost-repo"]})
    assert r.status_code == 200
    lines = r.content.decode().splitlines()
    assert lines[-1].startswith("__MSHIP_EXIT__ ")
    assert lines[-1] != "__MSHIP_EXIT__ 0"
    assert any("ghost-repo" in ln for ln in lines[:-1])
    assert not fake.streaming_calls


# --- Task 6 hardening: a branch-materialize failure surfaces cleanly, named with the repo ---


def test_run_verb_stream_materialize_failure_surfaces_repo_and_stops(tmp_path, monkeypatch):
    """If `git fetch`/`git worktree add` fails while materializing a repo's
    task branch, run_verb_stream must not silently continue on to run the
    task against a missing/stale worktree — it should fail cleanly, naming
    the repo, via the same data-conveyed error-line + non-zero-exit pattern."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["should not run\n"], returncode=0))

    def _failing_run(command, cwd, env=None):
        fake.run_calls.append((command, Path(cwd)))
        if command.startswith("git worktree add"):
            return ShellResult(returncode=128, stdout="", stderr="fatal: could not create worktree")
        return ShellResult(returncode=0, stdout="", stderr="")

    fake.run = _failing_run
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/run", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200
    lines = r.content.decode().splitlines()
    assert lines[-1].startswith("__MSHIP_EXIT__ ")
    assert lines[-1] != "__MSHIP_EXIT__ 0"
    assert any("api" in ln and "fatal: could not create worktree" in ln for ln in lines[:-1])
    assert not fake.streaming_calls, "the task must not run once materialize fails"


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
    assert lines[-1] == "__MSHIP_EXIT__ 0"  # trailing exit-code sentinel, conveyed as data

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
    assert lines[-1] == "__MSHIP_EXIT__ 2"


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
        lines = list(r.iter_lines())
    assert lines == ["a", "b", "c", "__MSHIP_EXIT__ 0"]


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

    lines, tar_bytes, exit_line = _parse_exec_stream(r.content)
    assert "captured" in lines
    assert exit_line == "__MSHIP_EXIT__ 0"
    assert tar_bytes is not None

    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        names = tar.getnames()
        assert "screen.png" in names
        assert "layout.json" in names
        assert tar.extractfile("screen.png").read() == b"\x89PNGfakebytes"
        assert tar.extractfile("layout.json").read() == b'{"a": 1}'


def test_exec_capture_no_artifacts_emits_no_artifact_block(tmp_path, monkeypatch):
    """A capture whose task produces nothing (empty MSHIP_CAPTURE_DIR) must
    not emit an artifact marker — only the plain stream + exit sentinel."""
    fake = _FakeShellRunner(streaming_proc=_FakeProc(stdout_lines=["nothing here\n"], returncode=0))
    _patch_shell(monkeypatch, fake)
    client = TestClient(_app(tmp_path))

    r = client.post("/exec/capture", json={"task": "t1", "repos": ["api"]})
    assert r.status_code == 200
    assert b"__MSHIP_ARTIFACTS__" not in r.content
    lines = r.content.decode().splitlines()
    assert lines == ["nothing here", "__MSHIP_EXIT__ 0"]


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
    assert lines == ["boom", "__MSHIP_EXIT__ 1"]


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
