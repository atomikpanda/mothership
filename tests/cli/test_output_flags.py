"""MOS-103: explicit --json / --quiet / --no-color control with documented
precedence (explicit ctor arg > global settings from the CLI callback > env
var > TTY auto-detection)."""
import io
import json
import os
import pty
import subprocess
import sys
from pathlib import Path

import pytest

from mship.cli.output import Output, configure_output, reset_output_settings


class FakeStream(io.StringIO):
    """StringIO that reports a configurable isatty()."""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    # Each test starts from a clean global + no inherited env.
    for var in ("MSHIP_JSON", "MSHIP_QUIET", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    reset_output_settings()
    yield
    reset_output_settings()


def tty(**kw) -> Output:
    return Output(stream=FakeStream(tty=True), err_stream=FakeStream(tty=True), **kw)


def pipe(**kw) -> Output:
    return Output(stream=FakeStream(tty=False), err_stream=FakeStream(tty=False), **kw)


# ---- baseline: TTY auto-detection (unchanged behavior) ----

def test_pipe_defaults_to_json_mode():
    o = pipe()
    assert o.json_mode is True
    assert o.human_mode is False


def test_tty_defaults_to_human_mode():
    o = tty()
    assert o.json_mode is False
    assert o.human_mode is True


# ---- precedence: explicit > settings > env > tty ----

def test_env_msship_json_forces_json_on_a_tty(monkeypatch):
    monkeypatch.setenv("MSHIP_JSON", "1")
    o = tty()
    assert o.json_mode is True
    assert o.human_mode is False


def test_env_msship_json_falsey_forces_text_on_a_pipe(monkeypatch):
    monkeypatch.setenv("MSHIP_JSON", "0")
    o = pipe()
    assert o.json_mode is False


def test_json_forced_off_on_pipe_renders_human_not_json():
    # Forcing JSON off on a pipe must yield human output, not a silent JSON
    # fallback — human_mode is the negation of json_mode, independent of is_tty.
    o = pipe(force_json=False)
    assert o.json_mode is False
    assert o.human_mode is True
    o.table("Repos", ["Repo"], [["mothership"]])
    out = o._stream.getvalue()
    assert '"rows"' not in out  # not the JSON fallback shape
    assert "Repo" in out and "mothership" in out  # rendered as a table


def test_callback_setting_beats_env(monkeypatch):
    # CLI flag (recorded via configure_output) wins over the env var.
    monkeypatch.setenv("MSHIP_JSON", "0")
    configure_output(json=True)
    o = tty()
    assert o.json_mode is True


def test_explicit_ctor_beats_everything(monkeypatch):
    monkeypatch.setenv("MSHIP_JSON", "1")
    configure_output(json=True)
    o = tty(force_json=False)
    assert o.json_mode is False


# ---- quiet ----

def test_quiet_suppresses_warning_and_breadcrumb():
    o = tty(force_quiet=True)
    o.warning("careful")
    o.breadcrumb("→ task: foo")
    assert o._err_stream.getvalue() == ""


def test_quiet_does_not_suppress_errors():
    o = tty(force_quiet=True)
    o.error("boom")
    assert "boom" in o._err_stream.getvalue()


def test_quiet_via_env(monkeypatch):
    monkeypatch.setenv("MSHIP_QUIET", "1")
    o = tty()
    assert o.quiet is True


# ---- no-color ----

def _has_ansi(s: str) -> bool:
    return "\x1b[" in s


def test_tty_emits_color_by_default():
    o = tty()
    o.success("done")
    assert _has_ansi(o._stream.getvalue())


def test_no_color_strips_ansi_on_a_tty():
    o = tty(force_no_color=True)
    o.success("done")
    out = o._stream.getvalue()
    assert "done" in out
    assert not _has_ansi(out)


def test_no_color_via_NO_COLOR_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    o = tty()
    o.success("done")
    assert not _has_ansi(o._stream.getvalue())


def test_json_implies_no_color():
    # --json forces JSON shape (human_mode False) and no color.
    o = tty(force_json=True)
    assert o.json_mode is True
    assert o.use_color is False


# ---- byte-equality: forced --json is identical regardless of TTY ----

def test_forced_json_bytes_identical_tty_vs_pipe():
    payload = {"slug": "x", "phase": "dev", "n": 3}
    a = tty(force_json=True)
    b = pipe(force_json=True)
    a.json(payload)
    b.json(payload)
    assert a._stream.getvalue() == b._stream.getvalue()


# ---- end-to-end (AC): `mship --json <cmd>` over a real pty == over a pipe ----


def _run_mship(args, cwd, *, use_pty: bool) -> str:
    """Run the real CLI in a subprocess, capturing stdout over a pty (so the CLI
    sees a terminal) or a plain pipe. Returns stdout with CR stripped (a pty
    translates \\n -> \\r\\n; that terminal artifact is not part of the payload)."""
    code = (
        "import sys; from mship.cli import run; "
        f"sys.argv = ['mship'] + {args!r}; run()"
    )
    repo_src = str(Path(__file__).resolve().parents[2] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src + os.pathsep + env.get("PYTHONPATH", "")
    if use_pty:
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=slave, stderr=subprocess.DEVNULL, cwd=str(cwd), env=env,
        )
        os.close(slave)
        out = b""
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        proc.wait(timeout=30)
        os.close(master)
    else:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(cwd), env=env,
            timeout=30,
        )
        out = proc.stdout
    return out.decode().replace("\r", "")


def test_json_flag_pty_equals_pipe(workspace):
    # `mship --json graph` must produce identical, valid JSON whether stdout is a
    # terminal (pty) or a pipe — the determinism guarantee for CI / agent pty
    # capture (the core of MOS-103).
    over_pty = _run_mship(["--json", "graph"], workspace, use_pty=True)
    over_pipe = _run_mship(["--json", "graph"], workspace, use_pty=False)
    assert over_pty == over_pipe
    parsed = json.loads(over_pty)
    assert "order" in parsed and "repos" in parsed


def test_json_flag_changes_pty_output(workspace):
    # Without --json a pty gets human output; with --json it gets JSON. Proves the
    # flag actually overrides TTY auto-detection (not a no-op).
    human = _run_mship(["graph"], workspace, use_pty=True)
    forced = _run_mship(["--json", "graph"], workspace, use_pty=True)
    assert human != forced
    json.loads(forced)  # forced output parses as JSON
