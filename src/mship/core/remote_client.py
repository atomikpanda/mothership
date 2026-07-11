"""Client-side of `mship run/capture/build --remote[=role]` (Task 5,
specs/2026-07-11-remote-run-machine.md, MOS-191/MOS-203) — the counterpart to
`core/remote_exec.py`'s serve-side `run_verb_stream`.

`exec_remote` POSTs `{task, repos, platform?, kind}` to a run-host's
`POST /exec/{verb}` (bearer-auth'd, see `mship.core.run_host.RunHostConnection`)
and drives the streamed `application/octet-stream` response: it prints
stdout/stderr lines live as they arrive and, for `verb == "capture"` when a
local destination is given, extracts the artifact tar (if any) there. See
`mship.core.remote_exec`'s module docstring for the exact wire framing this
parses (line-per-chunk task output, an optional `__MSHIP_ARTIFACTS__:<nonce>
<n>` + `n` raw tar bytes, and a trailing `__MSHIP_EXIT__:<nonce> <code>`
sentinel — where `<nonce>` is the per-request secret from the
`X-Mship-Exec-Nonce` response header that stops task stdout from spoofing a
control record).

The CLI (`cli/exec.py`'s `run`/`build`, `cli/capture.py`'s `capture`) is the
only caller: it resolves `--remote[=role]` to a `RunHostConnection` via
`mship.core.run_host.resolve_run_host`, calls `exec_remote`, and mirrors the
returned int as its own process exit code (`raise typer.Exit(code)`).
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Callable, Iterator, Optional

import httpx

from mship.core.remote_exec import ARTIFACT_MARKER, EXIT_MARKER
from mship.core.run_host import RunHostConnection

# The response header carrying the per-request anti-spoof nonce (see
# `core/serve.py post_exec` / `core/remote_exec.py`). Read BEFORE draining the
# body; a control line counts only if it carries this exact nonce.
NONCE_HEADER = "X-Mship-Exec-Nonce"

# Hard cap on the advertised artifact-tar size. The server only ever writes a
# handful of small capture files (screen.png, layout.*), so a wildly larger
# advertised count is a bug or a hostile/compromised remote — reject it BEFORE
# reading (no unbounded allocation / tar-bomb landing on disk). 256 MiB.
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


class RemoteExecError(Exception):
    """A connection-level failure talking to a run-host: unreachable/timed
    out, a non-2xx HTTP response, or a stream that ended without ever
    producing the trailing `__MSHIP_EXIT__` sentinel (a truncated/dropped
    connection). Distinct from a non-zero REMOTE TASK exit — that's conveyed
    as data (the wire contract's `__MSHIP_EXIT__ <code>` line) and returned as
    a plain int from `exec_remote`, never raised.

    Task 6: the message is chosen per failure so the CLI error names the
    actual fix — see `_http_status_message` for the non-2xx cases (a 503
    "not bootstrapped" remote vs. any other status) and `exec_remote`'s
    `except httpx.HTTPError` for the connection-level ("unreachable via
    relay") case."""


def _http_status_message(url: str, resp: httpx.Response) -> str:
    """Build an actionable message for a non-2xx `POST /exec/{verb}`
    response. `resp` has already been `.read()` so `.json()`/`.text` are
    safe to inspect (the streaming context manager doesn't buffer the body
    otherwise)."""
    detail = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = body.get("detail")
    except Exception:
        pass

    if resp.status_code == 503:
        # Task 3's `POST /exec/{verb}` 503s when the remote serve has no
        # workspace config wired in — i.e. that machine was never bootstrapped
        # as an mship workspace (or serve started without one).
        base = f"remote workspace not bootstrapped at {url} (503)"
        return f"{base}: {detail}" if detail else (
            f"{base}; bootstrap that machine as an mship workspace and "
            f"restart `mship serve --relay` there"
        )
    if resp.status_code == 401:
        return (
            f"remote host at {url} rejected the bearer token (401); the "
            f"run-host mapping may be stale — re-run `mship run-host add "
            f"<role>` with a fresh pair link/token"
        )
    if detail:
        return f"remote exec failed ({url}): HTTP {resp.status_code}: {detail}"
    return f"remote exec failed ({url}): HTTP {resp.status_code}"


class _ChunkReader:
    """Buffered `readline()` / `read_exact()` over an iterator of raw byte
    chunks (`httpx.Response.iter_raw()`).

    The wire framing in `core/remote_exec.py` interleaves newline-terminated
    text lines with one raw (NOT line-safe — may contain arbitrary bytes
    including `\\n`) tar block, so a plain line-iterator isn't enough:
    `read_exact()` must consume exactly N raw bytes without splitting on
    newlines, while `readline()` still works line-by-line the rest of the
    time. Both methods buffer across chunk boundaries — a single line, or the
    artifact block, need not fall inside one network chunk.
    """

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buf = bytearray()
        self._eof = False

    def _fill(self) -> bool:
        """Pull one more chunk into the buffer. Returns False at EOF."""
        if self._eof:
            return False
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._eof = True
            return False
        self._buf.extend(chunk)
        return True

    def readline(self) -> Optional[bytes]:
        """The next newline-terminated line (newline included), or `None`
        once the stream is exhausted with nothing left buffered. A final
        line missing its trailing newline (a truncated stream) is still
        returned once, unterminated — the caller decides whether that's
        acceptable (here, it never matches a marker and so surfaces as
        "missing __MSHIP_EXIT__ sentinel")."""
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                line = bytes(self._buf[: nl + 1])
                del self._buf[: nl + 1]
                return line
            if not self._fill():
                if self._buf:
                    line = bytes(self._buf)
                    self._buf.clear()
                    return line
                return None

    def read_exact(self, n: int) -> bytes:
        """Exactly `n` raw bytes (binary-safe — never line-split)."""
        while len(self._buf) < n:
            if not self._fill():
                raise RemoteExecError(
                    f"remote stream ended while reading {n} artifact bytes "
                    f"(only {len(self._buf)} available)"
                )
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data


def _control_count(text: str) -> int:
    """Parse the base-10 count off a nonce-tagged control line
    (`__MSHIP_EXIT__:<nonce> <count>` / `__MSHIP_ARTIFACTS__:<nonce> <count>`).
    A non-numeric/empty count is a malformed record from the remote — surface
    it as a clean `RemoteExecError`, not an uncaught `ValueError` traceback."""
    parts = text.split(" ", 1)
    try:
        return int(parts[1])
    except (IndexError, ValueError):
        raise RemoteExecError(
            f"malformed control record from remote: {text!r}"
        )


def _drive(
    reader: _ChunkReader,
    *,
    nonce: str,
    captures_dir_for: Optional[Path],
    print_fn: Callable[[str], None],
) -> int:
    # A line is a CONTROL record only if it carries this request's nonce (see
    # NONCE_HEADER). Everything else — including task stdout that literally
    # prints `__MSHIP_EXIT__ 0` without the nonce — is passthrough output.
    artifact_prefix = f"{ARTIFACT_MARKER}:{nonce} "
    exit_prefix = f"{EXIT_MARKER}:{nonce} "
    while True:
        line = reader.readline()
        if line is None:
            raise RemoteExecError(
                "remote stream ended without a __MSHIP_EXIT__ sentinel"
            )
        text = line.decode("utf-8", errors="replace").rstrip("\n")

        if text.startswith(artifact_prefix):
            n = _control_count(text)
            if n < 0:
                # A negative count would slip past the cap check below and reach
                # read_exact(-1), which reads nothing yet leaves the buffer /
                # stream desynced (the exit sentinel never gets parsed). Refuse.
                raise RemoteExecError(
                    f"remote advertised negative artifact byte count {n}; "
                    f"refusing to read"
                )
            if n > MAX_ARTIFACT_BYTES:
                # Refuse BEFORE reading — no unbounded allocation / tar-bomb.
                raise RemoteExecError(
                    f"remote advertised {n} artifact bytes, exceeding the "
                    f"{MAX_ARTIFACT_BYTES}-byte cap; refusing to read"
                )
            tar_bytes = reader.read_exact(n)
            if captures_dir_for is not None:
                captures_dir_for.mkdir(parents=True, exist_ok=True)
                # mode="r:" = UNCOMPRESSED only (the server writes mode="w").
                # A gzip/xz "tar bomb" then raises tarfile.ReadError instead of
                # being transparently decompressed. `filter="data"` still guards
                # path traversal / unsafe members on extract.
                try:
                    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
                        tar.extractall(captures_dir_for, filter="data")
                except tarfile.TarError as exc:
                    raise RemoteExecError(
                        f"remote artifact block is not a valid uncompressed "
                        f"tar: {exc}"
                    ) from exc
            continue

        if text.startswith(exit_prefix):
            return _control_count(text)

        print_fn(text)


def exec_remote(
    *,
    verb: str,
    conn: RunHostConnection,
    task: str,
    repos: list[str],
    platform: str | None = None,
    kind: str = "all",
    captures_dir_for: Path | None = None,
    print_fn: Callable[[str], None] = print,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """POST `{task, repos, platform?, kind}` to `{conn.url}/exec/{verb}`
    (bearer-auth'd with `conn.token`) and drive the streamed response.

    Prints each stdout/stderr line as it arrives (via `print_fn`, default the
    `print` builtin). When `verb == "capture"` and `captures_dir_for` is
    given, extracts the artifact tar (if the remote produced one) into that
    directory — callers pass the SAME local path a local capture would use
    (see `cli/capture.py`'s out_dir computation) so a remote capture is
    indistinguishable on disk from a local one. `captures_dir_for` is ignored
    (nothing to extract) for `run`/`build`, which never emit an artifact
    block.

    Returns the remote task's exit code, parsed from the trailing
    `__MSHIP_EXIT__ <code>` line — conveyed as DATA, not an HTTP error, so a
    non-zero remote task exit is a normal return here, not a raise.

    Raises `RemoteExecError` for a genuine connection failure. Task 6 gives
    each case a specific, actionable message: a non-2xx HTTP response (a 503
    "remote workspace not bootstrapped", a 401 stale-token, or a generic
    status — see `_http_status_message`), a connection-level failure
    (unreachable host/timeout — "unreachable via relay"), or a stream that
    never produced the exit sentinel (unchanged from Task 5). `transport` is
    an injection seam for tests (e.g. `httpx.MockTransport`); production
    callers omit it and get a real network connection.
    """
    body: dict = {"task": task, "repos": repos, "kind": kind}
    if platform is not None:
        body["platform"] = platform

    headers = {"Authorization": f"Bearer {conn.token}"}
    url = f"{conn.url}/exec/{verb}"

    try:
        with httpx.Client(transport=transport) as client:
            with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise RemoteExecError(_http_status_message(url, resp))
                # Read the anti-spoof nonce from the response HEADERS before
                # draining the body — headers arrive first, and the task can't
                # inject into them (httpx headers are case-insensitive). Without
                # it we can't tell a real control record from spoofed output.
                nonce = resp.headers.get(NONCE_HEADER)
                if not nonce:
                    raise RemoteExecError(
                        f"remote response missing the {NONCE_HEADER} header; "
                        f"cannot authenticate the exit-code/artifact framing "
                        f"(is the remote running a current mship serve?)"
                    )
                reader = _ChunkReader(resp.iter_raw())
                return _drive(
                    reader, nonce=nonce,
                    captures_dir_for=captures_dir_for, print_fn=print_fn,
                )
    except httpx.HTTPError as exc:
        # Connection-level failure (unreachable host, DNS, timeout, dropped
        # tunnel before a response was ever received) — distinct from the
        # non-2xx case above, which raises RemoteExecError directly and so
        # never reaches this handler.
        raise RemoteExecError(
            f"remote host at {url} is unreachable via relay ({exc}); check "
            f"that machine is running `mship serve --relay` and its pairing "
            f"is still valid"
        ) from exc
