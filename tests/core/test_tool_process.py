import sys
import threading
import time
from pathlib import Path

from mship.core.remote_tool import ToolContext, ToolRequest
from mship.core.tool_process import ToolOperationRegistry
from mship.core.session_runtime import SessionPreparation


_REVISION = "a" * 40


def _context(tmp_path: Path) -> ToolContext:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return ToolContext(
        task="demo",
        repo="app",
        worktree=worktree.resolve(),
        source_revision=_REVISION,
    )


def _discover(
    argv: tuple[str, ...], *, timeout: float = 2, stdout: int = 1024, stderr: int = 1024
) -> ToolRequest:
    return ToolRequest(
        task="demo",
        repo="app",
        argv=argv,
        preparation="discover",
        max_stdout_bytes=stdout,
        max_stderr_bytes=stderr,
        timeout_seconds=timeout,
    )


def _launch(argv: tuple[str, ...], *, timeout: float = 2) -> ToolRequest:
    return ToolRequest(
        task="demo",
        repo="app",
        argv=argv,
        preparation="launch",
        timeout_seconds=timeout,
    )


def test_quiet_discovery_times_out_and_discards_partial_inventory(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    events = list(
        registry.run(
            _discover(
                (sys.executable, "-c", "import time; time.sleep(60)"), timeout=0.15
            ),
            _context(tmp_path),
        )
    )

    result = events[-1].result
    assert result.status == "timeout"
    assert result.stdout == b""
    assert result.stderr == b""


def test_newline_free_stdout_flood_stops_the_owned_group(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    code = "import os,time; os.write(1, b'x' * 1025); time.sleep(60)"

    events = list(
        registry.run(
            _discover((sys.executable, "-c", code), stdout=1024),
            _context(tmp_path),
        )
    )

    assert events[-1].result.status == "stdout_limit"
    assert events[-1].result.stdout == b""


def test_timeout_reaps_descendants_in_the_owned_process_group(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    descendant_path = tmp_path / "descendant-pid"
    code = """
import os
import sys
import time
from pathlib import Path

child = os.fork()
if child == 0:
    while True:
        time.sleep(1)
Path(sys.argv[1]).write_text(str(child))
while True:
    time.sleep(1)
"""

    events = list(
        registry.run(
            _discover((sys.executable, "-c", code, str(descendant_path)), timeout=0.3),
            _context(tmp_path),
        )
    )

    if not sys.platform.startswith("linux"):
        return
    assert events[-1].result.status == "timeout"
    descendant_pid = int(descendant_path.read_text())
    proc_status = Path(f"/proc/{descendant_pid}/stat")
    try:
        state = proc_status.read_text().split()[2]
    except FileNotFoundError:
        state = None
    assert state in {None, "X", "Z"}


def test_stderr_flood_has_its_own_cap_and_no_discovery_output(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    code = "import os,time; os.write(2, b'e' * 1025); time.sleep(60)"

    events = list(
        registry.run(
            _discover((sys.executable, "-c", code), stderr=1024),
            _context(tmp_path),
        )
    )

    assert events[-1].result.status == "stderr_limit"
    assert events[-1].result.stderr == b""


def test_argv_and_server_selected_environment_are_exact(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    marker = "value; touch should-not-run"
    code = "import os,sys; os.write(1, (sys.argv[1] + ':' + os.environ['ONLY_THIS']).encode())"
    request = ToolRequest(
        task="demo",
        repo="app",
        argv=(sys.executable, "-c", code, marker),
        env={"ONLY_THIS": "present"},
        preparation="discover",
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
        timeout_seconds=2,
    )

    events = list(registry.run(request, _context(tmp_path)))

    assert events[-1].result.stdout == f"{marker}:present".encode()
    assert not (tmp_path / "should-not-run").exists()


def test_unsafe_cwd_symlink_is_rejected_before_spawning(tmp_path):
    context = _context(tmp_path)
    (context.worktree / "escape").symlink_to(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    request = ToolRequest(
        task="demo",
        repo="app",
        argv=(sys.executable, "-c", "raise SystemExit(1)"),
        cwd="escape",
        preparation="discover",
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
        timeout_seconds=2,
    )

    events = list(registry.run(request, context))

    assert len(events) == 1
    assert events[0].result.status == "invalid"


def test_pre_cancelled_run_does_not_launch_a_marker_child(tmp_path):
    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    cancelled = threading.Event()
    marker = tmp_path / "executed"
    cancelled.set()

    result = list(
        registry.run(
            _discover(
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                    str(marker),
                )
            ),
            context,
            cancel_event=cancelled,
        )
    )[-1].result

    assert result.status == "cancelled"
    assert not marker.exists()


def test_cancellation_during_evidence_preparation_does_not_launch_marker_child(
    monkeypatch, tmp_path
):
    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    cancelled = threading.Event()
    preparation_started = threading.Event()
    marker = tmp_path / "executed"
    original_prepare_output = registry._prepare_output

    def wait_for_cancellation(operation):
        preparation_started.set()
        assert cancelled.wait(2)
        original_prepare_output(operation)

    monkeypatch.setattr(registry, "_prepare_output", wait_for_cancellation)
    events = []
    runner = threading.Thread(
        target=lambda: events.extend(
            registry.run(
                _discover(
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                        str(marker),
                    )
                ),
                context,
                cancel_event=cancelled,
            )
        )
    )
    runner.start()
    assert preparation_started.wait(2)
    cancelled.set()
    runner.join(2)

    assert not runner.is_alive()
    assert events[-1].result.status == "cancelled"
    assert not marker.exists()


def test_status_refuses_mismatched_owner_generation_and_repo(tmp_path):
    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    events = list(
        registry.run(
            _discover((sys.executable, "-c", "import os; os.write(1, b'{}')")),
            context,
        )
    )
    result = events[-1].result

    assert (
        registry.status(
            task="demo",
            repo="other",
            owner_ref=result.owner_ref,
            generation=result.generation,
        ).status
        == "invalid"
    )
    assert (
        registry.status(
            task="demo",
            repo="app",
            owner_ref=result.owner_ref,
            generation="other",
        ).status
        == "unknown"
    )


def test_empty_observation_reads_durable_terminal_status(tmp_path):
    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    events = list(
        registry.run(
            _discover((sys.executable, "-c", "import os; os.write(1, b'{}')")),
            context,
        )
    )
    completed = events[-1].result
    observation = ToolRequest(
        task="demo",
        repo="app",
        argv=(),
        preparation="observe",
        owner_ref=completed.owner_ref,
        generation=completed.generation,
        source_revision=_REVISION,
        timeout_seconds=2,
    )

    observed = list(registry.observe(observation))

    assert observed[-1].result.status == "completed"
    assert observed[-1].result.owner_ref == completed.owner_ref


def test_same_state_dir_does_not_share_operation_identity_between_workspaces(tmp_path):
    state_dir = tmp_path / "shared-state"
    first_workspace = tmp_path / "first-workspace"
    second_workspace = tmp_path / "second-workspace"
    first_workspace.mkdir()
    second_workspace.mkdir()
    first_context = ToolContext(
        task="demo",
        repo="app",
        worktree=(first_workspace / "worktree"),
        source_revision=_REVISION,
    )
    first_context.worktree.mkdir()
    first = ToolOperationRegistry(first_workspace, state_dir=state_dir)
    events = list(
        first.run(
            _discover((sys.executable, "-c", "import os; os.write(1, b'{}')")),
            first_context,
        )
    )
    result = events[-1].result
    second = ToolOperationRegistry(second_workspace, state_dir=state_dir)

    assert (
        second.status(
            task="demo",
            repo="app",
            owner_ref=result.owner_ref,
            generation=result.generation,
        ).status
        == "unknown"
    )


def test_restart_treats_live_evidence_as_unknown_not_a_pid_to_signal(tmp_path):
    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    stream = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=10),
        context,
    )
    started = next(stream).result
    restarted = ToolOperationRegistry(tmp_path)
    try:
        assert restarted.admission_status("demo") == "unknown"
        assert (
            restarted.status(
                task="demo",
                repo="app",
                owner_ref=started.owner_ref,
                generation=started.generation,
            ).status
            == "unknown"
        )
        refused = list(
            restarted.run(
                _discover(
                    (sys.executable, "-c", "raise AssertionError('must not execute')")
                ),
                context,
            )
        )[-1].result
        assert refused.status == "unknown"
    finally:
        stream.close()


def test_same_task_distinct_repositories_have_independent_live_owners(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    app_context = _context(tmp_path)
    web_worktree = tmp_path / "web-worktree"
    web_worktree.mkdir()
    web_context = ToolContext(
        task="demo",
        repo="web",
        worktree=web_worktree.resolve(),
        source_revision=_REVISION,
    )
    app_stream = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=10),
        app_context,
    )
    web_stream = registry.run(
        ToolRequest(
            task="demo",
            repo="web",
            argv=(sys.executable, "-c", "import time; time.sleep(60)"),
            preparation="launch",
            timeout_seconds=10,
        ),
        web_context,
    )
    app_owner = next(app_stream).result
    web_owner = next(web_stream).result
    assert app_owner is not None and web_owner is not None
    try:
        assert registry.admission_status("demo", repo="app") == "busy"
        assert registry.admission_status("demo", repo="web") == "busy"
        assert registry.admission_status("demo") == "busy"
    finally:
        registry.stop_owner(
            task="demo",
            repo="app",
            owner_ref=app_owner.owner_ref,
            generation=app_owner.generation,
            source_revision=_REVISION,
        )
        registry.stop_owner(
            task="demo",
            repo="web",
            owner_ref=web_owner.owner_ref,
            generation=web_owner.generation,
            source_revision=_REVISION,
        )
        list(app_stream)
        list(web_stream)


def test_generic_profile_lifecycle_survives_ready_deadline(tmp_path, monkeypatch):
    registry = ToolOperationRegistry(tmp_path)
    context = _context(tmp_path)
    monkeypatch.setattr("mship.core.tool_process._GENERIC_READY_TIMEOUT_SECONDS", 1.0)
    script = """
import signal
import time
from mship.core.session_channel import OwnerContext

owner = OwnerContext.from_environ()
owner.begin()
owner.ready()
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit))
try:
    time.sleep(60)
finally:
    owner.finish(cleanup_known=True)
"""
    stream = registry.run(
        _launch((sys.executable, "-c", script), timeout=10),
        context,
        session=SessionPreparation(
            operation="run",
            owner_kind=None,
            sealed_context="{}",
        ),
    )
    started = next(stream)
    ready = next(stream)
    assert started.kind == "started"
    assert ready.kind == "ready"
    assert ready.result is not None
    time.sleep(1.1)
    assert (
        registry.status(
            task="demo",
            repo="app",
            owner_ref=ready.result.owner_ref,
            generation=ready.result.generation,
        ).status
        == "running"
    )
    try:
        assert (
            registry.stop_owner(
                task="demo",
                repo="app",
                owner_ref=ready.result.owner_ref,
                generation=ready.result.generation,
                source_revision=_REVISION,
            )
            == "stopped"
        )
    finally:
        list(stream)


def test_accepted_quiet_owner_stream_emits_typed_keepalives(tmp_path, monkeypatch):
    registry = ToolOperationRegistry(tmp_path)
    context = _context(tmp_path)
    monkeypatch.setattr(
        "mship.core.tool_process._STREAM_KEEPALIVE_SECONDS", 0.1
    )
    script = """
import signal
import time
from mship.core.session_channel import OwnerContext

owner = OwnerContext.from_environ()
owner.begin()
owner.ready()
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit))
try:
    time.sleep(60)
finally:
    owner.finish(cleanup_known=True)
"""
    stream = registry.run(
        _launch((sys.executable, "-c", script), timeout=10),
        context,
        session=SessionPreparation(
            operation="run",
            owner_kind=None,
            sealed_context="{}",
        ),
    )
    started = next(stream)
    ready = next(stream)
    assert started.kind == "started"
    assert ready.kind == "ready"
    assert ready.result is not None
    try:
        keepalive = next(stream)
        assert keepalive.kind == "keepalive"
        assert keepalive.data == b""
        assert keepalive.result is None
        assert (
            registry.status(
                task="demo",
                repo="app",
                owner_ref=ready.result.owner_ref,
                generation=ready.result.generation,
            ).status
            == "running"
        )
    finally:
        assert (
            registry.stop_owner(
                task="demo",
                repo="app",
                owner_ref=ready.result.owner_ref,
                generation=ready.result.generation,
                source_revision=_REVISION,
            )
            == "stopped"
        )
        list(stream)


def test_generic_session_without_cleanup_acknowledgement_is_unknown(
    tmp_path, monkeypatch
):
    registry = ToolOperationRegistry(tmp_path)
    monkeypatch.setattr(
        "mship.core.tool_process._GENERIC_CLEANUP_TIMEOUT_SECONDS", 0.05
    )
    script = """
import time
from mship.core.session_channel import OwnerContext

owner = OwnerContext.from_environ()
owner.begin()
owner.ready()
time.sleep(60)
"""
    stream = registry.run(
        _launch((sys.executable, "-c", script), timeout=10),
        _context(tmp_path),
        session=SessionPreparation(
            operation="run",
            owner_kind=None,
            sealed_context="{}",
        ),
    )
    ready = next(stream)
    assert ready.kind == "started"
    ready = next(stream)
    assert ready.kind == "ready"
    assert ready.result is not None
    try:
        assert (
            registry.stop_owner(
                task="demo",
                repo="app",
                owner_ref=ready.result.owner_ref,
                generation=ready.result.generation,
                source_revision=_REVISION,
            )
            == "unknown"
        )
    finally:
        assert list(stream)[-1].result.status == "unknown"


def test_stop_owner_accepts_exact_durable_clean_terminal_result(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    result = list(
        registry.run(_launch((sys.executable, "-c", "pass")), _context(tmp_path))
    )[-1].result
    assert result is not None
    assert result.status == "completed"
    assert (
        registry.stop_owner(
            task="demo",
            repo="app",
            owner_ref=result.owner_ref,
            generation=result.generation,
            source_revision=_REVISION,
        )
        == "stopped"
    )


def test_paused_consumer_cannot_prevent_deadline_cleanup(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    stream = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=0.15),
        _context(tmp_path),
    )
    assert next(stream).kind == "started"
    time.sleep(0.3)

    assert next(stream).result.status == "timeout"


def test_slow_stream_consumer_preserves_all_successful_output(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    chunk_count = 40
    request = _launch(
        (
            sys.executable,
            "-c",
            f"import os; os.write(1, b'x' * ({chunk_count} * 16384))",
        )
    )
    stream = registry.run(request, _context(tmp_path))
    assert next(stream).kind == "started"
    time.sleep(0.05)

    remaining = list(stream)
    output = b"".join(event.data for event in remaining if event.kind == "stdout")

    assert output == b"x" * (chunk_count * 16384)
    assert remaining[-1].result.status == "completed"


def test_observer_cancellation_does_not_cancel_its_live_parent(tmp_path):
    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    parent = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=10),
        context,
    )
    started = next(parent).result
    cancelled = threading.Event()
    observation = ToolRequest(
        task="demo",
        repo="app",
        argv=(sys.executable, "-c", "import time; time.sleep(60)"),
        preparation="observe",
        owner_ref=started.owner_ref,
        generation=started.generation,
        timeout_seconds=10,
    )
    observer = registry.observe(observation, cancel_event=cancelled)
    assert next(observer).kind == "started"
    cancelled.set()
    try:
        assert next(observer).result.status == "cancelled"
        assert (
            registry.status(
                task="demo",
                repo="app",
                owner_ref=started.owner_ref,
                generation=started.generation,
            ).status
            == "running"
        )
    finally:
        observer.close()
        parent.close()


def test_parent_teardown_cancels_observer_before_marker_child_spawns(
    monkeypatch, tmp_path
):
    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    parent = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=10),
        context,
    )
    started = next(parent).result
    marker = tmp_path / "observer-executed"
    preparation_started = threading.Event()
    release_preparation = threading.Event()
    cancellation_published = threading.Event()
    original_prepare_output = registry._prepare_output

    def hold_observer_preparation(operation):
        if not operation.indexed:
            preparation_started.set()
            assert release_preparation.wait(2)
        original_prepare_output(operation)

    monkeypatch.setattr(registry, "_prepare_output", hold_observer_preparation)
    parent_operation = registry._operations[(started.owner_ref, started.generation)]
    original_cancel_observers = parent_operation.cancel_observers

    def publish_cancellation():
        done = original_cancel_observers()
        cancellation_published.set()
        return done

    monkeypatch.setattr(parent_operation, "cancel_observers", publish_cancellation)
    events = []
    observer = threading.Thread(
        target=lambda: events.extend(
            registry.observe(
                ToolRequest(
                    task="demo",
                    repo="app",
                    preparation="observe",
                    argv=(
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                        str(marker),
                    ),
                    owner_ref=started.owner_ref,
                    generation=started.generation,
                    source_revision=_REVISION,
                )
            )
        )
    )
    teardown = threading.Thread(target=parent.close)
    observer.start()
    assert preparation_started.wait(2)
    teardown.start()
    assert cancellation_published.wait(2)
    release_preparation.set()
    observer.join(2)
    teardown.join(2)

    assert not observer.is_alive()
    assert not teardown.is_alive()
    assert events[-1].result.status == "cancelled"
    assert not marker.exists()


def test_private_output_write_failure_is_typed_and_not_partial(monkeypatch, tmp_path):
    registry = ToolOperationRegistry(tmp_path)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(registry, "_append_output", fail_write)
    events = list(
        registry.run(
            _discover((sys.executable, "-c", "import os; os.write(1, b'payload')")),
            _context(tmp_path),
        )
    )

    assert events[-1].result.status == "evidence_error"
    assert events[-1].result.stdout == b""


def test_host_toolchain_configuration_is_available_without_daemon_secrets(
    tmp_path, monkeypatch
):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "ready").write_text("configured toolchain")
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk))
    monkeypatch.setenv("MSHIP_DAEMON_SECRET", "private-daemon-value")
    code = (
        "import os; from pathlib import Path; "
        "assert 'MSHIP_DAEMON_SECRET' not in os.environ; "
        "print((Path(os.environ['ANDROID_SDK_ROOT']) / 'ready').read_text())"
    )
    result = list(
        ToolOperationRegistry(tmp_path).run(
            _discover((sys.executable, "-c", code)),
            _context(tmp_path),
        )
    )[-1].result
    assert result.status == "completed" and result.exit_code == 0
    assert result.stdout == b"configured toolchain\n"


def test_observation_rejects_replacement_of_the_live_parent_root(tmp_path):
    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    parent = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=10),
        context,
    )
    started = next(parent).result
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    context.worktree.rename(tmp_path / "original")
    context.worktree.symlink_to(replacement, target_is_directory=True)
    try:
        result = list(
            registry.observe(
                ToolRequest(
                    task="demo",
                    repo="app",
                    preparation="observe",
                    argv=(
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('observed').touch()",
                    ),
                    owner_ref=started.owner_ref,
                    generation=started.generation,
                    source_revision=_REVISION,
                )
            )
        )[-1].result
        assert result.status == "unknown"
        assert not (replacement / "observed").exists()
    finally:
        parent.close()


def test_evidence_parent_symlink_is_rejected_before_execution(tmp_path):
    state = tmp_path / ".mothership"
    state.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (state / "remote-tool-operations").symlink_to(redirected, target_is_directory=True)
    marker = tmp_path / "executed"
    result = list(
        ToolOperationRegistry(tmp_path).run(
            _discover(
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                    str(marker),
                )
            ),
            _context(tmp_path),
        )
    )[-1].result
    assert result.status == "evidence_error"
    assert not marker.exists()
    assert not tuple(redirected.iterdir())


def test_corrupt_active_evidence_returns_a_typed_failure(tmp_path):
    registry = ToolOperationRegistry(tmp_path)
    context = _context(tmp_path)
    events = list(registry.run(_discover((sys.executable, "-c", "pass")), context))
    assert events[-1].result.status == "completed"
    registry._active_path("demo").write_text("{")
    result = list(
        registry.run(
            _discover(
                (sys.executable, "-c", "raise AssertionError('must not execute')")
            ),
            context,
        )
    )[-1].result
    assert result.status == "evidence_error"


def test_operation_checkpoints_are_visible_to_native_journal_readers(tmp_path):
    from mship.core.log import LogManager

    state = tmp_path / ".mothership"
    logs = state / "logs"
    logs.mkdir(parents=True)
    state.chmod(0o775)
    logs.chmod(0o775)
    journal = logs / "demo.md"
    journal.write_text("# Task Log: demo\n")
    journal.chmod(0o664)
    result = list(
        ToolOperationRegistry(tmp_path).run(
            _discover((sys.executable, "-c", "print('private output')")),
            _context(tmp_path),
        )
    )[-1].result
    entries = LogManager(tmp_path / ".mothership" / "logs").read("demo")
    assert [entry.message for entry in entries] == ["starting", "running", "completed"]
    assert all(
        entry.id == result.owner_ref and entry.parent == result.generation
        for entry in entries
    )


def test_observation_cannot_be_redirected_after_cwd_validation(tmp_path):
    from mship.util.shell import ShellRunner

    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    parent = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=10),
        context,
    )
    started = next(parent).result
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    original = tmp_path / "original"

    def race(args, cwd, env):
        context.worktree.rename(original)
        context.worktree.symlink_to(replacement, target_is_directory=True)
        return ShellRunner().spawn_argv(args, cwd, env)

    try:
        result = list(
            registry.observe(
                ToolRequest(
                    task="demo",
                    repo="app",
                    preparation="observe",
                    argv=(
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('observed').touch()",
                    ),
                    owner_ref=started.owner_ref,
                    generation=started.generation,
                    source_revision=_REVISION,
                ),
                spawn=race,
            )
        )[-1].result
        assert result.status == "completed" and result.exit_code == 0
        assert (original / "observed").exists()
        assert not (replacement / "observed").exists()
    finally:
        parent.close()


def test_cwd_replacement_after_identity_capture_refuses_marker_child(
    monkeypatch, tmp_path
):
    import mship.core.tool_process as tool_process

    context = _context(tmp_path)
    selected = context.worktree / "selected"
    selected.mkdir()
    original = context.worktree / "selected-original"
    replacement = tmp_path / "replacement"
    marker = "executed"
    registry = ToolOperationRegistry(tmp_path)
    parent = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=10),
        context,
    )
    started = next(parent).result
    original_open = tool_process.os.open
    swapped = threading.Event()

    def replace_before_descriptor_open(path, flags, *args, **kwargs):
        if path == "selected" and not swapped.is_set():
            selected.rename(original)
            replacement.mkdir()
            replacement.rename(selected)
            swapped.set()
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(tool_process.os, "open", replace_before_descriptor_open)
    try:
        result = list(
            registry.observe(
                ToolRequest(
                    task="demo",
                    repo="app",
                    preparation="observe",
                    argv=(
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('executed').touch()",
                    ),
                    cwd="selected",
                    owner_ref=started.owner_ref,
                    generation=started.generation,
                    source_revision=_REVISION,
                )
            )
        )[-1].result

        assert swapped.is_set()
        assert result.status == "launch_error"
        assert not (original / marker).exists()
        assert not (selected / marker).exists()
    finally:
        parent.close()


def test_completed_observer_releases_its_external_cancellation_event(tmp_path):
    import gc
    import weakref

    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    parent = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=10),
        context,
    )
    started = next(parent).result
    cancel = threading.Event()
    reference = weakref.ref(cancel)
    try:
        result = list(
            registry.observe(
                ToolRequest(
                    task="demo",
                    repo="app",
                    preparation="observe",
                    argv=(sys.executable, "-c", "pass"),
                    owner_ref=started.owner_ref,
                    generation=started.generation,
                    source_revision=_REVISION,
                ),
                cancel_event=cancel,
            )
        )[-1].result
        assert result.status == "completed"
        del cancel
        gc.collect()
        assert reference() is None
    finally:
        if reference() is not None:
            reference().set()
        parent.close()


def test_parent_teardown_waits_for_admitted_observer_spawn(tmp_path, monkeypatch):
    from mship.util.shell import ShellRunner

    context = _context(tmp_path)
    registry = ToolOperationRegistry(tmp_path)
    cancel = threading.Event()
    parent = registry.run(
        _launch((sys.executable, "-c", "import time; time.sleep(60)"), timeout=10),
        context,
        cancel_event=cancel,
    )
    started = next(parent).result
    operation = registry._operations[(started.owner_ref, started.generation)]
    spawning = threading.Event()
    teardown_waiting = threading.Event()

    class ObservedAdmissionLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if spawning.is_set():
                teardown_waiting.set()
            self.lock.acquire()

        def __exit__(self, *_args):
            self.lock.release()

    monkeypatch.setattr(operation, "observer_lock", ObservedAdmissionLock())

    def spawn(args, cwd, env):
        spawning.set()
        cancel.set()
        assert teardown_waiting.wait(timeout=3)
        assert not operation.stop.is_set()
        assert not operation.leader_exited()
        return ShellRunner().spawn_argv(args, cwd, env)

    try:
        events = list(
            registry.observe(
                ToolRequest(
                    task="demo",
                    repo="app",
                    preparation="observe",
                    argv=(sys.executable, "-c", "pass"),
                    owner_ref=started.owner_ref,
                    generation=started.generation,
                    source_revision=_REVISION,
                ),
                spawn=spawn,
            )
        )
        assert any(event.kind == "started" for event in events)
        assert events[-1].result.status in {"completed", "cancelled"}
    finally:
        parent.close()
