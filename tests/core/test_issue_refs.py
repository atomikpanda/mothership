from mship.core.issue_refs import append_closes_footer, extract_issue_refs


def test_extract_empty_returns_empty():
    assert extract_issue_refs([]) == []
    assert extract_issue_refs(["", None]) == []  # type: ignore[list-item]


def test_extract_single_ref():
    assert extract_issue_refs(["fix #3 audit check"]) == [3]


def test_extract_multiple_refs_deduped_and_sorted():
    refs = extract_issue_refs([
        "fix #8 auto link",
        "also closes #3 and #12",
        "mentions #8 again",
    ])
    assert refs == [3, 8, 12]


def test_extract_ignores_identifier_prefix():
    """`abc#3` is an anchor-link-ish pattern, not an issue ref."""
    assert extract_issue_refs(["see section abc#3 for details"]) == []


def test_extract_ignores_double_hash():
    """`##3` is a markdown heading hint, not an issue ref."""
    assert extract_issue_refs(["##3 some heading"]) == []


def test_extract_allows_parens_and_punctuation():
    assert extract_issue_refs(["fix (#3) and closes #7, also #12."]) == [3, 7, 12]


def test_extract_ignores_bare_hash():
    assert extract_issue_refs(["C# is a language", "# heading"]) == []


def test_extract_scans_across_multiple_texts():
    refs = extract_issue_refs(["desc mentions #3", "log says #8", "commit: fix #12"])
    assert refs == [3, 8, 12]


def test_append_closes_footer_empty_refs_is_noop():
    assert append_closes_footer("some body", []) == "some body"


def test_append_closes_footer_single_ref():
    assert append_closes_footer("body", [3]) == "body\n\nCloses #3"


def test_append_closes_footer_multiple_refs():
    assert append_closes_footer("body", [3, 7]) == "body\n\nCloses #3, #7"


def test_append_closes_footer_handles_trailing_newline():
    # Input with a single trailing newline gets a blank-line separator
    assert append_closes_footer("body\n", [3]) == "body\n\nCloses #3"


def test_append_closes_footer_handles_double_trailing_newline():
    assert append_closes_footer("body\n\n", [3]) == "body\n\nCloses #3"
import pytest

from mship.core.issue_refs import IssueRefError, issue_slug_and_number, issue_url, normalize_issue_ref


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


# --- append_linked_closes (#386) ---

def test_linked_closes_same_repo_uses_short_form():
    from mship.core.issue_refs import append_linked_closes
    out = append_linked_closes("Body.", ["acme/widgets#12"], "acme/widgets")
    assert out == "Body.\n\nCloses #12"


def test_linked_closes_cross_repo_uses_full_form():
    from mship.core.issue_refs import append_linked_closes
    out = append_linked_closes("Body.", ["acme/widgets#12"], "acme/other")
    assert out == "Body.\n\nCloses acme/widgets#12"


def test_linked_closes_multiple_and_noop_when_empty():
    from mship.core.issue_refs import append_linked_closes
    out = append_linked_closes("Body.", ["a/b#1", "a/b#2"], "a/b")
    assert out.endswith("Closes #1\nCloses #2")
    assert append_linked_closes("Body.", [], "a/b") == "Body."


def test_linked_closes_skips_refs_already_in_body():
    from mship.core.issue_refs import append_linked_closes
    body = "Body.\n\nCloses #12"
    assert append_linked_closes(body, ["acme/widgets#12"], "acme/widgets") == body


def test_url_form_tolerates_fragment_and_query():
    assert normalize_issue_ref(
        "https://github.com/acme/widgets/issues/12#issuecomment-987654321",
        default_slug=None,
    ) == "acme/widgets#12"
    assert normalize_issue_ref(
        "https://github.com/acme/widgets/issues/12?foo=bar", default_slug=None
    ) == "acme/widgets#12"
