import io
from datetime import datetime, timezone

import pytest
from rich.text import Text

from mship.cli.output import Output
from mship.cli.run_target import choose_profile, choose_run, choose_target
from mship.core.run_host.config import HostRegistration, RunHostConnection
from mship.core.run_target.models import (
    AppRun,
    SelectedTarget,
    TargetCandidate,
    TargetSelectionError,
    host_endpoint_fingerprint,
)


@pytest.fixture(autouse=True)
def _styled_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")


class _Stream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _output(*, tty: bool) -> tuple[Output, _Stream, _Stream]:
    stdout, stderr = _Stream(tty), _Stream(tty)
    return (
        Output(
            stream=stdout,
            err_stream=stderr,
            force_json=not tty,
            force_quiet=False,
            force_no_color=True,
        ),
        stdout,
        stderr,
    )


def _selected(
    host: str, label: str, alias: str, *, scope: str = "project"
) -> SelectedTarget:
    registration = HostRegistration(
        host,
        ("ios",),
        (),
        0,
        RunHostConnection(f"https://{host}.invalid", "secret"),
        scope,
    )
    candidate = TargetCandidate(
        target_key=f"private-{alias}",
        label=label,
        tags=(),
        aliases=(alias,),
        capabilities=("run",),
        ready=True,
        reason=None,
        remediation=None,
        preparation=(),
        rank=(19, 0),
        binding={"private": f"private-{alias}"},
    )
    return SelectedTarget(
        registration, candidate, "adapter-a", ("major", "minor"), "profile-revision"
    )


def _run(run_id: str, *, private_binding: str) -> AppRun:
    now = datetime.now(timezone.utc)
    return AppRun(
        id=run_id,
        task_slug="task",
        repo="app",
        profile="phone",
        profile_revision="profile-revision",
        backend="native",
        backend_revision="a" * 40,
        host_name="mobile",
        host_scope="project",
        host_endpoint_fingerprint=host_endpoint_fingerprint("https://mobile.invalid"),
        safe_target_label="Phone",
        private_binding_ref=private_binding,
        target_aliases=("phone",),
        operation="run",
        protocol_version=1,
        capabilities=("run", "logs"),
        owner_ref="owner",
        owner_generation="generation",
        status="active",
        revision=0,
        created_at=now,
        updated_at=now,
        binary_provenance=None,
    )


def test_run_chooser_displays_only_safe_recorded_run_fields():
    first = _run("run-a", private_binding="private-a" + "x" * 23)
    second = _run("run-b", private_binding="private-b" + "x" * 23)
    output, stdout, stderr = _output(tty=True)

    selected = choose_run(
        (first, second),
        interactive=True,
        input_fn=lambda _prompt: "2",
        output=output,
    )

    rendered = Text.from_ansi(stdout.getvalue() + stderr.getvalue()).plain
    assert selected == second
    assert "run-a" in rendered and "run-b" in rendered
    assert "Phone" in rendered and "mobile" in rendered
    assert "owner" not in rendered
    assert "generation" not in rendered
    assert "private-a" not in rendered and "private-b" not in rendered
    assert first.public_projection()["target_aliases"] == ("phone",)


def test_interactive_chooser_selects_numbered_safe_candidate_without_private_target_data():
    first = _selected("studio", "iPhone 15", "desk-phone", scope="user")
    second = _selected("air", "iPhone 16", "travel-phone")
    output, stdout, stderr = _output(tty=True)

    selected = choose_target(
        (first, second),
        profile_name="ios-latest",
        backend_name="flutter",
        interactive=True,
        input_fn=lambda _prompt: "2",
        output=output,
    )

    rendered = Text.from_ansi(stdout.getvalue() + stderr.getvalue()).plain
    assert selected == second
    assert "1." in rendered and "2." in rendered
    assert "studio" in rendered and "air" in rendered
    assert "iPhone 15" in rendered and "iPhone 16" in rendered
    assert "project" in rendered and "user" in rendered
    assert "ios-latest" in rendered and "flutter" in rendered
    assert "profile-revision" not in rendered and "adapter-a" not in rendered
    assert "private-desk-phone" not in rendered
    assert "private-travel-phone" not in rendered


def test_noninteractive_ambiguity_never_reads_input_and_returns_actionable_error():
    output, stdout, stderr = _output(tty=False)

    def no_input(_prompt: str) -> str:
        raise AssertionError("noninteractive chooser attempted input")

    with pytest.raises(TargetSelectionError) as error:
        choose_target(
            (
                _selected("studio", "iPhone 15", "desk"),
                _selected("air", "iPhone 16", "travel"),
            ),
            profile_name="ios-latest",
            backend_name="flutter",
            interactive=False,
            input_fn=no_input,
            output=output,
        )

    assert error.value.code == "target_ambiguous"
    assert "--host" in str(error.value) and "--target" in str(error.value)
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


def test_interactive_chooser_cancellation_is_explicit():
    output, _stdout, _stderr = _output(tty=True)
    with pytest.raises(TargetSelectionError) as error:
        choose_target(
            (
                _selected("studio", "iPhone 15", "desk"),
                _selected("air", "iPhone 16", "travel"),
            ),
            profile_name="ios-latest",
            backend_name="flutter",
            interactive=True,
            input_fn=lambda _prompt: "cancel",
            output=output,
        )
    assert error.value.code == "target_selection_cancelled"


def test_profile_chooser_uses_typed_names_and_noninteractive_never_reads_input():
    output, _stdout, _stderr = _output(tty=False)

    def no_input(_prompt: str) -> str:
        raise AssertionError("noninteractive profile chooser attempted input")

    with pytest.raises(TargetSelectionError) as error:
        choose_profile(
            ("ios-latest", "android-usb"),
            interactive=False,
            input_fn=no_input,
            output=output,
        )
    assert error.value.code == "profile_missing"

    tty_output, stdout, _stderr = _output(tty=True)
    selected = choose_profile(
        ("ios-latest", "android-usb"),
        interactive=True,
        input_fn=lambda _prompt: "1",
        output=tty_output,
    )
    assert selected == "ios-latest"
    assert "ios-latest" in stdout.getvalue()


def test_noninteractive_single_profile_requires_an_explicit_or_default_selection():
    output, _stdout, _stderr = _output(tty=False)

    def no_input(_prompt: str) -> str:
        raise AssertionError("noninteractive profile chooser attempted input")

    with pytest.raises(TargetSelectionError) as error:
        choose_profile(
            ("ios-latest",), interactive=False, input_fn=no_input, output=output
        )
    assert error.value.code == "profile_missing"


def test_forced_json_on_tty_never_prompts_or_prints_choices():
    stdout, stderr = _Stream(True), _Stream(True)
    output = Output(
        stream=stdout,
        err_stream=stderr,
        force_json=True,
        force_quiet=False,
    )

    def no_input(_prompt: str) -> str:
        raise AssertionError("JSON output attempted interactive input")

    with pytest.raises(TargetSelectionError) as error:
        choose_target(
            (
                _selected("studio", "iPhone 15", "desk"),
                _selected("air", "iPhone 16", "travel"),
            ),
            profile_name="ios-latest",
            backend_name="flutter",
            interactive=True,
            input_fn=no_input,
            output=output,
        )
    assert error.value.code == "target_ambiguous"
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""
