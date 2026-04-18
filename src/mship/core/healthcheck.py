import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import BaseModel

from mship.core.config import Healthcheck
from mship.util.shell import ShellRunner


class HealthcheckResult(BaseModel):
    ready: bool
    message: str
    duration_s: float


def _parse_duration(s: str) -> float:
    """Parse '30s', '500ms', '2m' into seconds."""
    if s.endswith("ms"):
        return float(s[:-2]) / 1000
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60
    raise ValueError(f"Invalid duration: {s!r}")


class HealthcheckRunner:
    def __init__(self, shell: ShellRunner) -> None:
        self._shell = shell

    def wait(
        self,
        healthcheck: Healthcheck,
        repo_path: Path,
        env_runner: str | None = None,
        proc: subprocess.Popen | None = None,
    ) -> HealthcheckResult:
        timeout_s = _parse_duration(healthcheck.timeout)
        interval_s = _parse_duration(healthcheck.retry_interval)
        start = time.monotonic()
        deadline = start + timeout_s

        # sleep probe: unconditional
        if healthcheck.sleep is not None:
            sleep_s = _parse_duration(healthcheck.sleep)
            time.sleep(sleep_s)
            return HealthcheckResult(
                ready=True,
                message=f"slept {healthcheck.sleep}",
                duration_s=sleep_s,
            )

        probe_label = self._probe_label(healthcheck)
        last_error = "no attempts"

        while True:
            elapsed = time.monotonic() - start

            # Fast-fail when the background Popen has crashed. Exit 0 is
            # ignored because many legitimate `run` tasks (e.g., `docker
            # run -d`) detach cleanly; the probe is the right signal for
            # those. Non-zero exit means the task itself died.
            if proc is not None:
                rc = proc.poll()
                if rc is not None and rc != 0:
                    return HealthcheckResult(
                        ready=False,
                        message=(
                            f"background process exited with code {rc} "
                            f"before {probe_label} passed"
                        ),
                        duration_s=elapsed,
                    )

            if healthcheck.tcp is not None:
                ok, err = self._probe_tcp(healthcheck.tcp)
            elif healthcheck.http is not None:
                ok, err = self._probe_http(healthcheck.http)
            elif healthcheck.task is not None:
                ok, err = self._probe_task(healthcheck.task, repo_path, env_runner)
            else:
                ok, err = False, "no probe configured"

            if ok:
                return HealthcheckResult(
                    ready=True,
                    message=f"ready after {elapsed:.1f}s ({probe_label})",
                    duration_s=elapsed,
                )

            last_error = err
            if time.monotonic() + interval_s > deadline:
                break
            time.sleep(interval_s)

        elapsed = time.monotonic() - start
        return HealthcheckResult(
            ready=False,
            message=f"timeout after {elapsed:.1f}s ({probe_label}): {last_error}",
            duration_s=elapsed,
        )

    def _probe_label(self, hc: Healthcheck) -> str:
        if hc.tcp:
            return f"tcp {hc.tcp}"
        if hc.http:
            return f"http {hc.http}"
        if hc.task:
            return f"task {hc.task}"
        return "sleep"

    def _probe_tcp(self, addr: str) -> tuple[bool, str]:
        try:
            host, port_str = addr.rsplit(":", 1)
            port = int(port_str)
        except ValueError:
            return False, f"invalid tcp address: {addr}"
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True, ""
        except (OSError, socket.timeout) as e:
            return False, str(e)

    def _probe_http(self, url: str) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if 200 <= resp.status < 300:
                    return True, ""
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            return False, str(e)

    def _probe_task(
        self, task_name: str, repo_path: Path, env_runner: str | None
    ) -> tuple[bool, str]:
        result = self._shell.run_task(
            task_name=task_name,
            actual_task_name=task_name,
            cwd=repo_path,
            env_runner=env_runner,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()[:100] or f"exit {result.returncode}"
