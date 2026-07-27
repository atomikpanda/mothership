"""`refs/mship/run/<task>/<repo>` — the throwaway namespace, and the only place
its shape is decided."""
import pytest

from mship.core.run_ref import RUN_REF_PREFIX, RunRefNameError, is_run_ref, run_ref


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


@pytest.mark.parametrize("bad", ["..", ".", "a/b", "", "with space", "semi;colon", "dollar$"])
def test_traversal_and_shell_metachars_are_refused(bad):
    """The ref reaches `git push` / `git reset --hard` through a shell and names
    a file on disk, so anything outside the segment charset is refused up front."""
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
])
def test_is_run_ref_refuses_everything_else(bad):
    assert not is_run_ref(bad)
