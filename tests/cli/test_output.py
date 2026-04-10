import json
import sys
from io import StringIO
from unittest.mock import patch

from mship.cli.output import Output


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
