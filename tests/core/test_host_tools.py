import json
from pathlib import Path

import pytest

from mship.core.host_tools import (
    HostToolCommand,
    HostToolIdentity,
    HostToolInvocation,
    HostToolsConfig,
    declaration_inputs,
    diagnose,
    receipt_path,
    record_receipt,
)


def _declaration(**mise):
    return HostToolsConfig.model_validate({"mise": {"manifest": ".mise.toml", **mise}})


def _runner():
    def run(command: HostToolCommand) -> HostToolInvocation:
        if command.argv == ("mise", "--version"):
            return HostToolInvocation("completed", 0, b"2026.9.7\n")
        if command.argv == ("mise", "config", "--json"):
            return HostToolInvocation(
                "completed",
                0,
                json.dumps(
                    [
                        {"path": str(command.environment["MISE_GLOBAL_CONFIG_FILE"]), "tools": {}},
                        {"path": str(command.cwd / ".mise.toml"), "tools": {}},
                    ]
                ).encode(),
            )
        if command.argv == ("mise", "ls", "--current", "--json"):
            return HostToolInvocation("completed", 0, b"{}")
        raise AssertionError(f"unexpected command: {command.argv}")
    return run


def _diagnose(worktree: Path, state: Path):
    return diagnose(
        declaration=_declaration(),
        worktree=worktree,
        state_dir=state,
        identity=HostToolIdentity("host", "android", "server", "f" * 64),
        task="task",
        repo="app",
        source_revision="a" * 40,
        run=_runner(),
    )


def test_declaration_rejects_undeclared_native_lock(tmp_path: Path):
    (tmp_path / ".mise.toml").write_text("[tools]\n")
    (tmp_path / "other.lock").write_text("")
    with pytest.raises(ValueError, match="active manifest lock"):
        declaration_inputs(_declaration(lock="other.lock"), tmp_path)


def test_diagnosis_rejects_shadowed_project_configuration(tmp_path: Path):
    (tmp_path / ".mise.toml").write_text("[tools]\n")
    state = tmp_path / "state"

    def shadowed(command: HostToolCommand) -> HostToolInvocation:
        if command.argv == ("mise", "config", "--json"):
            return HostToolInvocation(
                "completed",
                0,
                json.dumps(
                    [
                        {"path": str(command.environment["MISE_GLOBAL_CONFIG_FILE"])},
                        {"path": str(tmp_path / ".mise.toml")},
                        {"path": str(tmp_path / "mise.local.toml")},
                    ]
                ).encode(),
            )
        if command.argv == ("mise", "--version"):
            return HostToolInvocation("completed", 0, b"mise")
        raise AssertionError("current configuration must not run after a shadow config")

    result = diagnose(
        declaration=_declaration(),
        worktree=tmp_path,
        state_dir=state,
        identity=HostToolIdentity("host", "android", "server", "f" * 64),
        task="task",
        repo="app",
        source_revision="a" * 40,
        run=shadowed,
    )
    assert result.status == "invalid_configuration"


def test_receipt_is_separate_safe_host_tools_state(tmp_path: Path):
    (tmp_path / ".mise.toml").write_text("[tools]\n")
    state = tmp_path / "state"
    resolution = _diagnose(tmp_path, state)
    assert resolution.status == "healthy"
    record_receipt(state, resolution)
    path = receipt_path(state, resolution)
    assert path.parent == state / "host-tools"
    stored = json.loads(path.read_text())
    assert stored["status"] == "healthy"
    assert "environment" not in path.read_text()


def test_symlinked_manifest_is_never_an_eligible_input(tmp_path: Path):
    outside = tmp_path.parent / "outside-mise.toml"
    outside.write_text("[tools]\n")
    (tmp_path / ".mise.toml").symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe"):
        declaration_inputs(_declaration(), tmp_path)

@pytest.mark.parametrize(
    ("requirement", "root_exists", "licenses", "config", "expected"),
    [
        ({"id": "android-sdk-root", "kind": "path"}, False, False, True, "missing_path"),
        ({"id": "android-sdk-license", "kind": "license"}, True, False, True, "missing_license"),
        ({"id": "android-sdk-config", "kind": "config"}, True, True, False, "missing_config"),
    ],
)
def test_android_prerequisites_have_distinct_server_checked_states(
    tmp_path: Path, requirement: dict[str, str], root_exists: bool, licenses: bool, config: bool, expected: str
):
    (tmp_path / ".mise.toml").write_text("[tools]\n")
    sdk = tmp_path / "sdk"
    if root_exists:
        sdk.mkdir()
    if licenses:
        (sdk / "licenses").mkdir()
        (sdk / "licenses" / "android-sdk-license").write_text("accepted")

    def run(command: HostToolCommand) -> HostToolInvocation:
        if command.argv == ("mise", "--version"):
            return HostToolInvocation("completed", 0)
        if command.argv == ("mise", "config", "--json"):
            return HostToolInvocation("completed", 0, json.dumps([
                {"path": str(command.environment["MISE_GLOBAL_CONFIG_FILE"])},
                {"path": str(tmp_path / ".mise.toml")},
            ]).encode())
        if command.argv == ("mise", "ls", "--current", "--json"):
            return HostToolInvocation("completed", 0, b"{}")
        if command.argv == ("android", "info", "sdk"):
            return HostToolInvocation("completed", 0, str(sdk).encode())
        if command.argv == ("android", "info", "config"):
            return HostToolInvocation("completed", 0, b"configured" if config else b"")
        raise AssertionError(command.argv)

    declaration = HostToolsConfig.model_validate({
        "mise": {"manifest": ".mise.toml"}, "requirements": [requirement]
    })
    result = diagnose(
        declaration=declaration, worktree=tmp_path, state_dir=tmp_path / "state",
        identity=HostToolIdentity("host", "android", "server", "f" * 64),
        task="task", repo="app", source_revision="a" * 40, run=run,
    )
    assert result.status == expected
