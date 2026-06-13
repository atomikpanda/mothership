from __future__ import annotations

import yaml

from mship.core.spec import Spec


class SpecParseError(Exception):
    pass


def parse_spec(text: str) -> Spec:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SpecParseError("spec file missing YAML frontmatter")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise SpecParseError("unterminated YAML frontmatter")
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1:])
    data = yaml.safe_load(fm_text) or {}
    return Spec(**data, body=body)


def serialize_spec(spec: Spec) -> str:
    data = spec.model_dump(mode="json", exclude={"body"})
    fm = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    return f"---\n{fm}---\n{spec.body}"
