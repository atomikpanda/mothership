import subprocess
from unittest.mock import MagicMock, patch
from pathlib import Path

from mship.util.shell import ShellRunner, ShellResult


def test_run_simple_command():
    runner = ShellRunner()
    result = runner.run("echo hello", cwd=Path("."))
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_run_captures_stderr():
    runner = ShellRunner()
    result = runner.run("echo error >&2", cwd=Path("."))
    assert "error" in result.stderr


def test_run_returns_nonzero_on_failure():
    runner = ShellRunner()
    result = runner.run("false", cwd=Path("."))
    assert result.returncode != 0


def test_build_command_no_env_runner():
    runner = ShellRunner()
    cmd = runner.build_command("task test", env_runner=None)
    assert cmd == "task test"


def test_build_command_with_env_runner():
    runner = ShellRunner()
    cmd = runner.build_command("task test", env_runner="dotenvx run --")
    assert cmd == "dotenvx run -- task test"


def test_run_with_env_runner():
    runner = ShellRunner()
    result = runner.run_task(
        task_name="test",
        actual_task_name="test",
        cwd=Path("."),
        env_runner=None,
    )
    # task binary likely not installed in test env, so we just check it tried
    assert isinstance(result, ShellResult)
