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
