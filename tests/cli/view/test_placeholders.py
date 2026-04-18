import pytest

from mship.cli.view._placeholders import placeholder_for
from mship.core.task_resolver import (
    AmbiguousTaskError,
    NoActiveTaskError,
    UnknownTaskError,
)


def test_no_active_task_placeholder():
    err = NoActiveTaskError()
    text = placeholder_for(err)
    assert "No active task" in text
    assert "mship spawn" in text


def test_ambiguous_placeholder_lists_slugs():
    err = AmbiguousTaskError(active=["alpha", "beta"])
    text = placeholder_for(err)
    assert "Multiple active tasks" in text
    assert "alpha" in text
    assert "beta" in text
    assert "--task" in text
    assert "MSHIP_TASK" in text


def test_unknown_slug_placeholder_names_slug():
    err = UnknownTaskError(slug="missing-one")
    text = placeholder_for(err)
    assert "missing-one" in text
    assert "Waiting" in text or "not found" in text


def test_unknown_exception_type_reraised():
    class _Other(Exception):
        pass
    with pytest.raises(_Other):
        placeholder_for(_Other("oops"))
