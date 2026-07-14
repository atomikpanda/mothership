"""Materialize a workspace from a fresh clone: clone missing members, set them
up, install git hooks, then run doctor. See spec mship-bootstrap (MOS-180)."""
from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from mship.core.clone_url import resolve_clone_url
from mship.core.config import ConfigLoader, RepoConfig, unique_git_roots
from mship.core.gh_auth import broker_config_from_env, resolve_token, git_cred_args
from mship.core.gh_preflight import repo_owner_names_from_config
from mship.util.shell import ShellRunner


def _looks_like_auth_failure(stderr: str) -> bool:
    s = stderr.lower()
    return any(k in s for k in (
        "authentication failed", "could not read username",
        "terminal prompts disabled", "permission denied", "403",
        "fatal: could not read",
    ))


@dataclass(frozen=True)
class MemberResult:
    name: str
    status: str  # "present" | "cloned" | "error"
    message: str


@dataclass(frozen=True)
class BootstrapReport:
    members: tuple[MemberResult, ...]
    doctor_ok: bool | None  # None when doctor was not run (clone errors / load failed)

    @property
    def has_errors(self) -> bool:
        return any(m.status == "error" for m in self.members)


def _clone_one(
    name: str, repo: RepoConfig, default_remote: str | None,
    workspace_root: Path, shell: ShellRunner, token: str | None = None,
) -> MemberResult:
    path = Path(repo.path)
    # No-clobber: lexists() is True for an existing dir OR a (possibly broken)
    # symlink, so neither is ever re-pointed, reset, or overwritten.
    if os.path.lexists(path):
        return MemberResult(name, "present", f"already present at {path}")

    url = resolve_clone_url(name, repo, default_remote)
    if url is None:
        return MemberResult(
            name, "error",
            "no resolvable url — set `url` on the member or `default_remote` "
            "on the workspace",
        )

    if token:
        cred_args, cred_env = git_cred_args(token)
        prefix = " ".join(shlex.quote(a) for a in cred_args) + " "
    else:
        prefix, cred_env = "", None
    res = shell.run(
        f"git {prefix}clone {shlex.quote(url)} {shlex.quote(str(path))}",
        cwd=workspace_root, env=cred_env,
    )
    if res.returncode != 0:
        hint = ""
        if token is None and _looks_like_auth_failure(res.stderr):
            hint = (" — authentication failed and no GH_TOKEN/GITHUB_TOKEN found; "
                    "set a token with repo scope or pass --token")
        return MemberResult(
            name, "error",
            f"clone failed: {res.stderr.strip()[:200] or 'unknown'}{hint}",
        )

    target = repo.expected_branch or repo.base_branch
    if target:
        cur = shell.run("git rev-parse --abbrev-ref HEAD", cwd=path).stdout.strip()
        if cur != target:
            co = shell.run(f"git checkout {shlex.quote(target)}", cwd=path)
            if co.returncode != 0:
                return MemberResult(
                    name, "cloned",
                    f"cloned from {url}; could not checkout {target}: "
                    f"{co.stderr.strip()[:120]}",
                )
    return MemberResult(name, "cloned", f"cloned from {url}")


def bootstrap(
    config_path: Path,
    shell: ShellRunner,
    *,
    state_dir: Path,
    repos: list[str] | None = None,
    token: str | None = None,
) -> BootstrapReport:
    config_path = Path(config_path)
    workspace_root = config_path.parent
    config = ConfigLoader.load(config_path, require_paths=False)

    # Repo set for the broker-pull fallback: every repo in the workspace config
    # that is its own GitHub repo (git_root repos are subdirectories of their
    # parent's checkout, not independently-installed repos — excluded so a
    # broker mint request never names a repo the App can't see).
    all_repo_names = [n for n, r in config.repos.items() if r.git_root is None]
    # Send the broker `owner/repo` slugs (not short config names) so the folded
    # serve can resolve the GitHub App installation per repo. Drop any repo that
    # doesn't resolve to a github.com owner/repo; fall back to the short names if
    # none resolve (Broker A ignores `repos`, so that stays backward-safe).
    _owner_map = repo_owner_names_from_config(config_path, all_repo_names)
    broker_repos = [_owner_map[n] for n in all_repo_names if n in _owner_map]
    broker_url, broker_bearer = broker_config_from_env()
    resolved_token = resolve_token(
        token, broker_url=broker_url, broker_bearer=broker_bearer,
        repos=broker_repos or all_repo_names,
    )

    names = repos or list(config.repos.keys())
    # A workspace with no `repos:` now loads (#259); bootstrapping it is a no-op, so say so
    # explicitly rather than silently "succeeding" with nothing cloned.
    if not names:
        raise ValueError(
            "this workspace has no repos configured to bootstrap — "
            "add a `repos:` map to mothership.yaml."
        )
    unknown = [n for n in names if n not in config.repos]
    if unknown:
        raise ValueError(
            f"Unknown repo name(s): {sorted(unknown)}. "
            f"Valid repos: {sorted(config.repos)}"
        )
    # git_root repos are subdirectories of their parent repo's checkout — they
    # are materialized when the parent is cloned, never cloned independently.
    names = [n for n in names if config.repos[n].git_root is None]
    results: list[MemberResult] = [
        _clone_one(n, config.repos[n], config.default_remote, workspace_root, shell,
                   resolved_token)
        for n in names
    ]

    cloned = [r.name for r in results if r.status == "cloned"]

    # task setup for freshly-cloned members (best-effort; a failure only annotates
    # the message — it must NOT change the member's "cloned" status).
    setup_failures: dict[str, str] = {}
    if cloned and shutil.which("task") is not None:
        for name in cloned:
            repo = config.repos[name]
            if "setup" in repo.not_applicable:
                continue
            actual = repo.tasks.get("setup", "setup")
            setup = shell.run_task(
                "setup", actual, cwd=Path(repo.path),
                env_runner=repo.env_runner or config.env_runner,
            )
            if setup.returncode != 0:
                setup_failures[name] = setup.stderr.strip()[:120]
    if setup_failures:
        results = [
            MemberResult(r.name, r.status,
                         f"{r.message}; setup failed: {setup_failures[r.name]}")
            if r.name in setup_failures else r
            for r in results
        ]

    # Install git hooks on each unique git root that is now present.
    present = [r.name for r in results if r.status in ("cloned", "present")]
    if present:
        from mship.core.hooks import install_hook
        for root in unique_git_roots(config, present):
            try:
                install_hook(root)
            except Exception:
                pass  # hook install is best-effort; doctor will flag if missing

    # Doctor — only meaningful once every member is present (strict load works).
    doctor_ok: bool | None = None
    if not any(r.status == "error" for r in results):
        try:
            strict = ConfigLoader.load(config_path, require_paths=True)
            from mship.core.doctor import DoctorChecker
            report = DoctorChecker(
                strict, shell, state_dir=state_dir, workspace_root=workspace_root
            ).run()
            doctor_ok = report.ok
        except Exception:
            doctor_ok = None

    return BootstrapReport(members=tuple(results), doctor_ok=doctor_ok)
