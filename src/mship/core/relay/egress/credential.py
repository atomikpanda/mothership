from __future__ import annotations

from dataclasses import dataclass


class AttachmentHostError(Exception):
    """Refused to attach a credential to a host outside the Attachment's lock."""


@dataclass(frozen=True)
class Attachment:
    """HOW a credential rides on the wire, decoupled from WHAT it is, plus a
    host-lock so a route misconfig can never send a credential to the wrong
    host."""
    header: str
    template: str            # contains `{value}`
    hosts: tuple[str, ...]

    def render(self, value: str) -> str:
        return self.template.format(value=value)

    def apply(self, headers: dict, *, host: str, value: str) -> None:
        if host not in self.hosts:
            raise AttachmentHostError(
                f"refusing to attach {self.header} to {host!r}; "
                f"allowed hosts: {list(self.hosts)}"
            )
        headers[self.header] = self.render(value)


@dataclass(frozen=True)
class Credential:
    value: str
    expires_at: str | None
    attach: Attachment


def github_token_attachment() -> Attachment:
    """GitHub App token rides as `Authorization: token <value>`, locked to
    github.com + api.github.com."""
    return Attachment(
        header="Authorization",
        template="token {value}",
        hosts=("github.com", "api.github.com"),
    )
