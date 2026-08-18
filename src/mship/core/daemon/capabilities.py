"""What this host publishes about itself (#471 AC6's one owner).

Two surfaces publish the same capability shape and neither can see the other's
code: the host app's `/health` + `/workspaces` responses (`host_app.py`, which
lives inside FastAPI) and the registration payload the daemon signs and POSTs
to the relay (`relay_link.py`, which must import no web framework at all). So
the projection lives here — workspace-free, import-light — and both call it.

#473 fills a real runner state in `runner_block` and nowhere else.
"""
from __future__ import annotations

from collections.abc import Mapping


def runner_block(raw: object | None) -> dict:
    """Project the registry's opaque `runner:` passthrough.

    One projection, one shape, everywhere a runner block is published — #473
    fills `state` in here and nowhere else. Declared-and-enabled reads
    `unknown` because #471 carries runner state but never observes it; anything
    else — absent, disabled, or malformed from a hand-edited registry — reads
    `disabled` rather than failing a request.
    """
    enabled = bool(raw.get("enabled")) if isinstance(raw, Mapping) else False
    return {"enabled": enabled, "state": "unknown" if enabled else "disabled"}


def host_capability_payload(runner: object | None = None) -> dict:
    """The `capabilities` + `runner` pair a registration payload carries.

    Assembled in exactly one function so the flat `capabilities` summary can
    never disagree with the `runner` block beside it: the summary is *derived*
    from that block rather than restated. `tunnel` is unconditionally True —
    only a host that dials the relay ever builds this payload.
    """
    block = runner_block(runner)
    return {
        "capabilities": {"tunnel": True, "runner": block["enabled"]},
        "runner": block,
    }
