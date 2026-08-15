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


RUN_REF_PREFIX = "refs/mship/run/"
_RUN_REF_SEGMENT_CHARACTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"

# One safe task/repository segment, shared with the remote exec boundary in
# `core/serve.py`. `.` and `..` are excluded outright so no traversal segment
# can exist.


class RunRefNameError(ValueError):
    """A task or repo name that cannot appear in a run ref."""


def canonical_run_ref_segment(value: str) -> str:
    """Validate and rebuild one safe task/repository run-ref segment."""
    if not value or value in {".", ".."}:
        raise RunRefNameError(
            f"name {value!r} cannot be used in a run ref; it must match "
            "[A-Za-z0-9._-]+ and not be '.' or '..'"
        )

    safe_characters = []
    for character in value:
        safe_index = _RUN_REF_SEGMENT_CHARACTERS.find(character)
        if safe_index < 0:
            raise RunRefNameError(
                f"name {value!r} cannot be used in a run ref; it must match "
                "[A-Za-z0-9._-]+ and not be '.' or '..'"
            )
        safe_characters.append(_RUN_REF_SEGMENT_CHARACTERS[safe_index])
    return "".join(safe_characters)


def is_run_ref_segment(value: str) -> bool:
    """Whether `value` is one safe task/repository run-ref segment."""
    try:
        canonical_run_ref_segment(value)
    except RunRefNameError:
        return False
    return True


def run_ref(task: str, repo: str) -> str:
    """`refs/mship/run/<task>/<repo>` — per task AND per git repo, so two tasks
    or two repos running remotely at once cannot overwrite each other's refs."""
    try:
        canonical_task = canonical_run_ref_segment(task)
    except RunRefNameError:
        raise RunRefNameError(
            f"task name {task!r} cannot be used in a run ref; it must match "
            "[A-Za-z0-9._-]+ and not be '.' or '..'"
        ) from None
    try:
        canonical_repo = canonical_run_ref_segment(repo)
    except RunRefNameError:
        raise RunRefNameError(
            f"repo name {repo!r} cannot be used in a run ref; it must match "
            "[A-Za-z0-9._-]+ and not be '.' or '..'"
        ) from None
    return f"{RUN_REF_PREFIX}{canonical_task}/{canonical_repo}"


def is_run_ref(name: str) -> bool:
    """True iff `name` is a ref this feature is allowed to write.

    The receive endpoint's ref-scope control (spec ac5). Strict on purpose:
    exactly the prefix, then exactly two well-formed segments.
    """
    if not name.startswith(RUN_REF_PREFIX):
        return False
    segments = name[len(RUN_REF_PREFIX):].split("/")
    return len(segments) == 2 and all(is_run_ref_segment(s) for s in segments)
