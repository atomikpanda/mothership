import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ShellResult:
    returncode: int
    stdout: str
    stderr: str


class ShellRunner:
    """Wraps subprocess execution with optional env_runner prefixing."""

    def build_command(self, command: str, env_runner: str | None = None) -> str:
        if env_runner:
            return f"{env_runner} {command}"
        return command

    def run(
        self, command: str, cwd: Path, env: dict[str, str] | None = None
    ) -> ShellResult:
        import os

        run_env = None
        if env:
            run_env = {**os.environ, **env}
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=run_env,
        )
        return ShellResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def run_task(
        self,
        task_name: str,
        actual_task_name: str,
        cwd: Path,
        env_runner: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ShellResult:
        command = self.build_command(f"task {actual_task_name}", env_runner)
        return self.run(command, cwd, env=env)

    def run_streaming(self, command: str, cwd: Path) -> subprocess.Popen:
        """Run a command with stdout/stderr streaming (for logs, run)."""
        return subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
