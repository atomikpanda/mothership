from mship.core.spec_body import (
    REQUIRED_SECTIONS, render_body, parse_body_sections, validate_body_structure,
)


def test_render_body_has_all_sections():
    body = render_body("the problem", "as a user...", "the approach")
    for section in REQUIRED_SECTIONS:
        assert f"## {section}" in body
    assert "the problem" in body and "the approach" in body


def test_parse_round_trips_rendered_body():
    body = render_body("P", "U", "A")
    sections = parse_body_sections(body)
    assert sections["Problem"] == "P"
    assert sections["User story"] == "U"
    assert sections["Approach"] == "A"


def test_validate_flags_missing_sections():
    assert validate_body_structure(render_body("p", "u", "a")) == []
    missing = validate_body_structure("## Problem\n\nonly problem\n")
    assert "User story" in missing and "Approach" in missing
