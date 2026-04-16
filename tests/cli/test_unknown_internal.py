"""Silent fallback for unknown `_`-prefixed internal commands (issue #38)."""
import pytest

from mship.cli import _should_silent_exit


class TestShouldSilentExit:
    def test_unknown_underscore_command_triggers_silent_exit(self):
        assert _should_silent_exit(["mship", "_log-commit"]) is True

    def test_known_underscore_command_does_not(self):
        # _check-commit / _post-checkout / _journal-commit are registered
        assert _should_silent_exit(["mship", "_check-commit", "/tmp"]) is False
        assert _should_silent_exit(["mship", "_journal-commit"]) is False
        assert _should_silent_exit(["mship", "_post-checkout", "a", "b"]) is False

    def test_non_underscore_unknown_command_does_not(self):
        """Unknown non-prefixed commands keep their normal error behavior —
        those are user-facing typos, not hook-renamed internals."""
        assert _should_silent_exit(["mship", "doesnotexist"]) is False
        assert _should_silent_exit(["mship", "klose"]) is False

    def test_known_public_command_does_not(self):
        assert _should_silent_exit(["mship", "status"]) is False
        assert _should_silent_exit(["mship", "close"]) is False

    def test_no_subcommand_does_not(self):
        assert _should_silent_exit(["mship"]) is False
        assert _should_silent_exit([]) is False

    def test_only_underscore_no_name_does_not_match_any_known(self):
        # A bare `_` isn't registered; treat as unknown internal → silent.
        assert _should_silent_exit(["mship", "_"]) is True

    def test_underscore_with_extra_args_still_matches(self):
        assert _should_silent_exit(["mship", "_old-renamed-thing", "arg1", "arg2"]) is True
