"""A deliberately narrow git smart-HTTP RECEIVE path for `mship serve`.

`mship run --remote` hands the operator's working tree to the run host by
pushing a synthesized commit straight to it (see `core/run_transfer.py`), so
serve needs somewhere for that push to land. This module is that somewhere, and
nothing more: it is not a mirror, not a remote an operator adds by hand, and not
a path for real history.

Two controls, both security controls rather than tidiness:

  - the REPO ALLOWLIST (`receive_repo_path`) — only repos this workspace's
    config declares, and only top-level ones;
  - the REF-NAME constraint (`check_ref_scope`) — only the run scratch
    namespace owned by `core/run_ref.py`.

Without the second this is an arbitrary-ref-write primitive against the run
host: a plain receive-pack pass-through was verified to accept
`<sha>:refs/heads/attacker` and create that branch. The body is therefore parsed
for its ref commands BEFORE any git process sees it.

Wire shape (verified end to end against real `git push`, git 2.43):

    GET  <base>/info/refs?service=git-receive-pack
      -> pkt_line(b"# service=git-receive-pack\\n") + b"0000"
         + `git receive-pack --http-backend-info-refs <repo>` stdout
    POST <base>/git-receive-pack
      -> raw body piped to `git receive-pack --stateless-rpc <repo>` stdin,
         its stdout returned verbatim

The HTTP layer (auth, status codes, threadpool) lives in `core/serve.py`; this
module is pure logic plus two subprocess calls so it can be tested without a
server.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from mship.core.run_ref import RUN_REF_PREFIX, is_run_ref

RECEIVE_SERVICE = "git-receive-pack"
ADVERTISEMENT_CONTENT_TYPE = "application/x-git-receive-pack-advertisement"
RESULT_CONTENT_TYPE = "application/x-git-receive-pack-result"

# An object id as receive-pack's own `parse_oid_hex` accepts it: hex, either
# hash algorithm's width, case-insensitive. Anything else is not a ref command
# and is refused rather than read past.
_OID_RE = re.compile(rb"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


class PktLineError(ValueError):
    """The request body is not parseable pkt-line framing. Refused rather than
    guessed at: a body we cannot read is a body whose refs we cannot check."""


class RefScopeError(ValueError):
    """The push asks to write a ref outside the run scratch namespace."""


class UnknownReceiveRepoError(ValueError):
    """No git repository this endpoint will accept a push for."""


class ReceivePackError(RuntimeError):
    """`git receive-pack` itself failed on this host."""


def pkt_line(payload: bytes) -> bytes:
    """`<4-hex-length><payload>`, where the length counts its own 4 bytes."""
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


def service_advertisement(refs_output: bytes) -> bytes:
    """The `info/refs` body: the service pkt-line, a flush, then receive-pack's
    own advertisement."""
    return pkt_line(f"# service={RECEIVE_SERVICE}\n".encode("utf-8")) + b"0000" + refs_output


def _command_ref(payload: bytes) -> str:
    """The ref name in one receive-pack command packet, read exactly as git
    reads it (`read_head_info` in receive-pack.c):

      - one trailing newline is chomped from the packet;
      - the payload is a C string, so it ends at the first NUL — which is why
        capabilities are stripped on EVERY line, not only the first one that
        carries them;
      - two object ids and a space each must lead, and the ref name is then the
        WHOLE remainder, spaces included. Stopping at the next space would
        report a shorter, in-scope name for a command git reads as a different
        ref.
    """
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    line = payload.split(b"\x00", 1)[0]
    fields = line.split(b" ", 2)
    if len(fields) < 3 or not fields[2]:
        raise PktLineError(f"malformed ref command: {payload!r}")
    if not (_OID_RE.match(fields[0]) and _OID_RE.match(fields[1])):
        raise PktLineError(f"ref command does not begin with two object ids: {payload!r}")
    return fields[2].decode("utf-8", errors="replace")


def ref_commands(body: bytes) -> list[str]:
    """The ref names a receive-pack request asks to update, in order.

    The request is `pkt_line("<old-oid> <new-oid> <ref>[\\0<capabilities>]")`
    repeated, then a `0000` flush, then push options and (for anything but a
    delete) the packfile. Parsing stops at the flush, so neither is touched.

    Only the flush ends the list. A body truncated instead is parsed to its end
    anyway (real receive-pack dies on one, but the check must never see fewer
    refs than git might act on).
    """
    names: list[str] = []
    i = 0
    while i + 4 <= len(body):
        header = body[i:i + 4]
        try:
            length = int(header, 16)
        except ValueError:
            raise PktLineError(f"not a pkt-line length: {header!r}")
        if length == 0:
            break
        if length < 4 or i + length > len(body):
            raise PktLineError(
                f"pkt-line length {length} runs past the end of the request body"
            )
        payload = body[i + 4:i + length]
        i += length
        names.append(_command_ref(payload))
    return names


def check_ref_scope(body: bytes) -> None:
    """Raise unless every ref the push writes is in the run scratch namespace."""
    names = ref_commands(body)
    if not names:
        raise PktLineError("push request contains no ref command")
    outside = [n for n in names if not is_run_ref(n)]
    if outside:
        raise RefScopeError(
            f"refusing to write {', '.join(outside)}: this endpoint accepts "
            f"pushes only onto {RUN_REF_PREFIX}<task>/<repo>"
        )


def receive_repo_path(config, repo: str) -> Path:
    """The git directory a push for `repo` may land in — the ALLOWLIST.

    Only repos declared in this workspace's config are accepted (spec ac5). A
    `git_root` child has no git directory of its own (its tree IS its parent's),
    so a push named for a child is refused and names the parent instead (ac7).

    `ConfigLoader.load` resolves top-level repo paths to absolute, so the value
    returned here is directly usable as a git argument.
    """
    repo_config = getattr(config, "repos", {}).get(repo)
    if repo_config is None:
        known = ", ".join(sorted(getattr(config, "repos", {}))) or "(none)"
        raise UnknownReceiveRepoError(
            f"unknown repo {repo!r}; this workspace knows: {known}"
        )
    if repo_config.git_root is not None:
        raise UnknownReceiveRepoError(
            f"{repo!r} is a git_root child of {repo_config.git_root!r} and has "
            f"no git directory of its own; push to {repo_config.git_root!r}"
        )
    return Path(repo_config.path)


def advertise_refs(repo_path: Path) -> bytes:
    """The `info/refs?service=git-receive-pack` body for `repo_path`."""
    result = subprocess.run(
        ["git", "receive-pack", "--http-backend-info-refs", str(repo_path)],
        capture_output=True,
    )
    if result.returncode != 0:
        raise ReceivePackError(
            f"git receive-pack advertisement failed for {repo_path}: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return service_advertisement(result.stdout)


def receive_pack(repo_path: Path, body: bytes) -> bytes:
    """Run the push. Ref scope is checked BEFORE git is invoked, so an
    out-of-scope push never reaches the repository at all."""
    check_ref_scope(body)
    result = subprocess.run(
        ["git", "receive-pack", "--stateless-rpc", str(repo_path)],
        input=body, capture_output=True,
    )
    if result.returncode != 0:
        raise ReceivePackError(
            f"git receive-pack failed for {repo_path}: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return result.stdout
