"""Tests for body_resolver — `--body-map` parsing and per-repo body loading.

Mirrors the shape of test_base_resolver.py but for PR bodies. See #114.
"""
from pathlib import Path

import pytest

from mship.core.body_resolver import (
    EmptyBodyInMapError,
    InvalidBodyMapError,
    UnknownRepoInBodyMapError,
    load_body_map,
    parse_body_map,
)


def test_parse_body_map_empty_string_returns_empty_dict():
    assert parse_body_map("") == {}
    assert parse_body_map("   ") == {}


def test_parse_body_map_single_pair():
    assert parse_body_map("api=/tmp/api.md") == {"api": "/tmp/api.md"}


def test_parse_body_map_multiple_pairs():
    result = parse_body_map("api=/tmp/a.md,shared=/tmp/s.md")
    assert result == {"api": "/tmp/a.md", "shared": "/tmp/s.md"}


def test_parse_body_map_strips_whitespace():
    assert parse_body_map(" api = /tmp/a.md , shared = /tmp/s.md ") == {
        "api": "/tmp/a.md",
        "shared": "/tmp/s.md",
    }


def test_parse_body_map_missing_equals_raises():
    with pytest.raises(InvalidBodyMapError, match="expected repo=path"):
        parse_body_map("api/tmp/a.md")


def test_parse_body_map_empty_key_raises():
    with pytest.raises(InvalidBodyMapError, match="non-empty"):
        parse_body_map("=/tmp/a.md")


def test_parse_body_map_empty_value_raises():
    with pytest.raises(InvalidBodyMapError, match="non-empty"):
        parse_body_map("api=")


def test_load_body_map_reads_files(tmp_path: Path):
    a = tmp_path / "a.md"
    a.write_text("## API body\n")
    s = tmp_path / "s.md"
    s.write_text("## Shared body\n")
    result = load_body_map(
        {"api": str(a), "shared": str(s)},
        known_repos=["api", "shared", "other"],
    )
    assert result == {"api": "## API body\n", "shared": "## Shared body\n"}


def test_load_body_map_unknown_repo_raises(tmp_path: Path):
    a = tmp_path / "a.md"
    a.write_text("body")
    with pytest.raises(UnknownRepoInBodyMapError, match="ghost"):
        load_body_map({"ghost": str(a)}, known_repos=["api", "shared"])


def test_load_body_map_missing_file_raises(tmp_path: Path):
    with pytest.raises(InvalidBodyMapError, match="Could not read"):
        load_body_map(
            {"api": str(tmp_path / "missing.md")},
            known_repos=["api"],
        )


def test_load_body_map_empty_body_raises(tmp_path: Path):
    empty = tmp_path / "empty.md"
    empty.write_text("   \n  \n")  # whitespace only
    with pytest.raises(EmptyBodyInMapError, match="api"):
        load_body_map({"api": str(empty)}, known_repos=["api"])


def test_load_body_map_empty_input_returns_empty(tmp_path: Path):
    """Empty parsed map produces empty loaded map — finish proceeds with fallback."""
    assert load_body_map({}, known_repos=["api"]) == {}
