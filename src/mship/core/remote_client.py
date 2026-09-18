"""Client-side of `mship run/capture/build --remote[=role]` (Task 5,
specs/2026-07-11-remote-run-machine.md, MOS-191/MOS-203) — the counterpart to
`core/remote_exec.py`'s serve-side `run_verb_stream`.

`exec_remote` resolves a named run-host registration at the operation boundary,
then POSTs `{task, repos, platform?, kind}` to its authenticated
`POST /exec/{verb}` route and drives the streamed `application/octet-stream`
response: it prints stdout/stderr lines live as they arrive and, for
`verb == "capture"` when a local destination is given, extracts the artifact tar.
`mship.core.remote_exec`'s module docstring for the exact wire framing this
parses (line-per-chunk task output, an optional `__MSHIP_ARTIFACTS__:<nonce>
<n>` + `n` raw tar bytes, and a trailing `__MSHIP_EXIT__:<nonce> <code>`
sentinel — where `<nonce>` is the per-request secret from the
`X-Mship-Exec-Nonce` response header that stops task stdout from spoofing a
control record).

The CLI (`cli/exec.py`'s `run`/`build`, `cli/capture.py`'s `capture`) resolves
`--remote[=role]` to a registration, supplies a `RunHostResolver` to
`exec_remote`, and mirrors the returned int as its own process exit code
(`raise typer.Exit(code)`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from mship.core.session_source import SourceUpdateReply, SourceUpdateRequest

import httpx

from mship.core.remote_exec import ARTIFACT_MARKER, EXIT_MARKER, KEEPALIVE_MARKER
from mship.core.run_host import (
    HostRegistration,
    ResolvedRunHostConnection,
    RunHostResolver,
)
from mship.core.remote_tool import (
    ToolEvent,
    ToolProtocolError,
    ToolRequest,
    ToolResult,
    iter_tool_events,
)

# The response header carrying the per-request anti-spoof nonce (see
# `core/serve.py post_exec` / `core/remote_exec.py`). Read BEFORE draining the
# body; a control line counts only if it carries this exact nonce.

# Legacy verb streams are line-oriented, but an ordinary tool is allowed to
# produce a long newline-free line.  Deliver bounded text fragments instead of
# retaining it all; `_drive` tracks logical line starts so a later fragment
# cannot be mistaken for a nonce-authenticated control record.
MAX_LEGACY_LINE_BYTES = 64 * 1024
MAX_HTTP_RAW_CHUNK_BYTES = 64 * 1024
NONCE_HEADER = "X-Mship-Exec-Nonce"

# Hard cap on the advertised artifact-tar size. The server only ever writes a
# handful of small capture files (screen.png, layout.*), so a wildly larger
# advertised count is a bug or a hostile/compromised remote — reject it BEFORE
MAX_SOURCE_UPDATE_BYTES = 64 * 1024
MAX_RESULT_METADATA_BYTES = 512 * 1024
# reading (no unbounded allocation / tar-bomb landing on disk). 256 MiB.
_SAFE_PLATFORM = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


def _raw_chunks(response: httpx.Response) -> Iterator[bytes]:
    # HTTPX's chunk_size aggregates small reads, delaying live output until the
    # buffer fills. Split only oversized chunks; never wait to fill a chunk.
    for chunk in response.iter_raw():
        if len(chunk) <= MAX_HTTP_RAW_CHUNK_BYTES:
            yield chunk
        else:
            for offset in range(0, len(chunk), MAX_HTTP_RAW_CHUNK_BYTES):
                yield chunk[offset : offset + MAX_HTTP_RAW_CHUNK_BYTES]


class RemoteExecError(Exception):
    """A connection-level failure talking to a run-host.

    A non-zero task exit is data returned from ``exec_remote``, never raised.
    """


class TaskResultRetrievalError(RuntimeError):
    """An authenticated immutable-result response was unavailable or invalid."""


_RESULT_ID = re.compile(r"^[A-Za-z0-9_-]{24,128}$")


def list_task_results(
    *,
    host: HostRegistration,
    resolver: RunHostResolver,
    task_slug: str | None = None,
    work_item_id: str | None = None,
    repo: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, object]]:
    """List safe immutable result summaries over the existing bearer boundary."""
    selectors = {
        key: value
        for key, value in {
            "task_slug": task_slug,
            "work_item_id": work_item_id,
            "repo": repo,
        }.items()
        if value is not None
    }
    if not selectors or any(
        not isinstance(value, str) or not value or len(value) > 128
        for value in selectors.values()
    ):
        raise ValueError("invalid task result selector")
    value = _result_json(
        host, resolver, "/task-results", params=selectors, transport=transport
    )
    if not isinstance(value, list):
        raise TaskResultRetrievalError("task result list is invalid")
    return value


def get_task_result(
    *,
    host: HostRegistration,
    resolver: RunHostResolver,
    result_id: str,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    _validate_result_id(result_id)
    value = _result_json(
        host, resolver, f"/task-results/{result_id}", transport=transport
    )
    if not isinstance(value, dict):
        raise TaskResultRetrievalError("task result metadata is invalid")
    return value


def download_task_artifact(
    *,
    host: HostRegistration,
    resolver: RunHostResolver,
    result_id: str,
    artifact_id: str,
    expected_sha256: str,
    destination: Path,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Stream one selected artifact to a private temporary file and verify it."""
    _validate_result_id(result_id)
    _validate_result_id(artifact_id)
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("invalid selected artifact digest")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    received = 0
    digest = hashlib.sha256()
    try:
        with _result_response(
            host,
            resolver,
            f"/task-results/{result_id}/artifacts/{artifact_id}",
            transport=transport,
        ) as response:
            header_digest = response.headers.get("X-Mship-SHA256")
            length = response.headers.get("Content-Length")
            try:
                expected_length = int(length) if length is not None else -1
            except ValueError:
                raise TaskResultRetrievalError(
                    "task result artifact is invalid"
                ) from None
            if (
                header_digest != expected_sha256
                or not 0 <= expected_length <= 512 * 1024 * 1024
            ):
                raise TaskResultRetrievalError("task result artifact is invalid")
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    received += len(chunk)
                    if received > expected_length:
                        raise TaskResultRetrievalError(
                            "task result artifact is invalid"
                        )
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        if received != expected_length or digest.hexdigest() != expected_sha256:
            raise TaskResultRetrievalError("task result artifact failed verification")
        os.replace(temporary, destination)
        return destination
    except httpx.HTTPError:
        raise TaskResultRetrievalError("task result retrieval failed") from None
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _result_response(
    host: HostRegistration,
    resolver: RunHostResolver,
    path: str,
    *,
    params: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[httpx.Response]:
    """Resolve the owning workspace and retry only before accepting response bytes."""
    with httpx.Client(transport=transport, follow_redirects=False) as client:
        for attempt in range(2):
            active = resolver.resolve(host, force_refresh=attempt == 1)
            with client.stream(
                "GET",
                _operation_url(host, active, path),
                params=params,
                headers={"Authorization": f"Bearer {active.token}"},
            ) as response:
                if (
                    response.status_code == 401
                    and attempt == 0
                    and hasattr(host.connection, "workspace_id")
                ):
                    continue
                if response.status_code != 200:
                    raise TaskResultRetrievalError("task result is unavailable")
                yield response
                return


def _result_json(
    host: HostRegistration,
    resolver: RunHostResolver,
    path: str,
    *,
    params: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> object:
    try:
        with _result_response(
            host, resolver, path, params=params, transport=transport
        ) as response:
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if len(raw) + len(chunk) > MAX_RESULT_METADATA_BYTES:
                    raise TaskResultRetrievalError("task result metadata is too large")
                raw.extend(chunk)
    except httpx.HTTPError:
        raise TaskResultRetrievalError("task result retrieval failed") from None
    try:
        return json.loads(raw)
    except ValueError, UnicodeDecodeError, RecursionError:
        raise TaskResultRetrievalError("task result metadata is invalid") from None


def _validate_result_id(value: str) -> None:
    if not isinstance(value, str) or _RESULT_ID.fullmatch(value) is None:
        raise ValueError("invalid task result identifier")


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
        """Pull one more bounded raw chunk into the buffer."""
        if self._eof:
            return False
        try:
            chunk = next(self._chunks)
        except StopIteration:
            self._eof = True
            return False
        if not isinstance(chunk, bytes) or len(chunk) > MAX_HTTP_RAW_CHUNK_BYTES:
            raise RemoteExecError("remote stream emitted an oversized chunk")
        self._buf.extend(chunk)
        return True

    def readline(self) -> Optional[bytes]:
        """Return one bounded logical-line fragment, or ``None`` at EOF."""
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                take = min(nl + 1, MAX_LEGACY_LINE_BYTES)
                line = bytes(self._buf[:take])
                del self._buf[:take]
                return line
            if len(self._buf) >= MAX_LEGACY_LINE_BYTES:
                line = bytes(self._buf[:MAX_LEGACY_LINE_BYTES])
                del self._buf[:MAX_LEGACY_LINE_BYTES]
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
    except IndexError, ValueError:
        raise RemoteExecError(f"malformed control record from remote: {text!r}")


def _drive(
    reader: _ChunkReader,
    *,
    nonce: str,
    captures_dir_for: Optional[Path],
    print_fn: Callable[[str], None],
) -> int:
    # A control record normally begins a logical line. A keepalive, artifact, or
    # terminal record may directly follow a child's unterminated final fragment.
    # Retain that fragment across control records so line-rendering callbacks
    # never turn each keepalive into a visible synthetic newline.
    artifact_prefix = f"{ARTIFACT_MARKER}:{nonce} ".encode("utf-8")
    keepalive_line = f"{KEEPALIVE_MARKER}:{nonce}\n".encode("utf-8")
    exit_prefix = f"{EXIT_MARKER}:{nonce} ".encode("utf-8")

    def render(data: bytes) -> None:
        if data:
            print_fn(data.decode("utf-8", errors="replace").rstrip("\n"))

    def append_output(data: bytes) -> None:
        pending.extend(data)
        # Match `_ChunkReader`'s existing bounded long-line behavior: a child
        # cannot turn keepalive retention into unbounded client memory.
        while len(pending) >= MAX_LEGACY_LINE_BYTES:
            render(bytes(pending[:MAX_LEGACY_LINE_BYTES]))
            del pending[:MAX_LEGACY_LINE_BYTES]

    def extract_artifact(header: bytes) -> None:
        text = header.decode("utf-8", errors="replace").rstrip("\n")
        n = _control_count(text)
        if n < 0:
            raise RemoteExecError(
                f"remote advertised negative artifact byte count {n}; "
                f"refusing to read"
            )
        if n > MAX_ARTIFACT_BYTES:
            raise RemoteExecError(
                f"remote advertised {n} artifact bytes, exceeding the "
                f"{MAX_ARTIFACT_BYTES}-byte cap; refusing to read"
            )
        tar_bytes = reader.read_exact(n)
        if captures_dir_for is not None:
            captures_dir_for.mkdir(parents=True, exist_ok=True)
            try:
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
                    tar.extractall(captures_dir_for, filter="data")
            except tarfile.TarError as exc:
                raise RemoteExecError(
                    f"remote artifact block is not a valid uncompressed tar: {exc}"
                ) from exc

    pending = bytearray()

    while True:
        line = reader.readline()
        if line is None:
            render(bytes(pending))
            raise RemoteExecError(
                "remote stream ended without a __MSHIP_EXIT__ sentinel"
            )

        # A control frame either starts this logical line or is an authenticated
        # suffix after an unterminated child fragment. `find` covers only the
        # latter; the first branch prevents arbitrary first-line output from
        # being treated as control.
        artifact_at = (
            0 if line.startswith(artifact_prefix) else line.find(artifact_prefix)
        )
        if artifact_at >= 0:
            append_output(line[:artifact_at])
            extract_artifact(line[artifact_at:])
            continue

        if line == keepalive_line or line.endswith(keepalive_line):
            append_output(line[: -len(keepalive_line)])
            continue

        exit_at = 0 if line.startswith(exit_prefix) else line.find(exit_prefix)
        if exit_at >= 0:
            text = line[exit_at:].decode("utf-8", errors="replace").rstrip("\n")
            append_output(line[:exit_at])
            render(bytes(pending))
            return _control_count(text)

        append_output(line)
        if line.endswith(b"\n"):
            render(bytes(pending))
            pending.clear()


def _operation_url(
    host: HostRegistration, conn: ResolvedRunHostConnection, suffix: str
) -> str:
    if hasattr(host.connection, "workspace_id"):
        return f"{conn.url.rstrip('/')}/workspaces/{quote(host.connection.workspace_id, safe='')}{suffix}"
    return f"{conn.url.rstrip('/')}{suffix}"


def exec_remote(
    *,
    verb: str,
    task: str,
    repos: list[str],
    host: HostRegistration,
    resolver: RunHostResolver,
    platform: str | None = None,
    kind: str = "all",
    captures_dir_for: Path | None = None,
    run_ref_repos: list[str] | None = None,
    print_fn: Callable[[str], None] = print,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Execute once, with one refresh/retry only for a pre-stream 401/403."""
    body: dict = {"task": task, "repos": repos, "kind": kind}
    if platform is not None:
        body["platform"] = platform
    if run_ref_repos:
        body["run_ref_repos"] = list(run_ref_repos)
    for attempt in range(2):
        active = resolver.resolve(host, force_refresh=attempt == 1)
        url = _operation_url(host, active, f"/exec/{verb}")
        try:
            with httpx.Client(transport=transport, follow_redirects=False) as client:
                with client.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {active.token}"},
                    json=body,
                ) as response:
                    if (
                        response.status_code in {401, 403}
                        and attempt == 0
                        and hasattr(host.connection, "workspace_id")
                    ):
                        continue
                    if response.status_code >= 400:
                        raise RemoteExecError(
                            f"remote execution was refused (HTTP {response.status_code})"
                        )
                    nonce = response.headers.get(NONCE_HEADER)
                    if not nonce:
                        raise RemoteExecError(
                            f"remote response missing the {NONCE_HEADER} header"
                        )
                    timeout = response.request.extensions.get("timeout")
                    if isinstance(timeout, dict):
                        timeout["read"] = None
                    return _drive(
                        _ChunkReader(_raw_chunks(response)),
                        nonce=nonce,
                        captures_dir_for=captures_dir_for,
                        print_fn=print_fn,
                    )
        except httpx.HTTPError:
            raise RemoteExecError(
                "remote host is unreachable; check relay pairing and host availability"
            ) from None


def exec_session_capture(
    *,
    operation: ToolRequest,
    kinds: tuple[str, ...],
    platform: str,
    host: HostRegistration,
    resolver: RunHostResolver,
    captures_dir_for: Path,
    print_fn: Callable[[str], None] = print,
    transport: httpx.BaseTransport | None = None,
) -> int:
    if (
        operation.preparation != "observe"
        or operation.argv
        or operation.task_key is None
        or set(operation.input_files) != {"MSHIP_TARGET_REQUEST_FILE"}
        or operation.env
        or operation.cwd != "."
        or operation.run_ref_repos
        or operation.owner_ref is None
        or operation.generation is None
        or operation.source_revision is None
        or not kinds
        or any(kind not in {"image", "layout"} for kind in kinds)
        or not isinstance(platform, str)
        or not _SAFE_PLATFORM.fullmatch(platform)
    ):
        raise ValueError("invalid session capture request")
    body = {
        "operation": operation.to_dict(),
        "kinds": list(kinds),
        "platform": platform,
    }
    for attempt in range(2):
        active = resolver.resolve(host, force_refresh=attempt == 1)
        url = _operation_url(host, active, "/exec/session-capture")
        try:
            with httpx.Client(transport=transport, follow_redirects=False) as client:
                with client.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {active.token}"},
                    json=body,
                ) as response:
                    if response.status_code in {401, 403} and attempt == 0:
                        continue
                    if response.status_code >= 400:
                        raise RemoteExecError(
                            f"remote session capture was refused (HTTP {response.status_code})"
                        )
                    nonce = response.headers.get(NONCE_HEADER)
                    if not nonce:
                        raise RemoteExecError(
                            f"remote response missing the {NONCE_HEADER} header"
                        )
                    timeout = response.request.extensions.get("timeout")
                    if isinstance(timeout, dict):
                        timeout["read"] = None
                    return _drive(
                        _ChunkReader(_raw_chunks(response)),
                        nonce=nonce,
                        captures_dir_for=captures_dir_for,
                        print_fn=print_fn,
                    )
        except httpx.HTTPError:
            raise RemoteExecError(
                "remote host is unreachable; check relay pairing and host availability"
            ) from None
    raise RemoteExecError(
        "remote host rejected refreshed credentials; re-enrol the host and re-pair if needed"
    )


def stop_session(
    *,
    task: str,
    repo: str,
    owner_ref: str,
    generation: str,
    source_revision: str,
    host: HostRegistration,
    resolver: RunHostResolver,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Ask one authenticated host runner to stop its exact live owner."""
    body = {
        "task": task,
        "repo": repo,
        "owner_ref": owner_ref,
        "generation": generation,
        "source_revision": source_revision,
    }
    for attempt in range(2):
        active = resolver.resolve(host, force_refresh=attempt == 1)
        url = _operation_url(host, active, "/exec/session-stop")
        try:
            with httpx.Client(transport=transport, follow_redirects=False) as client:
                with client.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {active.token}"},
                    json=body,
                    # Domain cleanup and process reaping can exceed the default 5s.
                    timeout=httpx.Timeout(5, read=30),
                ) as response:
                    if response.status_code in {401, 403} and attempt == 0:
                        continue
                    if response.status_code >= 400:
                        raise RemoteExecError(
                            f"remote session stop was refused (HTTP {response.status_code})"
                        )
                    value = _bounded_response_json(response)
        except httpx.HTTPError:
            raise RemoteExecError(
                "remote host is unreachable; check relay pairing and host availability"
            ) from None
        if (
            not isinstance(value, dict)
            or set(value) != {"status"}
            or value["status"] not in {"stopped", "unknown", "invalid"}
        ):
            raise RemoteExecError("remote session stop response is invalid")
        return value["status"]
    raise RemoteExecError(
        "remote host rejected refreshed credentials; re-enrol the host and re-pair if needed"
    )




def cleanup_task_worktrees(
    *,
    task: str,
    repos: list[str],
    expected_revisions: dict[str, str],
    host: HostRegistration,
    resolver: RunHostResolver,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Ask one pinned host to remove only its certified task worktrees once."""
    if (
        not isinstance(task, str)
        or not task
        or not repos
        or len(set(repos)) != len(repos)
        or any(not isinstance(repo, str) or not repo for repo in repos)
        or set(expected_revisions) != set(repos)
        or any(
            not isinstance(sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", sha) is None
            for sha in expected_revisions.values()
        )
    ):
        raise ValueError("invalid remote task-worktree cleanup request")
    active = resolver.resolve(host)
    url = _operation_url(host, active, "/exec/task-worktrees-cleanup")
    try:
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            with client.stream(
                "POST",
                url,
                headers={"Authorization": f"Bearer {active.token}"},
                json={
                    "task": task,
                    "repos": repos,
                    "expected_revisions": expected_revisions,
                },
                timeout=httpx.Timeout(5, read=30),
            ) as response:
                if response.status_code >= 400:
                    raise RemoteExecError(
                        f"remote task-worktree cleanup was refused (HTTP {response.status_code})"
                    )
                value = _bounded_response_json(response)
    except httpx.HTTPError:
        raise RemoteExecError(
            "remote host is unreachable; check relay pairing and host availability"
        ) from None
    if (
        not isinstance(value, dict)
        or set(value) != {"status"}
        or value["status"] not in {"removed", "busy", "unknown", "invalid"}
    ):
        raise RemoteExecError("remote task-worktree cleanup response is invalid")
    return value["status"]


def _bounded_response_json(response: httpx.Response) -> object:
    payload = bytearray()
    for chunk in _raw_chunks(response):
        if len(payload) + len(chunk) > MAX_SOURCE_UPDATE_BYTES:
            raise RemoteExecError("remote source-update response is too large")
        payload.extend(chunk)
    try:
        return json.loads(payload)
    except UnicodeDecodeError, ValueError:
        raise RemoteExecError("remote source-update response is invalid") from None


def source_update_remote(
    *,
    host: HostRegistration,
    resolver: RunHostResolver,
    request: "SourceUpdateRequest",
    transport: httpx.BaseTransport | None = None,
) -> "SourceUpdateReply":
    """Exchange one bounded source-update stage without replay after acceptance."""
    from mship.core.session_source import SourceUpdateReply, SourceUpdateRequest

    if not isinstance(request, SourceUpdateRequest):
        raise ValueError("invalid source update request")
    body = request.to_dict()
    for attempt in range(2):
        active = resolver.resolve(host, force_refresh=attempt == 1)
        url = _operation_url(host, active, "/exec/source-update")
        try:
            with httpx.Client(transport=transport, follow_redirects=False) as client:
                with client.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {active.token}"},
                    json=body,
                    # Source phases drain observers and await Flutter before replying.
                    timeout=httpx.Timeout(5, read=120),
                ) as response:
                    if response.status_code in {401, 403} and attempt == 0:
                        continue
                    if response.status_code >= 400:
                        raise RemoteExecError(
                            f"remote source update was refused (HTTP {response.status_code})"
                        )
                    value = _bounded_response_json(response)
        except httpx.HTTPError:
            raise RemoteExecError(
                "remote host is unreachable; check relay pairing and host availability"
            ) from None
        try:
            return SourceUpdateReply.from_dict(value)
        except TypeError, ValueError:
            raise RemoteExecError("remote source-update response is invalid") from None
    raise RemoteExecError(
        "remote host rejected refreshed credentials; re-enrol the host and re-pair if needed"
    )


def _exec_tool_once(
    *,
    request: ToolRequest,
    conn: ResolvedRunHostConnection,
    workspace_id: str | None,
    event_sink: Callable[[ToolEvent], None] | None = None,
    session_source_revision: Callable[[str, str], str | None] | None = None,
    cancel_event: threading.Event | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ToolResult:
    """Execute one structured tool request against the already selected host.

    The request is sent once to the dedicated authenticated route.  Transport,
    redirect, HTTP-status, and framing failures become a payload-free typed
    result; this adapter never retries, switches routes, or invokes a local
    fallback.  Nonterminal events are delivered to ``event_sink`` as they
    arrive; the terminal result is delivered only after complete validation.
    """
    if not isinstance(request, ToolRequest):
        return ToolResult(status="invalid")

    headers = {"Authorization": f"Bearer {conn.token}"}
    workspace_prefix = (
        f"/workspaces/{quote(workspace_id, safe='')}" if workspace_id else ""
    )
    url = f"{conn.url.rstrip('/')}{workspace_prefix}/exec/tool"
    if cancel_event is not None and cancel_event.is_set():
        return ToolResult(status="cancelled")
    try:
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            with client.stream(
                "POST", url, headers=headers, json=request.to_dict()
            ) as response:
                if response.status_code >= 400:
                    # Host-tools reports preserve only typed transport/auth
                    # categories. Ordinary requests retain the established
                    # auth_error/protocol_error behavior.
                    if request.host_tools_action is not None:
                        status = {
                            401: "unauthed",
                            403: "unauthorized",
                            503: "workspace_unavailable",
                        }.get(response.status_code, "unreachable")
                        return ToolResult(status=status)
                    return ToolResult(
                        status="auth_error"
                        if response.status_code in {401, 403}
                        else "protocol_error"
                    )
                nonce = response.headers.get(NONCE_HEADER)
                if not nonce:
                    return ToolResult(status="protocol_error")
                timeout = response.request.extensions.get("timeout")
                if isinstance(timeout, dict):
                    timeout["read"] = None
                final: ToolResult | None = None
                accepted: ToolResult | None = None
                terminal_event: ToolEvent | None = None
                ready_seen = False
                stop_watcher = threading.Event()

                def cancel_stream_before_ready() -> None:
                    while not stop_watcher.wait(0.02):
                        if (
                            cancel_event is not None
                            and cancel_event.is_set()
                            and not ready_seen
                        ):
                            try:
                                response.close()
                            except httpx.HTTPError:
                                pass
                            return

                watcher = (
                    None
                    if cancel_event is None
                    else threading.Thread(
                        target=cancel_stream_before_ready, daemon=True
                    )
                )
                if watcher is not None:
                    watcher.start()
                try:
                    for event in iter_tool_events(
                        _raw_chunks(response),
                        nonce,
                    ):
                        if (
                            cancel_event is not None
                            and cancel_event.is_set()
                            and not ready_seen
                        ):
                            return ToolResult(status="cancelled")
                        if (
                            request.preparation == "discover"
                            and request.host_tools_action != "bootstrap"
                            and event.kind in {"stdout", "stderr"}
                        ):
                            return ToolResult(status="protocol_error")
                        if event.kind == "keepalive":
                            # A nonce-framed keepalive is protocol metadata, not
                            # task output. It may only follow accepted ownership
                            # and is deliberately withheld from application sinks.
                            if accepted is None:
                                return ToolResult(status="protocol_error")
                            continue

                        result = event.result
                        if result is not None:
                            if (
                                not (
                                    request.host_tools_action is not None
                                    and event.kind == "result"
                                    and result.host_tools_report is not None
                                )
                                and (
                                    event.kind == "started"
                                    or result.status in {"running", "completed"}
                                    or accepted is not None
                                )
                                and (
                                    result.owner_ref is None
                                    or result.generation is None
                                    or result.source_revision is None
                                )
                            ):
                                return ToolResult(status="protocol_error")
                            if (
                                request.owner_ref is not None
                                and result.owner_ref is not None
                                and (
                                    result.owner_ref != request.owner_ref
                                    or result.generation != request.generation
                                )
                            ):
                                return ToolResult(status="protocol_error")
                            if accepted is not None and (
                                result.owner_ref != accepted.owner_ref
                                or result.generation != accepted.generation
                            ):
                                return ToolResult(status="protocol_error")
                            # The exact accepted owner/generation was checked
                            # above. Its cancellation proves termination, not
                            # source content: close may already have deleted the
                            # controller row containing a hot-reloaded revision.
                            owner_cancelled = (
                                request.preparation == "launch"
                                and accepted is not None
                                and event.kind == "result"
                                and result.status == "cancelled"
                            )
                            expected_source = (
                                None if owner_cancelled else request.source_revision
                            )
                            if (
                                not owner_cancelled
                                and session_source_revision is not None
                                and accepted is not None
                                and result.owner_ref is not None
                                and result.generation is not None
                            ):
                                advanced_source = session_source_revision(
                                    result.owner_ref, result.generation
                                )
                                if advanced_source is not None:
                                    expected_source = advanced_source
                            if (
                                expected_source is not None
                                and result.source_revision is not None
                                and result.source_revision != expected_source
                            ):
                                return ToolResult(status="protocol_error")
                            if event.kind == "started":
                                if accepted is not None:
                                    return ToolResult(status="protocol_error")
                                accepted = result
                            if (
                                event.kind == "result"
                                and result.status == "completed"
                                and accepted is None
                                and request.host_tools_action is None
                                and (
                                    request.preparation != "observe"
                                    or bool(request.argv)
                                )
                            ):
                                return ToolResult(status="protocol_error")
                            if (
                                event.kind == "result"
                                and result.status == "running"
                                and (request.preparation != "observe" or request.argv)
                            ):
                                return ToolResult(status="protocol_error")
                            if (
                                request.preparation == "discover"
                                and event.kind == "result"
                                and (
                                    request.max_stdout_bytes is None
                                    or request.max_stderr_bytes is None
                                    or len(result.stdout) > request.max_stdout_bytes
                                    or len(result.stderr) > request.max_stderr_bytes
                                )
                            ):
                                return ToolResult(status="protocol_error")
                        if event.kind == "ready":
                            ready_seen = True
                        if event_sink is not None and event.kind != "result":
                            event_sink(event)
                        if event.kind == "result":
                            final = result
                            terminal_event = event
                    if final is None or terminal_event is None:
                        return ToolResult(status="protocol_error")
                    if event_sink is not None:
                        event_sink(terminal_event)
                    return final
                finally:
                    stop_watcher.set()
                    if watcher is not None:
                        watcher.join(timeout=1)
    except httpx.HTTPError:
        if cancel_event is not None and cancel_event.is_set():
            return ToolResult(status="cancelled")
        return ToolResult(
            status="unreachable"
            if request.host_tools_action is not None
            else "protocol_error"
        )
    except ToolProtocolError, UnicodeError, ValueError:
        return ToolResult(status="protocol_error")


def exec_tool(
    *,
    request: ToolRequest,
    host: HostRegistration,
    resolver: RunHostResolver,
    event_sink: Callable[[ToolEvent], None] | None = None,
    session_source_revision: Callable[[str, str], str | None] | None = None,
    cancel_event: threading.Event | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ToolResult:
    """Issue one typed request, retrying only a definite pre-start auth rejection."""
    for attempt in range(2):
        active = resolver.resolve(host, force_refresh=attempt == 1)
        observed_event = False

        def attempt_sink(event: ToolEvent) -> None:
            nonlocal observed_event
            observed_event = True
            if event_sink is not None:
                event_sink(event)

        result = _exec_tool_once(
            request=request,
            conn=active,
            workspace_id=getattr(host.connection, "workspace_id", None),
            event_sink=attempt_sink,
            session_source_revision=session_source_revision,
            transport=transport,
            cancel_event=cancel_event,
        )
        if (
            result.status != "auth_error"
            or observed_event
            or attempt == 1
            or not hasattr(host.connection, "workspace_id")
        ):
            return result
    return ToolResult(status="auth_error")
