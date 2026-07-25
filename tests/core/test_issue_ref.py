"""Tests for canonical GitHub issue-ref parsing (#386)."""
import pytest

from mship.core.issue_ref import IssueRefError, issue_slug_and_number, issue_url, normalize_issue_ref


def test_full_slug_form():
    assert normalize_issue_ref("acme/widgets#12", default_slug=None) == "acme/widgets#12"


def test_url_form():
    assert normalize_issue_ref(
        "https://github.com/acme/widgets/issues/12", default_slug=None
    ) == "acme/widgets#12"


def test_hash_and_bare_number_use_default_slug():
    assert normalize_issue_ref("#7", default_slug="acme/widgets") == "acme/widgets#7"
    assert normalize_issue_ref("7", default_slug="acme/widgets") == "acme/widgets#7"


def test_bare_number_without_default_slug_fails_loud():
    with pytest.raises(IssueRefError, match="owner/repo#N"):
        normalize_issue_ref("#7", default_slug=None)


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "acme/widgets", "acme#12", "https://github.com/acme/widgets/pull/12"],
)
def test_invalid_refs_rejected(bad):
    with pytest.raises(IssueRefError):
        normalize_issue_ref(bad, default_slug="acme/widgets")


def test_issue_url():
    assert issue_url("acme/widgets#12") == "https://github.com/acme/widgets/issues/12"


def test_issue_slug_and_number():
    assert issue_slug_and_number("acme/widgets#12") == ("acme/widgets", 12)
