import pytest

from mship.core.clone_url import resolve_clone_url
from mship.core.config import RepoConfig


def _repo(url=None):
    return RepoConfig(path="x", type="library", url=url)


@pytest.mark.parametrize("url, default_remote, expected", [
    # full URL / SSH → as-is
    ("https://example.com/o/r.git", None, "https://example.com/o/r.git"),
    ("  https://example.com/o/r.git  ", None, "https://example.com/o/r.git"),  # leading/trailing whitespace stripped
    ("git@github.com:o/r.git", None, "git@github.com:o/r.git"),
    ("file:///tmp/src", None, "file:///tmp/src"),
    # owner/repo → github
    ("atomikpanda/mothership", None, "https://github.com/atomikpanda/mothership"),
    ("atomikpanda/mothership", "https://github.com/other", "https://github.com/atomikpanda/mothership"),  # default_remote ignored for owner/repo
    # bare repo → default_remote/repo
    ("mothership", "https://github.com/atomikpanda", "https://github.com/atomikpanda/mothership"),
    # trailing slash on default_remote normalized
    ("mothership", "https://github.com/atomikpanda/", "https://github.com/atomikpanda/mothership"),
    # bare repo, no default_remote → None
    ("mothership", None, None),
])
def test_resolve_with_url(url, default_remote, expected):
    assert resolve_clone_url("name-ignored", _repo(url), default_remote) == expected


def test_resolve_omitted_uses_member_name():
    assert resolve_clone_url(
        "ground-control", _repo(None), "https://github.com/atomikpanda"
    ) == "https://github.com/atomikpanda/ground-control"


def test_resolve_omitted_no_default_remote_is_none():
    assert resolve_clone_url("lib", _repo(None), None) is None
