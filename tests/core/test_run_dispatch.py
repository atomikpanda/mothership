def test_resumable_prompt_flags_prior_work():
    from mship.core.run_dispatch import resumable_dispatch
    base = "## Task\nImplement X"
    out = resumable_dispatch(base_prompt=base, branch="feat/x", commits_ahead=3,
                             recent_journal=["wrote parser", "tests green"])
    assert "RESUMING" in out and "feat/x" in out and "3 commit" in out
    assert "wrote parser" in out and base in out


def test_fresh_prompt_unchanged():
    from mship.core.run_dispatch import resumable_dispatch
    base = "## Task\nImplement X"
    assert resumable_dispatch(base_prompt=base, branch="feat/x", commits_ahead=0,
                              recent_journal=[]) == base
