"""Pure logic + thin orchestration for `mship capture`.

mship delegates the actual platform capture to a per-repo go-task `capture`
target (adb/simctl/etc.) and only understands the artifact contract: the
target writes conventionally-named files into MSHIP_CAPTURE_DIR; this module
discovers them by a kind->filename map and validates at least one was produced.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class CaptureError(Exception):
    """Capture could not be completed (target failed, or produced no artifact)."""


# kind -> candidate filenames (first existing non-empty file wins per kind)
KIND_FILENAMES: dict[str, tuple[str, ...]] = {
    "image": ("screen.png",),
    "layout": ("layout.xml", "layout.json", "layout.html"),
}
ALL_KINDS: tuple[str, ...] = tuple(KIND_FILENAMES)


@dataclass(frozen=True)
class Artifact:
    kind: str
    path: Path


def resolve_adhoc_repo(repo_names: list[str], repo_flag: str | None) -> str:
    """Pick a repo for an ad-hoc (no active task) capture.

    Capture observes a running app, not worktree source, so it can run without a
    task — against a repo's main checkout. Priority: --repo flag > sole repo >
    CaptureError (ambiguous; caller must pass --repo).
    """
    if repo_flag is not None:
        if repo_flag not in repo_names:
            raise CaptureError(
                f"unknown repo {repo_flag!r}. Workspace repos: {sorted(repo_names)}"
            )
        return repo_flag
    if len(repo_names) == 1:
        return repo_names[0]
    raise CaptureError(
        "no active task and no single repo could be selected; "
        f"pass --repo <name> (workspace repos: {sorted(repo_names)})."
    )


def resolve_kinds(kind_flag: str) -> list[str]:
    """Map the --kind value ('all' | a single kind) to a concrete list."""
    if kind_flag == "all":
        return list(ALL_KINDS)
    if kind_flag not in KIND_FILENAMES:
        raise CaptureError(
            f"unknown kind {kind_flag!r}; expected one of: "
            f"{', '.join(ALL_KINDS)} or 'all'"
        )
    return [kind_flag]


def discover_artifacts(out_dir: Path, kinds: list[str]) -> list[Artifact]:
    """Return the non-empty artifact file produced for each requested kind."""
    found: list[Artifact] = []
    for kind in kinds:
        for name in KIND_FILENAMES[kind]:
            p = out_dir / name
            if p.is_file() and p.stat().st_size > 0:
                found.append(Artifact(kind=kind, path=p))
                break
    return found


def run_capture(
    *,
    shell,
    worktree: Path,
    actual_task_name: str,
    env_runner: str | None,
    platform: str | None,
    kinds: list[str],
    out_dir: Path,
) -> list[Artifact]:
    """Run the repo's capture target, then discover + validate artifacts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "MSHIP_CAPTURE_DIR": str(out_dir),
        "MSHIP_CAPTURE_KINDS": ",".join(kinds),
    }
    if platform is not None:
        env["MSHIP_CAPTURE_PLATFORM"] = platform

    result = shell.run_task(
        "capture", actual_task_name, cwd=worktree, env_runner=env_runner, env=env
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise CaptureError(
            f"capture target failed (exit {result.returncode}):\n{tail}"
        )

    artifacts = discover_artifacts(out_dir, kinds)
    if not artifacts:
        tail = (result.stderr or "").strip()[-2000:]
        raise CaptureError(
            f"capture target produced no recognized artifact in {out_dir} "
            f"for kinds {kinds}."
            + (f" target stderr:\n{tail}" if tail else "")
        )
    return artifacts
