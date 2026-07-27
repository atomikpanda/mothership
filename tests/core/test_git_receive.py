"""The scoped git receive path.

Layered on purpose: pure pkt-line framing first (no git, no HTTP), then the
allowlist and receive-pack against REAL repositories, then the FastAPI routes,
then a real `git push` over a real socket. The framing is the security boundary,
so it is tested where nothing can hide behind a mock.
"""
import pytest

from mship.core.git_receive import (
    PktLineError,
    RefScopeError,
    check_ref_scope,
    pkt_line,
    ref_commands,
    service_advertisement,
)

ZERO = "0" * 40
SHA = "9647986511a9eb8e9260ca70fc90406674ece7a9"


def _body(*commands: bytes, pack: bytes = b"") -> bytes:
    return b"".join(pkt_line(c) for c in commands) + b"0000" + pack


def test_pkt_line_prefixes_a_four_hex_length_including_itself():
    assert pkt_line(b"abc") == b"0007abc"


def test_service_advertisement_matches_the_wire_prefix_git_expects():
    """Verified against a real `git push`: this exact prefix, then the
    `git receive-pack --http-backend-info-refs` output verbatim."""
    assert service_advertisement(b"REFS").startswith(b"001f# service=git-receive-pack\n0000")
    assert service_advertisement(b"REFS").endswith(b"REFS")


def test_first_command_capabilities_are_stripped():
    body = _body(f"{ZERO} {SHA} refs/mship/run/t1/api\x00report-status side-band-64k".encode())
    assert ref_commands(body) == ["refs/mship/run/t1/api"]


def test_every_command_is_reported_not_just_the_first():
    body = _body(
        f"{ZERO} {SHA} refs/mship/run/t1/api\x00report-status".encode(),
        f"{ZERO} {SHA} refs/heads/sneaky".encode(),
    )
    assert ref_commands(body) == ["refs/mship/run/t1/api", "refs/heads/sneaky"]


def test_a_delete_command_is_parsed():
    """Deleting a scratch ref is old=<sha>, new=<zeros> — the same ref-name
    control governs it."""
    body = _body(f"{SHA} {ZERO} refs/mship/run/t1/api".encode())
    assert ref_commands(body) == ["refs/mship/run/t1/api"]


def test_trailing_pack_data_is_ignored():
    body = _body(f"{ZERO} {SHA} refs/mship/run/t1/api".encode(), pack=b"PACK\x00\x01binary")
    assert ref_commands(body) == ["refs/mship/run/t1/api"]


@pytest.mark.parametrize("body", [
    b"zzzz0000",                    # length is not hex
    b"0003ab",                      # length below the 4-byte header
    b"00ff" + b"short",             # length runs past the body
    b"0010" + b"only two fields",   # not a ref command
])
def test_unparseable_bodies_raise_rather_than_being_guessed_at(body):
    """A body whose refs cannot be read is a body whose refs cannot be checked."""
    with pytest.raises(PktLineError):
        ref_commands(body)


def test_the_whole_remainder_of_a_command_is_the_ref_name():
    """git takes the ref name as everything after the two oids up to a NUL —
    spaces included: fed this command, real receive-pack answers
    `ng refs/heads/a b funny refname`. A parser that stopped at the next space
    would report the shorter, IN-SCOPE `refs/mship/run/t1/api` for a command git
    reads as a different ref. Read it git's way and the scope check refuses it."""
    command = f"{ZERO} {SHA} refs/mship/run/t1/api refs/heads/evil".encode()
    assert ref_commands(_body(command)) == ["refs/mship/run/t1/api refs/heads/evil"]
    with pytest.raises(RefScopeError):
        check_ref_scope(_body(command))


def test_commands_are_reported_when_the_body_ends_without_a_flush():
    """Only a flush ends the command list. Truncating the body instead makes
    real receive-pack die without touching a ref, so this is belt and braces —
    but the check must never see FEWER refs than git might act on, and a parser
    that treated end-of-body as end-of-list would silently drop the last one."""
    body = (
        pkt_line(f"{ZERO} {SHA} refs/mship/run/t1/api\x00report-status".encode())
        + pkt_line(f"{ZERO} {SHA} refs/heads/sneaky".encode())
    )
    assert ref_commands(body) == ["refs/mship/run/t1/api", "refs/heads/sneaky"]
    with pytest.raises(RefScopeError):
        check_ref_scope(body)


def test_check_ref_scope_accepts_the_run_namespace():
    check_ref_scope(_body(f"{ZERO} {SHA} refs/mship/run/t1/api".encode()))


def test_check_ref_scope_refuses_a_branch_write():
    """ac5. An unscoped receive path really does create refs/heads/<anything> —
    verified against a live prototype. This is the control that stops it."""
    with pytest.raises(RefScopeError) as exc:
        check_ref_scope(_body(f"{ZERO} {SHA} refs/heads/attacker".encode()))
    assert "refs/heads/attacker" in str(exc.value)


def test_one_out_of_scope_ref_refuses_the_whole_push():
    with pytest.raises(RefScopeError):
        check_ref_scope(_body(
            f"{ZERO} {SHA} refs/mship/run/t1/api".encode(),
            f"{ZERO} {SHA} refs/tags/v1".encode(),
        ))


def test_a_body_with_no_commands_is_refused():
    with pytest.raises(PktLineError):
        check_ref_scope(b"0000")
