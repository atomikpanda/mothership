from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RefUpdate:
    old_oid: str
    new_oid: str
    ref: str


def read_pkt_lines(data: bytes) -> list[bytes]:
    """Return the pkt-line payloads up to (not including) the first flush-pkt.

    Reads the receive-pack command list without touching the packfile that
    follows the flush. 4-hex length frames the line and INCLUDES the 4 length
    bytes; `0000` is the flush-pkt. Malformed framing stops the scan (fail
    closed — the enforcer treats an empty/short command list as a rejectable
    push, never a pass)."""
    out: list[bytes] = []
    i, n = 0, len(data)
    while i + 4 <= n:
        try:
            length = int(data[i : i + 4], 16)
        except ValueError:
            break
        if length == 0:            # flush-pkt: command list ends, packfile follows
            break
        if length < 4 or i + length > n:
            break
        out.append(data[i + 4 : i + length])
        i += length
    return out


def parse_receive_pack_commands(body: bytes) -> list[RefUpdate]:
    """Parse the receive-pack POST body's command list into RefUpdates.

    Each command is `<old> <new> <ref>`; the first carries a trailing
    `\\0<caps>` and every line a trailing `\\n` — both stripped from the ref."""
    cmds: list[RefUpdate] = []
    for line in read_pkt_lines(body):
        text = line.rstrip(b"\n")
        text = text.split(b"\x00", 1)[0]      # drop capabilities on the first line
        parts = text.split(b" ")
        if len(parts) < 3:
            continue
        old, new, ref = parts[0], parts[1], b" ".join(parts[2:])
        cmds.append(
            RefUpdate(
                old_oid=old.decode("ascii", "replace"),
                new_oid=new.decode("ascii", "replace"),
                ref=ref.decode("utf-8", "replace"),
            )
        )
    return cmds
