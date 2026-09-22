from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from mship.core.remote_client import exec_tool
from mship.core.remote_dispatch import run_remote_tool
from mship.core.remote_tool import (
    MAX_EVENT_FRAME_BYTES,
    ToolEvent,
    ToolProtocolError,
    ToolRequest,
    ToolResult,
    encode_tool_event,
    iter_tool_events,
)
from mship.core.run_host import HostRegistration, RunHostConnection, RunHostResolver
from mship.core.state import Task


NONCE = "nonceabcdef012345"


def _request(
    *, preparation: str = "launch", argv: tuple[str, ...] = ("tool",)
) -> ToolRequest:
    kwargs: dict[str, object] = {}
    if preparation == "discover":
        kwargs.update(max_stdout_bytes=1024, max_stderr_bytes=1024, timeout_seconds=1)
    if preparation == "observe":
        kwargs.update(owner_ref="owner-abcdef", generation="generation-abcdef")
    return ToolRequest(
        task="task-1",
        repo="app",
        argv=argv,
        preparation=preparation,
        **kwargs,
    )



def _host(connection: RunHostConnection) -> HostRegistration:
    return HostRegistration("host", ("role",), (), 0, connection, "project")

def _frame(event: ToolEvent) -> bytes:
    return encode_tool_event(event, NONCE)


def _stream(data: bytes):
    yield data


def test_tool_codec_rejects_truncation_wrong_nonce_and_oversized_frame_without_payload_echo():
    complete = _frame(ToolEvent("result", result=ToolResult("completed", exit_code=0)))

    with pytest.raises(ToolProtocolError) as truncated:
        list(iter_tool_events(iter([complete[:-1]]), NONCE))
    with pytest.raises(ToolProtocolError) as wrong_nonce:
        list(iter_tool_events(iter([complete]), "different-nonce"))
    with pytest.raises(ToolProtocolError) as oversized:
        list(
            iter_tool_events(
                iter(
                    [f"__MSHIP_TOOL__:{NONCE} {MAX_EVENT_FRAME_BYTES + 1}\n".encode()]
                ),
                NONCE,
            )
        )

    for failure in (truncated.value, wrong_nonce.value, oversized.value):
        assert "tool" in str(failure).lower()
        assert "argv" not in str(failure).lower()


def test_exec_tool_accepts_typed_keepalive_after_started_without_callback():
    identity = {
        "owner_ref": "owner-abcdef",
        "generation": "generation-abcdef",
        "source_revision": "abcdef1",
    }
    events = [
        ToolEvent("started", result=ToolResult("running", **identity)),
        ToolEvent("keepalive"),
        ToolEvent(
            "result",
            result=ToolResult("completed", exit_code=0, **identity),
        ),
    ]
    delivered: list[ToolEvent] = []

    result = exec_tool(
        request=_request(),
        host=_host(
            RunHostConnection(url="http://remote.example", token="secret-token")
        ),
        resolver=RunHostResolver(),
        event_sink=delivered.append,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=iter(_frame(event) for event in events),
                headers={"X-Mship-Exec-Nonce": NONCE},
            )
        ),
    )

    assert result.status == "completed"
    assert [event.kind for event in delivered] == ["started", "result"]


def test_exec_tool_rejects_keepalive_before_owner_acceptance():
    result = exec_tool(
        request=_request(),
        host=_host(
            RunHostConnection(url="http://remote.example", token="secret-token")
        ),
        resolver=RunHostResolver(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=iter(
                    (
                        _frame(ToolEvent("keepalive")),
                        _frame(
                            ToolEvent(
                                "result",
                                result=ToolResult("completed", exit_code=0),
                            )
                        ),
                    )
                ),
                headers={"X-Mship-Exec-Nonce": NONCE},
            )
        ),
    )

    assert result.status == "protocol_error"


def test_tool_request_rejects_unrepresentable_timeout_as_protocol_error():
    with pytest.raises(ToolProtocolError, match="invalid timeout"):
        ToolRequest(
            task="task-1",
            repo="app",
            argv=("tool",),
            timeout_seconds=10**400,
        )


@pytest.mark.parametrize(
    ("preparation", "argv"),
    [
        ("discover", ("tool",)),
        ("launch", ("tool",)),
        ("observe", ("tool",)),
    ],
)
def test_exec_tool_rejects_completed_execution_without_started(
    preparation: str, argv: tuple[str, ...]
):
    delivered: list[ToolEvent] = []
    event = ToolEvent(
        "result",
        result=ToolResult(
            "completed",
            exit_code=0,
            owner_ref="owner-abcdef",
            generation="generation-abcdef",
            source_revision="abcdef1",
        ),
    )

    result = exec_tool(request=_request(preparation=preparation, argv=argv), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), event_sink=delivered.append,
    transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=_stream(_frame(event)),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)

    assert result.status == "protocol_error"
    assert delivered == []


def test_exec_tool_streams_setup_then_tool_output_without_collecting_launch_output():
    events = [
        ToolEvent("stdout", data=b"setup\n"),
        ToolEvent(
            "started",
            result=ToolResult(
                "running",
                owner_ref="owner-abcdef",
                generation="generation-abcdef",
                source_revision="abcdef1",
            ),
        ),
        ToolEvent("stdout", data=b"tool output\n"),
        ToolEvent("stderr", data=b"diagnostic\n"),
        ToolEvent(
            "result",
            result=ToolResult(
                "completed",
                exit_code=7,
                owner_ref="owner-abcdef",
                generation="generation-abcdef",
                source_revision="abcdef1",
            ),
        ),
    ]
    delivered: list[ToolEvent] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/exec/tool"
        assert request.headers["authorization"] == "Bearer secret-token"
        return httpx.Response(
            200,
            content=iter(_frame(event) for event in events),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )

    result = exec_tool(request=_request(), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), event_sink=delivered.append,
    transport=httpx.MockTransport(handler),)

    assert result.status == "completed"
    assert result.exit_code == 7
    assert result.stdout == b""
    assert result.stderr == b""
    assert [event.kind for event in delivered] == [
        "stdout",
        "started",
        "stdout",
        "stderr",
        "result",
    ]
    assert (
        b"".join(event.data for event in delivered if event.kind == "stdout")
        == b"setup\ntool output\n"
    )


@pytest.mark.parametrize("kind", ["stdout", "stderr"])
def test_exec_tool_rejects_discovery_stream_output_before_callback(kind: str):
    events = [
        ToolEvent(kind, data=b"unbounded inventory\n"),
        ToolEvent(
            "started",
            result=ToolResult(
                "running",
                owner_ref="owner-abcdef",
                generation="generation-abcdef",
                source_revision="abcdef1",
            ),
        ),
        ToolEvent(
            "result",
            result=ToolResult(
                "completed",
                exit_code=0,
                owner_ref="owner-abcdef",
                generation="generation-abcdef",
                source_revision="abcdef1",
            ),
        ),
    ]
    delivered: list[ToolEvent] = []

    result = exec_tool(request=_request(preparation="discover"), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), event_sink=delivered.append,
    transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=iter(_frame(event) for event in events),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)

    assert result.status == "protocol_error"
    assert delivered == []


def test_exec_tool_accepts_bounded_discovery_result_after_started():
    events = [
        ToolEvent(
            "started",
            result=ToolResult(
                "running",
                owner_ref="owner-abcdef",
                generation="generation-abcdef",
                source_revision="abcdef1",
            ),
        ),
        ToolEvent(
            "result",
            result=ToolResult(
                "completed",
                exit_code=0,
                owner_ref="owner-abcdef",
                generation="generation-abcdef",
                source_revision="abcdef1",
                stdout=b"inventory\n",
                stderr=b"warning\n",
            ),
        ),
    ]
    delivered: list[ToolEvent] = []

    result = exec_tool(request=_request(preparation="discover"), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), event_sink=delivered.append,
    transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=iter(_frame(event) for event in events),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)

    assert result.status == "completed"
    assert result.stdout == b"inventory\n"
    assert result.stderr == b"warning\n"
    assert [event.kind for event in delivered] == ["started", "result"]


@pytest.mark.parametrize("output_field", ["stdout", "stderr"])
def test_exec_tool_rejects_discovery_result_over_requested_output_cap(
    output_field: str,
):
    over_cap = b"x" * 1025
    events = [
        ToolEvent(
            "started",
            result=ToolResult(
                "running",
                owner_ref="owner-abcdef",
                generation="generation-abcdef",
                source_revision="abcdef1",
            ),
        ),
        ToolEvent(
            "result",
            result=ToolResult(
                "completed",
                exit_code=0,
                owner_ref="owner-abcdef",
                generation="generation-abcdef",
                source_revision="abcdef1",
                stdout=over_cap if output_field == "stdout" else b"",
                stderr=over_cap if output_field == "stderr" else b"",
            ),
        ),
    ]
    delivered: list[ToolEvent] = []

    result = exec_tool(request=_request(preparation="discover"), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), event_sink=delivered.append,
    transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=iter(_frame(event) for event in events),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)

    assert result.status == "protocol_error"
    assert [event.kind for event in delivered] == ["started"]


@pytest.mark.parametrize("malformed_suffix", ["trailing", "duplicate"])
def test_exec_tool_defers_terminal_callback_until_stream_is_validated(
    malformed_suffix: str,
):
    started = ToolEvent(
        "started",
        result=ToolResult(
            "running",
            owner_ref="owner-abcdef",
            generation="generation-abcdef",
            source_revision="abcdef1",
        ),
    )
    completed = ToolEvent(
        "result",
        result=ToolResult(
            "completed",
            exit_code=0,
            owner_ref="owner-abcdef",
            generation="generation-abcdef",
            source_revision="abcdef1",
        ),
    )
    delivered: list[ToolEvent] = []
    chunks = [_frame(started), _frame(completed)]
    if malformed_suffix == "duplicate":
        chunks.append(_frame(completed))
    else:
        chunks.append(b"trailing bytes")

    result = exec_tool(request=_request(), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), event_sink=delivered.append,
    transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=iter(chunks),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)

    assert result.status == "protocol_error"
    assert [event.kind for event in delivered] == ["started"]


def test_observation_uses_pinned_owner_without_source_transfer_or_local_execution(
    tmp_path: Path,
):
    task = Task(
        slug="task-1",
        description="",
        phase="dev",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        affected_repos=["app"],
        worktrees={"app": tmp_path / "app"},
        branch="feat/task-1",
    )
    request = _request(preparation="observe", argv=())
    calls: list[str] = []

    class NoTransferShell:
        def run(self, *_args, **_kwargs):
            raise AssertionError("observation must not inspect or transfer source")

    class Output:
        def error(self, message: str) -> None:
            raise AssertionError(message)

    def handler(http_request: httpx.Request) -> httpx.Response:
        calls.append(str(http_request.url))
        body = _frame(
            ToolEvent(
                "result",
                result=ToolResult(
                    "completed",
                    exit_code=0,
                    owner_ref="owner-abcdef",
                    generation="generation-abcdef",
                    source_revision="abcdef1",
                ),
            )
        )
        return httpx.Response(
            200, content=_stream(body), headers={"X-Mship-Exec-Nonce": NONCE}
        )

    host = HostRegistration(
        "host", ("ios",), (), 0,
        RunHostConnection(url="http://remote.example", token="secret-token"),
        "project",
    )
    result = run_remote_tool(
        request=request,
        task_obj=task,
        config=object(),
        shell=NoTransferShell(),
        host=host,
        resolver=RunHostResolver(),
        output=Output(),
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "completed"
    assert calls == ["http://remote.example/exec/tool"]


def test_exec_tool_accepts_running_result_only_for_status_observation():
    request = _request(preparation="observe", argv=())
    event = ToolEvent(
        "result",
        result=ToolResult(
            "running",
            owner_ref="owner-abcdef",
            generation="generation-abcdef",
            source_revision="abcdef1",
        ),
    )

    result = exec_tool(request=request, host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=_stream(_frame(event)),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)

    assert result.status == "running"
    assert result.owner_ref == "owner-abcdef"


def test_exec_tool_rejects_running_result_for_non_status_request():
    event = ToolEvent(
        "result",
        result=ToolResult(
            "running",
            owner_ref="owner-abcdef",
            generation="generation-abcdef",
            source_revision="abcdef1",
        ),
    )

    result = exec_tool(request=_request(), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=_stream(_frame(event)),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)

    assert result.status == "protocol_error"


def test_exec_tool_rejects_mismatched_observation_owner():
    request = _request(preparation="observe", argv=())
    event = ToolEvent(
        "result",
        result=ToolResult(
            "completed",
            exit_code=0,
            owner_ref="other-owner",
            generation="generation-abcdef",
            source_revision="abcdef1",
        ),
    )

    result = exec_tool(request=request, host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=_stream(_frame(event)),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)

    assert result.status == "protocol_error"


def test_exec_tool_discards_hostile_error_body_without_reading_it():
    def hostile_body():
        raise AssertionError("client must not buffer an untrusted HTTP error body")
        yield b"unreachable"

    result = exec_tool(request=_request(), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), transport=httpx.MockTransport(
        lambda _request: httpx.Response(500, content=hostile_body())
    ),)

    assert result.status == "protocol_error"


@pytest.mark.parametrize(
    ("terminal_changes", "expected_status"),
    [
        ({}, "cancelled"),
        ({"owner_ref": "foreign-owner"}, "protocol_error"),
        ({"generation": "foreign-generation"}, "protocol_error"),
        ({"source_revision": None}, "protocol_error"),
        ({"status": "completed", "exit_code": 0}, "protocol_error"),
    ],
)
def test_launch_stop_receipt_after_source_update_and_controller_close(
    terminal_changes, expected_status
):
    request = ToolRequest(
        task="task-1",
        repo="app",
        argv=("tool",),
        preparation="launch",
        source_revision="abcdef1",
    )
    identity = {
        "owner_ref": "owner-abcdef",
        "generation": "generation-abcdef",
        "source_revision": "abcdef1",
    }
    terminal = {
        **identity,
        "status": "cancelled",
        "source_revision": "1234567",
        **terminal_changes,
    }
    events = [
        ToolEvent("started", result=ToolResult("running", **identity)),
        ToolEvent("ready", result=ToolResult("running", **identity)),
        ToolEvent("result", result=ToolResult(**terminal)),
    ]
    delivered = []
    result = exec_tool(
        request=request,
        host=_host(
            RunHostConnection(url="http://remote.example", token="secret-token")
        ),
        resolver=RunHostResolver(),
        # Close has already removed the row containing the updated source SHA.
        session_source_revision=lambda owner, generation: None,
        event_sink=delivered.append,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=iter(_frame(event) for event in events),
                headers={"X-Mship-Exec-Nonce": NONCE},
            )
        ),
    )

    assert result.status == expected_status
    assert any(event.kind == "result" for event in delivered) == (
        expected_status == "cancelled"
    )


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_exec_tool_rejects_mismatched_pinned_source_revision(status):
    request = ToolRequest(
        task="task-1",
        repo="app",
        argv=(),
        preparation="observe",
        owner_ref="owner-abcdef",
        generation="generation-abcdef",
        source_revision="abcdef1",
    )
    event = ToolEvent(
        "result",
        result=ToolResult(
            status,
            exit_code=0 if status == "completed" else None,
            owner_ref="owner-abcdef",
            generation="generation-abcdef",
            source_revision="1234567",
        ),
    )

    result = exec_tool(request=request, host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=_stream(_frame(event)),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)

    assert result.status == "protocol_error"


@pytest.mark.parametrize("missing", ["owner", "source"])
def test_exec_tool_rejects_success_that_drops_accepted_identity(missing):
    identity = {
        "owner_ref": "owner-abcdef",
        "generation": "generation-abcdef",
        "source_revision": "abcdef1",
    }
    started = ToolEvent("started", result=ToolResult("running", **identity))
    if missing == "owner":
        identity.update(owner_ref=None, generation=None)
    else:
        identity["source_revision"] = None
    final = ToolEvent("result", result=ToolResult("completed", exit_code=0, **identity))
    result = exec_tool(request=_request(), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=iter((_frame(started), _frame(final))),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)
    assert result.status == "protocol_error"


def test_exec_tool_rejects_excessively_nested_json_as_protocol_error():
    payload = b"[" * 2000 + b"0" + b"]" * 2000
    frame = f"__MSHIP_TOOL__:{NONCE} {len(payload)}\n".encode() + payload
    result = exec_tool(request=_request(), host=_host(RunHostConnection(url="http://remote.example", token="secret-token")), resolver=RunHostResolver(), transport=httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=_stream(frame),
            headers={"X-Mship-Exec-Nonce": NONCE},
        )
    ),)
    assert result.status == "protocol_error"
