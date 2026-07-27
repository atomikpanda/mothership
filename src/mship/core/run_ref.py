"""The scratch-ref namespace used to hand a working tree to a run host.

ONE owner for the ref name, imported by every side that touches it: the client
that pushes (`core/run_transfer.py`), the endpoint that decides which pushes to
accept (`core/git_receive.py`), the run host that materializes from it
(`core/remote_exec.py`), and `mship close`, which deletes it. If the shape ever
changes, it changes here.

`refs/mship/run/<task>/<repo>` is deliberately NOT under `refs/heads/`:

  - `receive.denyCurrentBranch` never applies, so a push lands cleanly in a
    non-bare repo whose branch is checked out (verified against real git);
  - nothing else writes this namespace, which is what makes the force-push per
    run safe;
  - it is not real history and must never become any — no code path branches
    from it, merges it, or opens a PR from it (spec ac14, guarded by
    `tests/core/test_remote_exact_copy_invariants.py`).

The `<repo>` segment is the TOP-LEVEL git repo's name. A `git_root` child has no
git directory of its own — its tree IS its parent's — so parent and child dedupe
to one ref (spec ac7). That collapsing happens in `core/remote_preflight.py`;
this module only refuses to build a name it cannot make safe.
"""
from __future__ import annotations

import re

RUN_REF_PREFIX = "refs/mship/run/"

# One path segment of the ref. Deliberately NARROWER than the task-name charset
# `core/serve.py` accepts for `/exec` (`^[A-Za-z0-9._/-]+$`, which allows `/` and
# a bare `.`): this string is interpolated into `git push` / `git reset --hard`
# run through a shell, and `core/remote_setup.py` derives a filename from the
# same values. `.` and `..` are excluded outright so no traversal segment can
# exist.
#
# Anchored `\Z`, not `$`: Python's `$` also matches BEFORE a trailing newline,
# so `$` accepts `api\n` — and a trailing newline TERMINATES a shell command,
# which is the one character this charset exists to keep out.
_SEGMENT_RE = re.compile(r"\A(?!\.{1,2}\Z)[A-Za-z0-9._-]+\Z")


class RunRefNameError(ValueError):
    """A task or repo name that cannot appear in a run ref."""


def run_ref(task: str, repo: str) -> str:
    """`refs/mship/run/<task>/<repo>` — per task AND per git repo, so two tasks
    or two repos running remotely at once cannot overwrite each other's refs."""
    for label, value in (("task", task), ("repo", repo)):
        if not _SEGMENT_RE.match(value or ""):
            raise RunRefNameError(
                f"{label} name {value!r} cannot be used in a run ref; it must "
                f"match [A-Za-z0-9._-]+ and not be '.' or '..'"
            )
    return f"{RUN_REF_PREFIX}{task}/{repo}"


def is_run_ref(name: str) -> bool:
    """True iff `name` is a ref this feature is allowed to write.

    The receive endpoint's ref-scope control (spec ac5). Strict on purpose:
    exactly the prefix, then exactly two well-formed segments.
    """
    if not name.startswith(RUN_REF_PREFIX):
        return False
    segments = name[len(RUN_REF_PREFIX):].split("/")
    return len(segments) == 2 and all(_SEGMENT_RE.match(s) for s in segments)
