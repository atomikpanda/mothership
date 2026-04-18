import io
import threading
import time

import pytest

from mship.util.stream_printer import StreamPrinter, drain_to_printer


class _FakePopen:
    """Minimal Popen-shaped object for drain tests."""
    def __init__(self, stdout_text: str = "", stderr_text: str = ""):
        self.stdout = io.StringIO(stdout_text) if stdout_text else None
        self.stderr = io.StringIO(stderr_text) if stderr_text else None


def test_drain_prints_stdout_lines(capsys):
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen(stdout_text="line1\nline2\n")
    threads = drain_to_printer(proc, "api", printer)
    for t in threads:
        t.join(timeout=1.0)
    out = capsys.readouterr().out
    assert "api  | line1\n" in out
    assert "api  | line2\n" in out


def test_drain_prints_stderr_lines(capsys):
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen(stderr_text="err-line\n")
    threads = drain_to_printer(proc, "api", printer)
    for t in threads:
        t.join(timeout=1.0)
    out = capsys.readouterr().out
    assert "api  | err-line\n" in out


def test_drain_handles_none_streams(capsys):
    """If proc.stdout or proc.stderr is None, drain should not error."""
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen()  # both streams None
    threads = drain_to_printer(proc, "api", printer)
    for t in threads:
        t.join(timeout=1.0)
    # No output, no exceptions
    assert capsys.readouterr().out == ""


def test_drain_prefixes_are_correct_for_multiple_repos(capsys):
    """Two separate drain invocations writing to same printer — both
    lines appear with correct prefixes, no tearing."""
    printer = StreamPrinter(repos=["api", "worker"], use_color=False)
    p1 = _FakePopen(stdout_text="hello from api\n")
    p2 = _FakePopen(stdout_text="hello from worker\n")
    threads = drain_to_printer(p1, "api", printer) + drain_to_printer(p2, "worker", printer)
    for t in threads:
        t.join(timeout=1.0)
    out = capsys.readouterr().out
    assert "api     | hello from api\n" in out
    assert "worker  | hello from worker\n" in out


def test_drain_returns_two_threads():
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen(stdout_text="a\n", stderr_text="b\n")
    threads = drain_to_printer(proc, "api", printer)
    assert len(threads) == 2
    for t in threads:
        t.join(timeout=1.0)


def test_drain_threads_are_daemons():
    printer = StreamPrinter(repos=["api"], use_color=False)
    proc = _FakePopen(stdout_text="a\n")
    threads = drain_to_printer(proc, "api", printer)
    for t in threads:
        assert t.daemon is True
        t.join(timeout=1.0)
