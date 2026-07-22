from mship.core.relay.egress.pktline import (
    read_pkt_lines, parse_receive_pack_commands, RefUpdate,
)

ZERO = "0" * 40


def pkt(payload: bytes) -> bytes:
    """Frame a payload as a pkt-line: 4-hex length (incl. the 4 prefix bytes)."""
    return b"%04x" % (len(payload) + 4) + payload


FLUSH = b"0000"

# Real client receive-pack command line (git 2.43): create feat/demo.
_CMD = (
    b"0000000000000000000000000000000000000000 "
    b"0cee56ac428e99ad3c1a55a8c6913250e9172578 "
    b"refs/heads/feat/demo\x00 report-status-v2 side-band-64k quiet "
    b"object-format=sha1 agent=git/2.43.0\n"
)


def test_read_pkt_lines_stops_at_flush_and_ignores_packfile_bytes():
    body = pkt(_CMD) + FLUSH + b"PACK\x00\x00garbage-packfile-bytes"
    lines = read_pkt_lines(body)
    assert lines == [_CMD]


def test_parse_receive_pack_commands_strips_caps_and_newline():
    body = pkt(_CMD) + FLUSH + b"PACKxxxx"
    cmds = parse_receive_pack_commands(body)
    assert cmds == [
        RefUpdate(
            old_oid=ZERO,
            new_oid="0cee56ac428e99ad3c1a55a8c6913250e9172578",
            ref="refs/heads/feat/demo",
        )
    ]


def test_parse_multiple_commands_atomic_push():
    line2 = (
        b"1111111111111111111111111111111111111111 "
        b"2222222222222222222222222222222222222222 "
        b"refs/heads/feat/demo\n"
    )
    body = pkt(_CMD) + pkt(line2) + FLUSH
    cmds = parse_receive_pack_commands(body)
    assert [c.ref for c in cmds] == ["refs/heads/feat/demo", "refs/heads/feat/demo"]
    assert cmds[1].old_oid == "1" * 40
