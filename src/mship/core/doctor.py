import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from mship.core import skill_install as _si
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


def _claude_target(skill_name: str) -> Path:
    return Path.home() / ".claude" / "skills" / skill_name


def _codex_target() -> Path:
    return Path.home() / ".agents" / "skills" / "mothership"


def _intended_target(symlink: Path) -> Path:
    """Read symlink without resolving — works for dangling links."""
    raw = Path(os.readlink(symlink))
    if not raw.is_absolute():
        raw = (symlink.parent / raw).resolve(strict=False)
    return raw


def check_skill_availability() -> list[CheckResult]:
    """One CheckResult per detected agent reporting installed/dangling/foreign."""
    results: list[CheckResult] = []
    pkg_src = _si.pkg_skills_source()
    skill_dirs = _si._iter_skill_dirs(pkg_src)
    total = len(skill_dirs)
    detected = _si._detect_agents()

    if detected.get("claude"):
        installed = dangling = foreign = 0
        for d in skill_dirs:
            target = _claude_target(d.name)
            if not target.exists() and not target.is_symlink():
                continue
            if target.is_symlink():
                intended = _intended_target(target)
                if target.exists() and intended.resolve() == d.resolve():
                    installed += 1
                elif _si.is_owned_target(intended):
                    dangling += 1
                else:
                    foreign += 1
            else:
                foreign += 1
        results.append(_format_skill_check("claude", installed, dangling, foreign, total))

    if detected.get("codex"):
        target = _codex_target()
        installed = dangling = foreign = 0
        if target.is_symlink():
            intended = _intended_target(target)
            if target.exists() and intended.resolve() == pkg_src.resolve():
                installed = total
            elif _si.is_owned_target(intended):
                dangling = total
            else:
                foreign = total
        elif target.exists():
            foreign = total
        results.append(_format_skill_check("codex", installed, dangling, foreign, total))

    return results


def _format_skill_check(agent: str, installed: int, dangling: int, foreign: int, total: int) -> CheckResult:
    if installed == total and dangling == 0 and foreign == 0:
        return CheckResult(
            name=f"skills/{agent}", status="pass",
            message=f"{installed}/{total} skills installed and current",
        )
    parts = [f"{installed}/{total} installed"]
    if dangling:
        parts.append(f"{dangling} dangling")
    if foreign:
        parts.append(f"{foreign} foreign (skipped)")
    msg = ", ".join(parts) + " — run `mship skill install`"
    if foreign:
        msg += " (use --force to overwrite foreign entries)"
    return CheckResult(name=f"skills/{agent}", status="warn", message=msg)


class DoctorChecker:
    """Run health checks on a mothership workspace."""

    def __init__(
        self,
        config: WorkspaceConfig,
        shell: ShellRunner,
        *,
        state_dir: Path | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self._config = config
        self._shell = shell
        self._state_dir = state_dir
        self._workspace_root = workspace_root

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
                if canonical in repo.not_applicable:
                    report.checks.append(CheckResult(
                        name=f"{name}/task:{canonical}",
                        status="pass",
                        message=f"task '{canonical}' not applicable (declared)",
                    ))
                    continue
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
                    message=f"mship git hooks installed at {root}/.git/hooks/",
                ))
            else:
                report.checks.append(CheckResult(
                    name=hook_name, status="warn",
                    message=(
                        f"git hooks missing or incomplete at {root}/.git/hooks/. "
                        f"Expected mship blocks in pre-commit, post-checkout, post-commit. "
                        f"Run `mship init --install-hooks` to install."
                    ),
                ))

        # Symlink-gitignore footgun check (#72).
        from mship.core.worktree import _symlink_gitignore_footgun
        for name, repo in self._config.repos.items():
            if not repo.symlink_dirs:
                continue
            if repo.git_root is not None:
                parent = self._config.repos[repo.git_root]
                check_path = Path(parent.path).resolve()
            else:
                check_path = Path(repo.path).resolve()
            if not (check_path / ".git").exists():
                continue  # can't check-ignore without a git repo
            for dir_name in repo.symlink_dirs:
                if _symlink_gitignore_footgun(check_path, dir_name):
                    report.checks.append(CheckResult(
                        name=f"{name}/symlink-ignore",
                        status="warn",
                        message=(
                            f"symlink '{dir_name}' is not ignored — "
                            f"add '{dir_name}' (no trailing slash) to .gitignore"
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

        # go-task binary — signals whether spawn will run per-repo setup tasks
        if shutil.which("task") is not None:
            report.checks.append(CheckResult(
                name="go-task",
                status="pass",
                message="go-task found",
            ))
        else:
            report.checks.append(CheckResult(
                name="go-task",
                status="warn",
                message=(
                    "go-task not installed (https://taskfile.dev); "
                    "mship will skip per-repo setup on spawn"
                ),
            ))

        # Pending diagnostics snapshots (spec 2026-04-21).
        if self._state_dir is not None:
            diag_dir = Path(self._state_dir) / "diagnostics"
            if diag_dir.is_dir():
                count = sum(1 for _ in diag_dir.glob("*.json"))
                if count > 0:
                    report.checks.append(CheckResult(
                        name="diagnostics",
                        status="warn",
                        message=(
                            f"{count} snapshot(s) in .mothership/diagnostics/ — "
                            f"review for unexpected-state captures; `rm -rf` to clear"
                        ),
                    ))

        # Dev-mode trap: installed mship may lag workspace source
        mship_source = self._detect_mship_dev_workspace()
        if mship_source is not None:
            report.checks.append(CheckResult(
                name="dev_mode",
                status="warn",
                message=(
                    f"mship dev workspace detected at {mship_source}. "
                    f"The installed `mship` binary may lag your in-progress source. "
                    f"For commands that should run against your local changes "
                    f"(especially audit/finish), invoke `uv run mship <cmd>` from "
                    f"the workspace root instead of `mship <cmd>`."
                ),
            ))

        # env_runner
        env_runner = self._config.env_runner
        if env_runner:
            binary = env_runner.split()[0]
            which_result = self._shell.run(f"which {binary}", cwd=Path("."))
            if which_result.returncode == 0:
                report.checks.append(CheckResult(name="env_runner", status="pass", message=f"{env_runner} — found"))
            else:
                report.checks.append(CheckResult(name="env_runner", status="warn", message=f"{binary} not found in PATH"))

        # Append skill-availability checks (workspace-independent)
        report.checks.extend(check_skill_availability())

        # Workspace .gitignore check: warn if .worktrees not listed
        ws = self._workspace_root
        if ws is not None and (ws / ".git").exists():
            gi = ws / ".gitignore"
            entries = gi.read_text().splitlines() if gi.exists() else []
            if ".worktrees" not in entries:
                report.checks.append(CheckResult(
                    name="workspace/gitignore",
                    status="warn",
                    message=(
                        "workspace .gitignore missing `.worktrees` entry "
                        "(will be added on next spawn)"
                    ),
                ))

        return report

    def _detect_mship_dev_workspace(self) -> Path | None:
        """Return the path of a configured repo whose pyproject declares mothership,
        or None. Used to warn users developing mship-on-mship that the installed
        binary may lag their in-progress source.
        """
        try:
            import tomllib
        except ImportError:  # Python <3.11 — unsupported, but fail safe
            return None
        for name, repo in self._config.repos.items():
            if repo.git_root is not None:
                parent = self._config.repos[repo.git_root]
                effective_path = (parent.path / repo.path).resolve()
            else:
                effective_path = Path(repo.path).resolve()
            pyproject = effective_path / "pyproject.toml"
            if not pyproject.exists():
                continue
            try:
                data = tomllib.loads(pyproject.read_text())
            except Exception:
                continue
            if data.get("project", {}).get("name") == "mothership":
                return effective_path
        return None
