"""`refs/mship/run/<task>/<repo>` — the throwaway namespace, and the only place
its shape is decided."""
import pytest

from mship.core.run_ref import (
    RUN_REF_PREFIX,
    RunRefNameError,
    is_run_ref,
    is_run_ref_segment,
    run_ref,
)


@pytest.mark.parametrize("value", ["t1", "release.v2_build-1"])
def test_run_ref_segment_accepts_safe_values(value):
    assert is_run_ref_segment(value)


@pytest.mark.parametrize(
    "value",
    ["", ".", "..", "../escape", "a/b", "with space", "semi;colon", "api\n"],
)
def test_run_ref_segment_rejects_unsafe_values(value):
    assert not is_run_ref_segment(value)


def test_ref_is_per_task_and_per_repo():
    """ac7: two tasks, or two repos, must never collide on one ref."""
    assert run_ref("t1", "api") == "refs/mship/run/t1/api"
    assert run_ref("t1", "api") != run_ref("t2", "api")
    assert run_ref("t1", "api") != run_ref("t1", "web")


def test_ref_is_outside_refs_heads():
    """Not a branch: `receive.denyCurrentBranch` never applies (verified against
    a real non-bare repo), and it is nothing a human would branch from (ac14)."""
    assert RUN_REF_PREFIX.startswith("refs/mship/")
    assert not run_ref("t1", "api").startswith("refs/heads/")


@pytest.mark.parametrize("bad", [
    "..", ".", "a/b", "", "with space", "semi;colon", "dollar$", "api\n",
])
def test_traversal_and_shell_metachars_are_refused(bad):
    """The ref reaches `git push` / `git reset --hard` through a shell and names
    a file on disk, so anything outside the segment charset is refused up front.

    `api\\n` is in the list because Python's `$` also matches BEFORE a trailing
    newline, so an anchor written `$` accepts one — and a trailing newline
    terminates a shell command. Harmless while nothing follows the ref on those
    command lines; Task 8 onward appends flags after it."""
    with pytest.raises(RunRefNameError):
        run_ref(bad, "api")
    with pytest.raises(RunRefNameError):
        run_ref("t1", bad)


def test_is_run_ref_accepts_exactly_two_segments():
    assert is_run_ref("refs/mship/run/t1/api")
    assert not is_run_ref("refs/mship/run/t1")
    assert not is_run_ref("refs/mship/run/t1/api/extra")


@pytest.mark.parametrize("bad", [
    "refs/heads/main",
    "refs/heads/../mship/run/t1/api",
    "refs/mship/runaway/t1/api",
    "refs/mship/run/../../heads/main",
    "HEAD",
    "",
    "refs/mship/run/t1/api\n",
])
def test_is_run_ref_refuses_everything_else(bad):
    """The trailing-newline case is the scope control's own business, not git's.
    Real receive-pack does refuse `refs/mship/run/t1/api\\n` ("refusing to update
    funny ref"), so nothing was ever written — but this check advertises itself
    as the last line of defence, and being saved by git downstream is not that."""
    assert not is_run_ref(bad)
