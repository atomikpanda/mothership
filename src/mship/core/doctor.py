import os
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mship.core import skill_install as _si
from mship.core.config import WorkspaceConfig, resolve_go_task_files, GO_TASK_FILENAMES, unique_git_roots
from mship.util.shell import ShellRunner

_AGENT_RUNTIME_PROBE_TIMEOUT_SECONDS = 5


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


def _shared_agent_skills_target() -> Path:
    return _si.shared_agent_skills_target()


def _intended_target(symlink: Path) -> Path:
    """Read symlink without resolving — works for dangling links."""
    raw = Path(os.readlink(symlink))
    if not raw.is_absolute():
        raw = (symlink.parent / raw).resolve(strict=False)
    return raw


def _classify_directory_skill_target(
    target: Path, pkg_src: Path, total: int
) -> tuple[int, int, int, int]:
    """Return installed, dangling, stale, and foreign counts for a shared link."""
    installed = dangling = stale = foreign = 0
    if target.is_symlink():
        intended = _intended_target(target)
        if target.exists() and intended.resolve() == pkg_src.resolve():
            installed = total
        elif _si.is_owned_target(intended):
            if target.exists():
                stale = total
            else:
                dangling = total
        else:
            foreign = total
    elif target.exists():
        foreign = total
    return installed, dangling, stale, foreign


def check_skill_availability() -> list[CheckResult]:
    """One CheckResult per detected agent reporting skill discovery health."""
    results: list[CheckResult] = []
    pkg_src = _si.pkg_skills_source()
    skill_dirs = _si._iter_skill_dirs(pkg_src)
    total = len(skill_dirs)
    detected = _si._detect_agents()

    if detected.get("claude"):
        installed = dangling = stale = foreign = 0
        for d in skill_dirs:
            target = _claude_target(d.name)
            if not target.exists() and not target.is_symlink():
                continue
            if target.is_symlink():
                intended = _intended_target(target)
                if target.exists() and intended.resolve() == d.resolve():
                    installed += 1
                elif _si.is_owned_target(intended):
                    if target.exists():
                        stale += 1
                    else:
                        dangling += 1
                else:
                    foreign += 1
            else:
                foreign += 1
        results.append(_format_skill_check(
            "claude", installed, dangling, stale, foreign, total,
            repair_command="mship skill install --only claude",
        ))

    shared_target = _shared_agent_skills_target()
    for agent in ("codex", "omp"):
        if not detected.get(agent):
            continue
        installed, dangling, stale, foreign = _classify_directory_skill_target(
            shared_target, pkg_src, total
        )
        results.append(_format_skill_check(
            agent, installed, dangling, stale, foreign, total,
            repair_command=f"mship skill install --only {agent}",
        ))

    return results


def _format_skill_check(
    agent: str,
    installed: int,
    dangling: int,
    stale: int,
    foreign: int,
    total: int,
    *,
    repair_command: str,
) -> CheckResult:
    if installed == total and dangling == 0 and stale == 0 and foreign == 0:
        return CheckResult(
            name=f"skills/{agent}", status="pass",
            message=f"{installed}/{total} skills installed and current",
        )
    parts = [f"{installed}/{total} installed"]
    if dangling:
        parts.append(f"{dangling} dangling")
    if stale:
        parts.append(f"{stale} stale")
    if foreign:
        parts.append(f"{foreign} foreign (skipped)")
        repair_command += " --force"
    msg = ", ".join(parts) + f" — run `{repair_command}`"
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
        config_path: Path | None = None,
        config_source: str | None = None,
        probe_network: bool = True,
    ) -> None:
        self._config = config
        self._shell = shell
        self._state_dir = state_dir
        self._workspace_root = workspace_root
        self._config_path = config_path
        self._config_source = config_source
        self._probe_network = probe_network

    def run(self) -> DoctorReport:
        report = DoctorReport()

        # issue 366 #6: report which config is live and how it resolved.
        if self._config_path is not None:
            report.checks.append(CheckResult(
                name="config",
                status="pass",
                message=(
                    f"config: {Path(self._config_path).resolve()} "
                    f"(resolved via {self._config_source or 'unknown'})"
                ),
            ))

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

            # go-task file(s) — accept any resolution-set spelling; warn when more
            # than one resolves (go-task picks by precedence, so mship must not
            # silently key off a different file than the one `task` runs). #366 #1.
            go_task_files = resolve_go_task_files(effective_path)
            if not go_task_files:
                report.checks.append(CheckResult(
                    name=f"{name}/taskfile", status="fail",
                    message=f"no go-task file found (looked for one of: {', '.join(GO_TASK_FILENAMES)})",
                ))
            elif len(go_task_files) > 1:
                listed = ", ".join(f.name for f in go_task_files)
                report.checks.append(CheckResult(
                    name=f"{name}/taskfile", status="warn",
                    message=(
                        f"multiple go-task files resolve in {effective_path}: {listed} "
                        f"— go-task runs '{go_task_files[0].name}' by precedence; remove "
                        f"or rename the others so mship and go-task agree"
                    ),
                ))
            else:
                report.checks.append(CheckResult(
                    name=f"{name}/taskfile", status="pass",
                    message=f"{go_task_files[0].name} found",
                ))

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
        gh_result = self._shell.run("gh auth status", cwd=self._workspace_root or Path("."))
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
            which_result = self._shell.run(f"which {binary}", cwd=self._workspace_root or Path("."))
            if which_result.returncode == 0:
                report.checks.append(CheckResult(name="env_runner", status="pass", message=f"{env_runner} — found"))
            else:
                report.checks.append(CheckResult(name="env_runner", status="warn", message=f"{binary} not found in PATH"))

        # Append skill-availability checks (workspace-independent)
        report.checks.extend(check_skill_availability())

        if self._workspace_root is not None:
            report.checks.extend(self._agent_integration_checks(self._workspace_root))

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

        # Bundler-exclusion heuristic (issue 366 #7) — best-effort, warn-only.
        if ws is not None and ws.is_dir():
            report.checks.extend(self._check_bundler_exclusions(ws))

        # Connectivity group — sourced from the SINGLE topology implementation
        # (`mship.core.topology.probe_topology`), the same one `mship net status`
        # and `GET /net/topology` use. No probe logic lives here.
        report.checks.extend(self._connectivity_checks())

        return report

    @staticmethod
    def _registered_commands(data: dict, event: str) -> list[str]:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return []
        groups = hooks.get(event)
        if not isinstance(groups, list):
            return []
        return [
            handler["command"]
            for group in groups if isinstance(group, dict)
            for handler in (group.get("hooks") or []) if isinstance(handler, dict)
            if isinstance(handler.get("command"), str)
        ]

    def _json_hook_check(
        self,
        *,
        name: str,
        path: Path,
        commands: dict[str, str],
        registration_issues: Callable[[dict], list[str]] | None = None,
    ) -> CheckResult:
        if not path.is_file():
            return CheckResult(
                name=name,
                status="warn",
                message=f"integration missing at {path}; run `mship init --install-hooks`",
            )
        try:
            data = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError) as exc:
            return CheckResult(
                name=name,
                status="warn",
                message=f"could not read {path}: {exc}",
            )
        except json.JSONDecodeError:
            return CheckResult(
                name=name,
                status="warn",
                message=f"{path} is not valid JSON; fix it before reinstalling hooks",
            )
        if not isinstance(data, dict):
            return CheckResult(name=name, status="warn", message=f"{path} is not a JSON object")

        invalid = (
            registration_issues(data)
            if registration_issues is not None
            else [
                event
                for event, command in commands.items()
                if self._registered_commands(data, event).count(command) != 1
            ]
        )
        if invalid:
            return CheckResult(
                name=name,
                status="warn",
                message=(
                    f"missing or stale Mothership event registrations in {path}: "
                    f"{', '.join(invalid)}; run `mship init --install-hooks`"
                ),
            )
        return CheckResult(name=name, status="pass", message=f"Mothership hooks installed at {path}")

    def _agent_integration_checks(self, workspace_root: Path) -> list[CheckResult]:
        from mship.core.claude_settings import (
            DRAIN_COMMAND,
            GUARD_COMMAND,
            SESSION_COMMAND,
            registration_issues as claude_registration_issues,
        )
        from mship.core.codex_hooks import (
            CODEX_COMMANDS,
            CODEX_FEATURE_ENABLE_COMMAND,
            CODEX_HOOKS_PATH,
            CODEX_TRUST_ACTION,
            CodexHookCapability,
            probe_codex_hook_capability,
            registration_issues as codex_registration_issues,
        )
        from mship.core.omp_extension import (
            OMP_EXTENSION_PATH,
            OMP_EXTENSION_SOURCE,
            OMP_MIN_VERSION,
        )

        root = Path(workspace_root)
        project_roots = unique_git_roots(self._config)
        if not project_roots:
            return []
        codex_paths = [project_root / CODEX_HOOKS_PATH for project_root in project_roots]
        omp_paths = [project_root / OMP_EXTENSION_PATH for project_root in project_roots]
        codex_binary = shutil.which("codex")
        codex_capability = probe_codex_hook_capability(
            self._shell,
            root,
            codex_binary=codex_binary,
        )
        checks = [
            self._json_hook_check(
                name="agent-hooks/claude",
                path=root / ".claude" / "settings.json",
                commands={
                    "SessionStart": SESSION_COMMAND,
                    "PreToolUse": GUARD_COMMAND,
                    "Stop": DRAIN_COMMAND,
                },
                registration_issues=claude_registration_issues,
            ),
        ]

        codex_results = [
            self._json_hook_check(
                name="agent-hooks/codex",
                path=path,
                commands=CODEX_COMMANDS,
                registration_issues=codex_registration_issues,
            )
            for path in codex_paths
        ]
        codex_issues = [result.message for result in codex_results if result.status != "pass"]
        if codex_issues:
            message = "; ".join(codex_issues)
            reinstall_action = "run `mship init --install-hooks`"
            if reinstall_action not in message:
                message += f"; {reinstall_action}"
            if codex_capability.state in {
                CodexHookCapability.DISABLED,
                CodexHookCapability.UNAVAILABLE,
            }:
                message += f"; run `{CODEX_FEATURE_ENABLE_COMMAND}`"
            message += f"; {CODEX_TRUST_ACTION}"
            checks.append(CheckResult(
                name="agent-hooks/codex",
                status="warn",
                message=message,
            ))
        elif codex_capability.state in {
            CodexHookCapability.DISABLED,
            CodexHookCapability.UNAVAILABLE,
        }:
            checks.append(CheckResult(
                name="agent-hooks/codex",
                status="warn",
                message=(
                    "Codex hooks configured but inactive: "
                    f"{codex_capability.detail}; "
                    f"run `{CODEX_FEATURE_ENABLE_COMMAND}`; {CODEX_TRUST_ACTION}"
                ),
            ))
        elif codex_capability.state is CodexHookCapability.ENABLED:
            checks.append(CheckResult(
                name="agent-hooks/codex",
                status="warn",
                message=(
                    "Codex hooks configured; capability enabled; trust still required: "
                    f"{CODEX_TRUST_ACTION}"
                ),
            ))
        else:
            checks.append(CheckResult(
                name="agent-hooks/codex",
                status="warn",
                message=(
                    "Codex hooks configured but not verified active: "
                    f"{codex_capability.detail}; {CODEX_TRUST_ACTION}"
                ),
            ))

        omp_issues: list[str] = []
        for path in omp_paths:
            if not path.is_file():
                omp_issues.append(f"integration missing at {path}")
                continue
            try:
                source = path.read_text()
            except (OSError, UnicodeDecodeError) as exc:
                omp_issues.append(f"could not read {path}: {exc}")
            else:
                if source != OMP_EXTENSION_SOURCE:
                    omp_issues.append(f"stale Mothership extension at {path}")
        if omp_issues:
            checks.append(CheckResult(
                name="agent-hooks/omp",
                status="warn",
                message="; ".join(omp_issues) + "; run `mship init --install-hooks`",
            ))
        else:
            checks.append(CheckResult(
                name="agent-hooks/omp",
                status="pass",
                message="Mothership extension installed at " + ", ".join(map(str, omp_paths)),
            ))

        if codex_capability.state is CodexHookCapability.ENABLED:
            checks.append(CheckResult(
                name="agent-runtime/codex",
                status="pass",
                message=f"Codex hook capability available at {codex_binary}",
            ))
        elif codex_capability.state in {
            CodexHookCapability.DISABLED,
            CodexHookCapability.UNAVAILABLE,
        }:
            checks.append(CheckResult(
                name="agent-runtime/codex",
                status="warn",
                message=(
                    f"{codex_capability.detail}; "
                    f"run `{CODEX_FEATURE_ENABLE_COMMAND}`"
                ),
            ))
        else:
            checks.append(CheckResult(
                name="agent-runtime/codex",
                status="warn",
                message=codex_capability.detail,
            ))

        omp_command = "omp"
        omp_binary = shutil.which(omp_command)
        if omp_binary is None:
            omp_command = "pi"
            omp_binary = shutil.which(omp_command)
        runtime_name = "OMP" if omp_command == "omp" else "Pi"
        if omp_binary is None:
            checks.append(CheckResult(
                name="agent-runtime/omp",
                status="warn",
                message="OMP/Pi is not installed; Claude and Codex integrations remain available",
            ))
        else:
            try:
                version_result = self._shell.run(
                    f"{omp_command} --version",
                    cwd=root,
                    timeout=_AGENT_RUNTIME_PROBE_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                checks.append(CheckResult(
                    name="agent-runtime/omp",
                    status="warn",
                    message=f"{runtime_name} version probe timed out",
                ))
            else:
                match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_result.stdout)
                if version_result.returncode != 0 or match is None:
                    checks.append(CheckResult(
                        name="agent-runtime/omp",
                        status="warn",
                        message=f"could not determine {runtime_name} version or extension compatibility",
                    ))
                else:
                    version = tuple(int(part) for part in match.groups())
                    if version < OMP_MIN_VERSION:
                        checks.append(CheckResult(
                            name="agent-runtime/omp",
                            status="warn",
                            message=(
                                f"{runtime_name} {'.'.join(match.groups())} is too old for this extension; "
                                f"requires {'.'.join(map(str, OMP_MIN_VERSION))} or newer"
                            ),
                        ))
                    else:
                        checks.append(CheckResult(
                            name="agent-runtime/omp",
                            status="pass",
                            message=f"{runtime_name} {'.'.join(match.groups())} supports project extensions",
                        ))
        return checks

    #: topology edge status -> doctor check status. `absent` means "not
    #: configured on this machine", which is not a problem to report.
    _CONNECTIVITY_STATUS = {
        "ok": "pass", "warn": "warn", "fail": "fail", "absent": "pass",
    }

    def _connectivity_checks(self) -> list[CheckResult]:
        if self._state_dir is None or self._workspace_root is None:
            return []
        from mship.core import topology as topo

        try:
            result = topo.probe_topology(
                config=self._config,
                state_dir=self._state_dir,
                workspace_root=self._workspace_root,
                shell=self._shell,
                skip_network=not self._probe_network,
            )
        except Exception as exc:
            # probe_topology promises never to raise; don't let a bug there take
            # down the checks that already ran.
            return [CheckResult(
                name="connectivity", status="warn",
                message=f"connectivity probe failed: {exc}",
            )]

        checks: list[CheckResult] = []
        for edge in result.edges:
            message = edge.detail if edge.fix is None else f"{edge.detail} — {edge.fix}"
            checks.append(CheckResult(
                name=f"connectivity/{edge.name}",
                status=self._CONNECTIVITY_STATUS.get(edge.status, "warn"),
                message=message,
            ))
        return checks

    def _check_bundler_exclusions(self, ws: Path) -> list[CheckResult]:
        """WARN when a known asset-bundling config at the workspace root does not
        exclude `.worktrees`/`.mothership`. Best-effort heuristic (issue 366 #7):
        it inspects a curated set of bundler configs, cannot detect every tool,
        and never raises severity above `warn`.
        """
        results: list[CheckResult] = []
        heur = (
            " (best-effort heuristic — mship cannot detect every bundler; "
            "verify your build excludes `.worktrees`/`.mothership`)"
        )
        tokens = (".worktrees", ".mothership")

        def _excludes(text: str) -> bool:
            return any(tok in text for tok in tokens)

        # Docker
        dockerignore = ws / ".dockerignore"
        dockerfile = ws / "Dockerfile"
        if dockerignore.exists():
            try:
                if not _excludes(dockerignore.read_text()):
                    results.append(CheckResult(
                        name="bundler/docker", status="warn",
                        message=(".dockerignore does not exclude `.worktrees`/"
                                 "`.mothership` — the Docker build context will ship "
                                 "worktree checkouts" + heur),
                    ))
            except OSError:
                pass
        elif dockerfile.exists():
            results.append(CheckResult(
                name="bundler/docker", status="warn",
                message=("Dockerfile present but no .dockerignore excludes "
                         "`.worktrees`/`.mothership`" + heur),
            ))

        # serverless
        for fname in ("serverless.yml", "serverless.yaml"):
            f = ws / fname
            if f.exists():
                try:
                    if not _excludes(f.read_text()):
                        results.append(CheckResult(
                            name="bundler/serverless", status="warn",
                            message=(f"{fname} does not exclude `.worktrees`/"
                                     "`.mothership`" + heur),
                        ))
                except OSError:
                    pass

        # SAM
        for fname in ("template.yaml", "template.yml"):
            f = ws / fname
            if f.exists():
                try:
                    text = f.read_text()
                except OSError:
                    continue
                if "AWS::Serverless" in text and not _excludes(text):
                    results.append(CheckResult(
                        name="bundler/sam", status="warn",
                        message=(f"SAM template {fname} does not exclude "
                                 "`.worktrees`/`.mothership`" + heur),
                    ))

        # npm pack — the `files` field is an ALLOWLIST: `npm pack` ships only what it
        # names, so a `files` list is the SAFE case (it's what protects the package).
        # `.worktrees`/`.mothership` leak ONLY if they are explicitly listed in `files`.
        pkg = ws / "package.json"
        if pkg.exists():
            try:
                import json as _json
                data = _json.loads(pkg.read_text())
            except Exception:
                data = {}
            files = data.get("files") if isinstance(data, dict) else None
            if isinstance(files, list) and any(
                any(tok in str(entry) for tok in tokens) for entry in files
            ):
                results.append(CheckResult(
                    name="bundler/npm", status="warn",
                    message=("package.json `files` explicitly lists `.worktrees`/"
                             "`.mothership` — `npm pack` will ship them" + heur),
                ))

        # CDK Code.fromAsset — shallow scan of workspace-root files only. Bounded so a
        # filesystem error can't crash `mship doctor` and a huge minified bundle isn't
        # read into memory.
        try:
            root_files = list(ws.iterdir())
        except OSError:
            root_files = []
        for f in root_files:
            if not f.is_file() or f.suffix not in (".ts", ".js", ".py"):
                continue
            try:
                if f.stat().st_size > 512_000:  # skip large/minified files
                    continue
                text = f.read_text()
            except OSError:
                continue
            if "Code.fromAsset" in text:
                results.append(CheckResult(
                    name="bundler/cdk", status="warn",
                    message=("CDK `Code.fromAsset` bundling detected at the workspace "
                             "root; ensure the asset root excludes `.worktrees`/"
                             "`.mothership`" + heur),
                ))
                break

        return results

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
