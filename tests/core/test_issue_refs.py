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
