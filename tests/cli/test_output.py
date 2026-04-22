import io
import json
import sys
from io import StringIO
from unittest.mock import patch

from mship.cli.output import Output


class _TTYStream:
    def __init__(self):
        self._buf = io.StringIO()
    def write(self, s):
        self._buf.write(s)
    def flush(self):
        pass
    def isatty(self):
        return True
    def getvalue(self):
        return self._buf.getvalue()


class _NonTTYStream:
    def __init__(self):
        self._buf = io.StringIO()
    def write(self, s):
        self._buf.write(s)
    def flush(self):
        pass
    def isatty(self):
        return False
    def getvalue(self):
        return self._buf.getvalue()


def test_is_tty_false_when_piped():
    fake_stdout = StringIO()
    output = Output(stream=fake_stdout)
    assert output.is_tty is False


def test_format_json():
    fake_stdout = StringIO()
    output = Output(stream=fake_stdout)
    output.json({"status": "ok", "phase": "dev"})
    result = json.loads(fake_stdout.getvalue())
    assert result["status"] == "ok"


def test_format_warning():
    fake_stdout = StringIO()
    output = Output(stream=fake_stdout)
    output.warning("Tests not passing")
    assert "Tests not passing" in fake_stdout.getvalue()


def test_format_error():
    fake_stderr = StringIO()
    output = Output(stream=StringIO(), err_stream=fake_stderr)
    output.error("Something failed")
    assert "Something failed" in fake_stderr.getvalue()


def test_format_success():
    fake_stdout = StringIO()
    output = Output(stream=fake_stdout)
    output.success("All tests passed")
    assert "All tests passed" in fake_stdout.getvalue()


def test_breadcrumb_writes_to_stderr_on_tty():
    out = _TTYStream()
    err = _TTYStream()
    output = Output(stream=out, err_stream=err)
    output.breadcrumb("→ task: foo  (resolved via cwd)")
    assert "→ task: foo" in err.getvalue()
    # Not on stdout.
    assert out.getvalue() == ""
    # No "ERROR:" prefix.
    assert "ERROR" not in err.getvalue()


def test_breadcrumb_suppressed_on_non_tty():
    out = _NonTTYStream()
    err = _NonTTYStream()
    output = Output(stream=out, err_stream=err)
    output.breadcrumb("→ task: foo")
    assert out.getvalue() == ""
    assert err.getvalue() == ""
