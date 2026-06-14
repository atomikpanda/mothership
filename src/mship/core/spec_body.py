from __future__ import annotations

REQUIRED_SECTIONS: tuple[str, ...] = ("Problem", "User story", "Approach")


def render_body(problem: str, user_story: str, approach: str) -> str:
    return (
        f"## Problem\n\n{problem.strip()}\n\n"
        f"## User story\n\n{user_story.strip()}\n\n"
        f"## Approach\n\n{approach.strip()}\n"
    )


def parse_body_sections(body: str) -> dict[str, str]:
    """Split a markdown body into {section-heading: prose} by `## ` headings."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def validate_body_structure(body: str) -> list[str]:
    """Return the names of any REQUIRED_SECTIONS missing from `body` (empty = ok)."""
    present = parse_body_sections(body)
    return [s for s in REQUIRED_SECTIONS if s not in present]
