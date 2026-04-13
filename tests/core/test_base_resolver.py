import pytest

from mship.core.base_resolver import (
    parse_base_map,
    resolve_base,
    InvalidBaseMapError,
    UnknownRepoInBaseMapError,
)


# --- parse_base_map ---

def test_parse_empty_is_empty():
    assert parse_base_map("") == {}


def test_parse_single_pair():
    assert parse_base_map("cli=main") == {"cli": "main"}


def test_parse_multiple_pairs():
    assert parse_base_map("cli=main,api=release/x") == {"cli": "main", "api": "release/x"}


def test_parse_tolerates_whitespace():
    assert parse_base_map("  cli = main , api=release/x ") == {"cli": "main", "api": "release/x"}


def test_parse_rejects_missing_equals():
    with pytest.raises(InvalidBaseMapError):
        parse_base_map("cli,api=main")


def test_parse_rejects_empty_key():
    with pytest.raises(InvalidBaseMapError):
        parse_base_map("=main")


def test_parse_rejects_empty_value():
    with pytest.raises(InvalidBaseMapError):
        parse_base_map("cli=")


# --- resolve_base ---

class _RepoCfg:
    def __init__(self, base_branch=None):
        self.base_branch = base_branch


KNOWN = {"cli": _RepoCfg("main"), "api": _RepoCfg("cli-refactor"), "schemas": _RepoCfg(None)}


def test_resolve_uses_config_when_no_cli_args():
    assert resolve_base("cli", KNOWN["cli"], cli_base=None, base_map={}, known_repos=KNOWN.keys()) == "main"


def test_resolve_returns_none_when_config_unset_and_no_cli_args():
    assert resolve_base("schemas", KNOWN["schemas"], cli_base=None, base_map={}, known_repos=KNOWN.keys()) is None


def test_resolve_cli_base_overrides_config():
    assert resolve_base("cli", KNOWN["cli"], cli_base="develop", base_map={}, known_repos=KNOWN.keys()) == "develop"


def test_resolve_base_map_overrides_cli_base():
    # most-specific wins
    result = resolve_base("cli", KNOWN["cli"], cli_base="develop", base_map={"cli": "release/7"}, known_repos=KNOWN.keys())
    assert result == "release/7"


def test_resolve_base_map_overrides_config_even_without_cli_base():
    result = resolve_base("cli", KNOWN["cli"], cli_base=None, base_map={"cli": "release/7"}, known_repos=KNOWN.keys())
    assert result == "release/7"


def test_resolve_cli_base_used_when_config_unset():
    result = resolve_base("schemas", KNOWN["schemas"], cli_base="develop", base_map={}, known_repos=KNOWN.keys())
    assert result == "develop"


def test_resolve_rejects_unknown_repo_in_map():
    with pytest.raises(UnknownRepoInBaseMapError) as exc:
        resolve_base("cli", KNOWN["cli"], cli_base=None, base_map={"nope": "main"}, known_repos=KNOWN.keys())
    assert "nope" in str(exc.value)
