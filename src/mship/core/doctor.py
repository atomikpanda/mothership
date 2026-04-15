from dataclasses import dataclass, field
from pathlib import Path

from mship.core.config import WorkspaceConfig
from mship.util.shell import ShellRunner


@dataclass
class CheckResult:
    name: str
    status: str  # "pass" | "warn" | "fail"
    message: str


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")

    @property
    def errors(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail")

    @property
    def ok(self) -> bool:
        return self.errors == 0


class DoctorChecker:
    """Run health checks on a mothership workspace."""

    def __init__(self, config: WorkspaceConfig, shell: ShellRunner) -> None:
        self._config = config
        self._shell = shell

    def run(self) -> DoctorReport:
        report = DoctorReport()

        for name, repo in self._config.repos.items():
            # Resolve effective path (handles git_root subdir repos)
            if repo.git_root is not None:
                parent = self._config.repos[repo.git_root]
                effective_path = (parent.path / repo.path).resolve()
            else:
                effective_path = repo.path

            # Path exists
            if effective_path.is_dir():
                report.checks.append(CheckResult(name=f"{name}/path", status="pass", message="path exists"))
            else:
                report.checks.append(CheckResult(name=f"{name}/path", status="fail", message=f"path not found: {effective_path}"))
                continue  # skip further checks for this repo

            # Taskfile.yml
            if (effective_path / "Taskfile.yml").exists():
                report.checks.append(CheckResult(name=f"{name}/taskfile", status="pass", message="Taskfile.yml found"))
            else:
                report.checks.append(CheckResult(name=f"{name}/taskfile", status="fail", message="Taskfile.yml not found"))

            # Git — for git_root subdir repos, git lives at the parent's path, not the subdir
            git_check_path = self._config.repos[repo.git_root].path if repo.git_root else effective_path
            if (git_check_path / ".git").exists():
                report.checks.append(CheckResult(name=f"{name}/git", status="pass", message="git initialized"))
            else:
                report.checks.append(CheckResult(name=f"{name}/git", status="warn", message="not a git repository"))

            # Standard tasks (resolved through tasks mapping)
            result = self._shell.run("task --list", cwd=effective_path)
            if result.returncode != 0:
                err_summary = (
                    result.stderr.strip()[:200]
                    if result.stderr
                    else "unknown error"
                )
                report.checks.append(CheckResult(
                    name=f"{name}/taskfile_parse",
                    status="fail",
                    message=f"Taskfile parse error: {err_summary}",
                ))
                continue  # skip per-task checks for this repo
            task_output = result.stdout
            for canonical in ["test", "run", "lint", "setup"]:
                actual = repo.tasks.get(canonical, canonical)
                if actual in task_output:
                    msg = (
                        f"task '{actual}' available"
                        if actual == canonical
                        else f"task '{actual}' available (alias for '{canonical}')"
                    )
                    report.checks.append(CheckResult(
                        name=f"{name}/task:{canonical}",
                        status="pass",
                        message=msg,
                    ))
                else:
                    msg = (
                        f"missing task: {actual}"
                        if actual == canonical
                        else f"missing task: {actual} (aliased from '{canonical}')"
                    )
                    report.checks.append(CheckResult(
                        name=f"{name}/task:{canonical}",
                        status="warn",
                        message=msg,
                    ))

        # Pre-commit hook presence per unique git root
        from mship.core.hooks import is_installed
        from pathlib import Path as _P
        seen_roots: set[_P] = set()
        for name, repo in self._config.repos.items():
            if repo.git_root is not None and repo.git_root in self._config.repos:
                root = _P(self._config.repos[repo.git_root].path).resolve()
            else:
                root = _P(repo.path).resolve()
            if root in seen_roots:
                continue
            seen_roots.add(root)
            if not (root / ".git").exists():
                continue  # doctor already warned about this above
            hook_name = f"hooks/{root.name}"
            if is_installed(root):
                report.checks.append(CheckResult(
                    name=hook_name, status="pass",
                    message=f"pre-commit hook installed at {root}/.git/hooks/pre-commit",
                ))
            else:
                report.checks.append(CheckResult(
                    name=hook_name, status="warn",
                    message=(
                        f"pre-commit hook missing at {root}/.git/hooks/pre-commit. "
                        f"Run `mship init --install-hooks` to install."
                    ),
                ))

        # gh CLI
        gh_result = self._shell.run("gh auth status", cwd=Path("."))
        if gh_result.returncode == 0:
            report.checks.append(CheckResult(name="gh", status="pass", message="authenticated"))
        elif gh_result.returncode == 127:
            report.checks.append(CheckResult(name="gh", status="warn", message="gh CLI not installed (optional — needed for mship finish)"))
        else:
            report.checks.append(CheckResult(name="gh", status="warn", message="gh CLI not authenticated (run gh auth login)"))

        # env_runner
        env_runner = self._config.env_runner
        if env_runner:
            binary = env_runner.split()[0]
            which_result = self._shell.run(f"which {binary}", cwd=Path("."))
            if which_result.returncode == 0:
                report.checks.append(CheckResult(name="env_runner", status="pass", message=f"{env_runner} — found"))
            else:
                report.checks.append(CheckResult(name="env_runner", status="warn", message=f"{binary} not found in PATH"))

        return report
