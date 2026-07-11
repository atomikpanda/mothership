"""Client-side of `mship run/capture/build --remote[=role]` (Task 5,
specs/2026-07-11-remote-run-machine.md, MOS-191/MOS-203) — the counterpart to
`core/remote_exec.py`'s serve-side `run_verb_stream`.

`exec_remote` POSTs `{task, repos, platform?, kind}` to a run-host's
`POST /exec/{verb}` (bearer-auth'd, see `mship.core.run_host.RunHostConnection`)
and drives the streamed `application/octet-stream` response: it prints
stdout/stderr lines live as they arrive and, for `verb == "capture"` when a
local destination is given, extracts the artifact tar (if any) there. See
`mship.core.remote_exec`'s module docstring for the exact wire framing this
parses (line-per-chunk task output, an optional `__MSHIP_ARTIFACTS__ <n>` +
`n` raw tar bytes, and a trailing `__MSHIP_EXIT__ <code>` sentinel).

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


class RemoteExecError(Exception):
    """A connection-level failure talking to a run-host: unreachable/timed
    out, a non-2xx HTTP response, or a stream that ended without ever
    producing the trailing `__MSHIP_EXIT__` sentinel (a truncated/dropped
    connection). Distinct from a non-zero REMOTE TASK exit — that's conveyed
    as data (the wire contract's `__MSHIP_EXIT__ <code>` line) and returned as
    a plain int from `exec_remote`, never raised."""


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


def _drive(
    reader: _ChunkReader,
    *,
    captures_dir_for: Optional[Path],
    print_fn: Callable[[str], None],
) -> int:
    while True:
        line = reader.readline()
        if line is None:
            raise RemoteExecError(
                "remote stream ended without a __MSHIP_EXIT__ sentinel"
            )
        text = line.decode("utf-8", errors="replace").rstrip("\n")

        if text.startswith(ARTIFACT_MARKER + " "):
            n = int(text.split(" ", 1)[1])
            tar_bytes = reader.read_exact(n)
            if captures_dir_for is not None:
                captures_dir_for.mkdir(parents=True, exist_ok=True)
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
                    tar.extractall(captures_dir_for, filter="data")
            continue

        if text.startswith(EXIT_MARKER + " "):
            return int(text.split(" ", 1)[1])

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

    Raises `RemoteExecError` for a genuine connection failure (unreachable
    host, non-2xx response, a stream that never produced the exit sentinel).
    `transport` is an injection seam for tests (e.g. `httpx.MockTransport`);
    production callers omit it and get a real network connection.
    """
    body: dict = {"task": task, "repos": repos, "kind": kind}
    if platform is not None:
        body["platform"] = platform

    headers = {"Authorization": f"Bearer {conn.token}"}
    url = f"{conn.url}/exec/{verb}"

    try:
        with httpx.Client(transport=transport) as client:
            with client.stream("POST", url, headers=headers, json=body) as resp:
                resp.raise_for_status()
                reader = _ChunkReader(resp.iter_raw())
                return _drive(
                    reader, captures_dir_for=captures_dir_for, print_fn=print_fn
                )
    except httpx.HTTPError as exc:
        raise RemoteExecError(f"remote exec failed ({url}): {exc}") from exc
