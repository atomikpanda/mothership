"""When a run host needs to re-run `task setup`.

Exact source with stale dependencies is its own trap: change a manifest, run
remotely, and the failure is a module-not-found with no visible relationship to
the edit. So the run host DERIVES what git cannot carry — it runs `task setup`
against the source the push just delivered, rather than copying `node_modules`
over the wire.

Running setup on every invocation would defeat the fast loop this exists to
enable, so it is keyed:

  - setup runs the first time a worktree is materialized for a task on a host;
  - and again whenever the repo's declared `setup_inputs` differ from what that
    host last set up at.

A source-only edit — the common case — pays nothing. A dependency change pays
once. A repo declaring no `setup_inputs` gets setup on first materialization
only, because there is nothing to invalidate against.

This is a cache key of exactly the shape any build cache uses, and it has the
same failure mode: a repo whose setup depends on something undeclared will skip
a re-run it needed. The mitigation is that the key is explicit config an
operator can see and widen, not an inferred heuristic they cannot inspect.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

SETUP_STATE_DIRNAME = "remote-setup"

# The key recorded for a repo that declares no inputs: constant, so it matches
# on every run after the first and setup never repeats.
FIRST_MATERIALIZATION_KEY = "first-materialization"

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def _safe(name: str) -> str:
    """A filename component that cannot escape its directory.

    The task name arrives over the wire, where `core/serve.py`'s `_TASK_NAME_RE`
    permits `.` and `/` — so `../..` would otherwise be a legal path component
    here.
    """
    return _UNSAFE.sub("-", name) or "unnamed"


def key_file(workspace_root: Path, task: str, repo: str) -> Path:
    """Where this host records the key it last set `task`/`repo` up at.

    The readable part is sanitized and therefore LOSSY — `_safe` maps every
    unsafe character to `-`, so `api.v2` and `api-v2` both read `api-v2`, and
    `_` survives intact, so a bare `__` join lets the boundary move (`a__b`/`c`
    vs `a`/`b__c`). Either collision would be a correctness bug, not just a
    spurious re-run: two repos sharing a file whose digests happen to agree —
    which they do whenever both declare the same patterns and neither has the
    files yet — let the second read the first's record, match, and skip the
    setup that installs ITS dependencies.

    So identity comes from a digest of the exact pair, and the sanitized names
    are kept only so an operator can tell at a glance whose state a file holds.
    """
    ident = hashlib.sha256(f"{task}\0{repo}".encode("utf-8")).hexdigest()[:16]
    return (
        Path(workspace_root) / ".mothership" / SETUP_STATE_DIRNAME
        / f"{_safe(task)}__{_safe(repo)}__{ident}.key"
    )


def setup_key(worktree_path: Path, patterns: list[str]) -> str:
    """A digest of the declared setup inputs as they exist in this worktree.

    Every match of every pattern contributes its worktree-relative path AND its
    bytes, so a rename, an edit, an addition or a deletion all move the key. A
    pattern that matches nothing contributes nothing but is still folded in as a
    declaration, so WIDENING `setup_inputs` moves the key by itself rather than
    silently reusing a narrower run's result.
    """
    if not patterns:
        return FIRST_MATERIALIZATION_KEY

    digest = hashlib.sha256()
    root = Path(worktree_path)
    for pattern in patterns:
        digest.update(f"\0pattern:{pattern}\0".encode("utf-8"))
        for match in sorted(root.glob(pattern)):
            if not match.is_file():
                continue
            digest.update(str(match.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(match.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def needs_setup(path: Path, key: str) -> bool:
    """True when this host has not recorded a successful setup at `key`."""
    try:
        return path.read_text().strip() != key
    except OSError:
        return True


def record_setup(path: Path, key: str) -> None:
    """Record `key` as set up. Called ONLY after setup exits zero — recording a
    failed setup would cache the failure and skip the retry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{key}\n")
