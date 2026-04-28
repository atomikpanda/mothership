from mship.util.slug import slugify


def test_basic_slugify():
    assert slugify("add labels to tasks") == "add-labels-to-tasks"


def test_strips_special_characters():
    assert slugify("fix auth (login)") == "fix-auth-login"


def test_collapses_multiple_hyphens():
    assert slugify("fix---auth---bug") == "fix-auth-bug"


def test_lowercases():
    assert slugify("Add Labels To Tasks") == "add-labels-to-tasks"


def test_strips_leading_trailing_hyphens():
    assert slugify("--add labels--") == "add-labels"


def test_empty_string():
    assert slugify("") == ""


def test_numbers_preserved():
    assert slugify("fix issue 42") == "fix-issue-42"


def test_first_phrase_split_on_em_dash():
    # Em-dash (U+2014) marks a long elaboration. Take the title only.
    assert slugify("Title — long elaboration with details") == "title"


def test_first_phrase_split_on_colon_space():
    # Colon-space marks a section/topic boundary; "key: value" style.
    assert slugify("auth refactor: switch to JWT and rotate secrets") == "auth-refactor"


def test_first_phrase_split_on_period_space():
    # Period-space marks a sentence boundary. Take the first sentence.
    assert slugify("Fix login bug. Also tidy up CSS.") == "fix-login-bug"


def test_period_without_space_is_not_split():
    # Version numbers / file extensions like "v1.0" or "config.yaml" must
    # survive intact.
    assert slugify("v1.0 release") == "v10-release"


def test_colon_without_space_is_not_split():
    # URL-like strings should survive intact (slugify still strips `:`).
    assert slugify("https://example.com endpoint") == "httpsexamplecom-endpoint"


def test_long_input_truncated_at_word_boundary():
    text = (
        "offboarding progress reporter surface per task offboarding "
        "progress mid flight via a progress reporter di dependency"
    )
    s = slugify(text)
    assert len(s) <= 40
    # Truncation should land on a word boundary, not mid-word.
    assert not s.endswith("-")
    # The first words should survive.
    assert s.startswith("offboarding-progress-reporter")


def test_long_single_word_hard_truncated():
    # No word boundary before the limit → fall back to hard truncation.
    s = slugify("a" * 80)
    assert len(s) <= 40
    assert s == "a" * 40


def test_short_input_unchanged_by_truncation():
    # Truncation should not affect inputs already within limits.
    assert slugify("add labels to tasks") == "add-labels-to-tasks"
