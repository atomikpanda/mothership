"""Codex event normalization and project-local hook installation."""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from mship.util.shell import ShellRunner


CODEX_HOOKS_PATH = Path(".codex/hooks.json")
CODEX_FEATURE_ENABLE_COMMAND = "codex features enable codex_hooks"
CODEX_TRUST_ACTION = "open `/hooks` in Codex to review and trust the project hooks"
CODEX_CAPABILITY_PROBE_TIMEOUT_SECONDS = 5
APPARMOR_USERNS_RESTRICTION_PATH = Path(
    "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
)
APPARMOR_BWRAP_PROFILE_PATH = Path("/etc/apparmor.d/bwrap-userns-restrict")
APPARMOR_CURRENT_PROFILE_PATH = Path("/proc/self/attr/current")
CODEX_SANDBOX_BOOTSTRAP_SIGNATURE = (
    "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"
)
BWRAP_APPARMOR_CONFINEMENT_RE = re.compile(
    r"^bwrap(?://&?unpriv_bwrap)? \(enforce\)$"
)
CODEX_COMMANDS = {
    "SessionStart": "mship _session-context --runtime codex",
    "PreToolUse": "mship _guard-edit --runtime codex",
    "Stop": "mship _drain --runtime codex",
}
CODEX_ENTRIES = {
    "SessionStart": {
        "hooks": [{
            "type": "command",
            "command": CODEX_COMMANDS["SessionStart"],
            "statusMessage": "Loading Mothership context",
        }],
    },
    "PreToolUse": {
        "matcher": "apply_patch|Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{
            "type": "command",
            "command": CODEX_COMMANDS["PreToolUse"],
            "statusMessage": "Checking Mothership edit policy",
        }],
    },
    "Stop": {
        "hooks": [{
            "type": "command",
            "command": CODEX_COMMANDS["Stop"],
        }],
    },
}

_EDIT_ALIASES = {"apply_patch", "Edit", "Write", "MultiEdit", "NotebookEdit"}
_PATCH_FILE_HEADER = re.compile(r"^\*\*\* (?:Add|Delete|Update) File:\s*(.+?)\s*$")
_PATCH_MOVE_HEADER = re.compile(r"^\*\*\* Move to:\s*(.+?)\s*$")


@dataclass(frozen=True)
class CodexInstallResult:
    status: str
    path: Path
    message: str = ""


class CodexHookCapability(str, Enum):
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    ENABLED = "enabled"
    TIMED_OUT = "timed-out"


@dataclass(frozen=True)
class CodexHookCapabilityResult:
    state: CodexHookCapability
    feature_name: str | None = None
    detail: str = ""


class CodexSandboxReadiness(str, Enum):
    NOT_APPLICABLE = "not-applicable"
    UNKNOWN = "unknown"
    UNREADABLE = "unreadable"
    PROFILE_MISSING = "profile-missing"
    PROFILE_PRESENT_UNVERIFIED = "profile-present-unverified"
    ACTIVE = "active"


@dataclass(frozen=True)
class CodexSandboxReadinessResult:
    state: CodexSandboxReadiness
    detail: str = ""


def format_codex_sandbox_warning(result: CodexSandboxReadinessResult) -> str:
    """Explain a non-ready sandbox result without probing nested bwrap."""
    return (
        f"Codex filesystem sandbox readiness could not be confirmed: "
        f"{result.detail}. If Codex reports "
        f"`{CODEX_SANDBOX_BOOTSTRAP_SIGNATURE}`, its outer filesystem sandbox "
        "failed before the requested edit runs; this is not a Mothership hook "
        "rejection. `MSHIP_BYPASS_GATE=1` bypasses only the WorkItem/spec gate "
        "and does not affect sandbox bootstrap. Do not test this by launching "
        "nested bwrap from Codex. Ask the host operator to enable the packaged "
        "`/usr/share/apparmor/extra-profiles/bwrap-userns-restrict` profile at "
        "`/etc/apparmor.d/bwrap-userns-restrict`, load and enforce it with "
        "`sudo aa-enforce /etc/apparmor.d/bwrap-userns-restrict`, restart "
        "Codex, and retry the original `apply_patch` edit."
    )


def inspect_codex_sandbox_readiness(
    *,
    codex_binary: str | None,
    bwrap_binary: str | None,
    platform_name: str | None = None,
    restriction_path: Path | None = None,
    profile_path: Path | None = None,
    current_profile_path: Path | None = None,
) -> CodexSandboxReadinessResult:
    """Inspect Ubuntu/AppArmor bwrap prerequisites without executing bwrap.

    A command probe is deliberately unsafe here: doctor can itself run inside
    Codex's outer bwrap sandbox, where a nested namespace failure is expected
    even when the host is configured correctly.
    """
    if codex_binary is None or (platform_name or platform.system()) != "Linux":
        return CodexSandboxReadinessResult(CodexSandboxReadiness.NOT_APPLICABLE)

    restriction = restriction_path or APPARMOR_USERNS_RESTRICTION_PATH
    profile = profile_path or APPARMOR_BWRAP_PROFILE_PATH
    current_profile = current_profile_path or APPARMOR_CURRENT_PROFILE_PATH
    try:
        confinement = current_profile.read_text().strip()
        confinement_error = ""
    except FileNotFoundError:
        confinement = ""
        confinement_error = ""
    except OSError as exc:
        confinement = ""
        confinement_error = (
            f"cannot read current AppArmor confinement at {current_profile}: {exc}"
        )
    if BWRAP_APPARMOR_CONFINEMENT_RE.fullmatch(confinement):
        return CodexSandboxReadinessResult(
            CodexSandboxReadiness.ACTIVE,
            detail=f"active AppArmor confinement: {confinement}",
        )

    if bwrap_binary is None:
        return CodexSandboxReadinessResult(CodexSandboxReadiness.NOT_APPLICABLE)

    try:
        restriction_value = restriction.read_text().strip()
    except FileNotFoundError:
        return CodexSandboxReadinessResult(CodexSandboxReadiness.NOT_APPLICABLE)
    except OSError as exc:
        return CodexSandboxReadinessResult(
            CodexSandboxReadiness.UNREADABLE,
            detail=f"cannot read userns restriction at {restriction}: {exc}",
        )
    if restriction_value == "0":
        return CodexSandboxReadinessResult(CodexSandboxReadiness.NOT_APPLICABLE)
    if restriction_value != "1":
        return CodexSandboxReadinessResult(
            CodexSandboxReadiness.UNKNOWN,
            detail=(
                f"userns restriction at {restriction} has unexpected value "
                f"{restriction_value!r}"
            ),
        )

    try:
        profile_mode = profile.stat().st_mode
    except FileNotFoundError:
        return CodexSandboxReadinessResult(
            CodexSandboxReadiness.PROFILE_MISSING,
            detail=(
                "Ubuntu's bwrap-userns-restrict AppArmor profile is missing at "
                f"{profile}"
            ),
        )
    except OSError as exc:
        return CodexSandboxReadinessResult(
            CodexSandboxReadiness.UNREADABLE,
            detail=f"cannot inspect AppArmor profile at {profile}: {exc}",
        )
    if not stat.S_ISREG(profile_mode):
        return CodexSandboxReadinessResult(
            CodexSandboxReadiness.UNKNOWN,
            detail=f"AppArmor profile path is not a regular file: {profile}",
        )
    if confinement_error:
        return CodexSandboxReadinessResult(
            CodexSandboxReadiness.UNREADABLE,
            detail=confinement_error,
        )
    return CodexSandboxReadinessResult(
        CodexSandboxReadiness.PROFILE_PRESENT_UNVERIFIED,
        detail=(
            f"AppArmor profile present at {profile}, but its loaded state is not "
            "visible from this process"
        ),
    )


def probe_codex_hook_capability(
    shell: ShellRunner,
    cwd: Path,
    *,
    codex_binary: str | None,
) -> CodexHookCapabilityResult:
    """Run and parse `codex features list` without mutating Codex config/trust."""
    if codex_binary is None:
        return CodexHookCapabilityResult(
            CodexHookCapability.ABSENT,
            detail="Codex is not installed",
        )

    try:
        result = shell.run(
            "codex features list",
            cwd=cwd,
            timeout=CODEX_CAPABILITY_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return CodexHookCapabilityResult(
            CodexHookCapability.TIMED_OUT,
            detail="Codex hook capability probe timed out",
        )

    if result.returncode != 0:
        return CodexHookCapabilityResult(
            CodexHookCapability.UNAVAILABLE,
            detail="Codex hook capability is unavailable",
        )

    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts or parts[0] not in {"codex_hooks", "hooks"}:
            continue
        enabled = parts[-1].lower()
        if enabled == "true":
            return CodexHookCapabilityResult(
                CodexHookCapability.ENABLED,
                feature_name=parts[0],
            )
        if enabled == "false":
            return CodexHookCapabilityResult(
                CodexHookCapability.DISABLED,
                feature_name=parts[0],
                detail="Codex hook capability is disabled",
            )

    return CodexHookCapabilityResult(
        CodexHookCapability.UNAVAILABLE,
        detail="Codex hook capability is unavailable",
    )


def _deduplicate(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _header_path(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        try:
            parsed = shlex.split(value)
        except ValueError as exc:
            raise ValueError("patch target path is malformed") from exc
        if len(parsed) != 1:
            raise ValueError("patch target path is ambiguous")
        value = parsed[0]
    if not value:
        raise ValueError("patch target path is empty")
    return value


def _patch_targets(patch: str) -> tuple[str, ...]:
    targets: list[str] = []
    for line in patch.splitlines():
        match = _PATCH_FILE_HEADER.match(line) or _PATCH_MOVE_HEADER.match(line)
        if match:
            targets.append(_header_path(match.group(1)))
    if not targets:
        raise ValueError("apply_patch edit did not contain a target path")
    return _deduplicate(targets)


def extract_edit_targets(event: dict[str, Any]) -> tuple[str, ...]:
    """Extract every path represented by a Codex guarded edit event."""
    tool_name = event.get("tool_name") or event.get("toolName")
    if tool_name not in _EDIT_ALIASES:
        return ()
    tool_input = event.get("tool_input") or event.get("input")
    if not isinstance(tool_input, dict):
        raise ValueError("guarded edit did not contain a target path")

    if tool_name == "apply_patch":
        patch = tool_input.get("command") or tool_input.get("patch")
        if not isinstance(patch, str):
            raise ValueError("apply_patch edit did not contain a target path")
        return _patch_targets(patch)

    targets: list[str] = []
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            targets.append(value)
    paths = tool_input.get("paths")
    if isinstance(paths, list):
        targets.extend(value for value in paths if isinstance(value, str) and value)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            value = edit.get("file_path") or edit.get("notebook_path") or edit.get("path")
            if isinstance(value, str) and value:
                targets.append(value)
    if not targets:
        raise ValueError("guarded edit did not contain a target path")
    return _deduplicate(targets)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _is_owned_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if len(parts) < 4 or parts[0] != "mship":
        return False
    owned_entrypoints = {
        "_session-context",
        "_guard-edit",
        "_drain",
    }
    if parts[1] not in owned_entrypoints:
        return False
    return any(
        parts[index:index + 2] == ["--runtime", "codex"]
        for index in range(2, len(parts) - 1)
    )

def registration_issues(data: dict[str, Any]) -> list[str]:
    """Return Codex events whose Mothership-owned registration is missing or stale."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return list(CODEX_ENTRIES)
    issues: list[str] = []
    for event, desired in CODEX_ENTRIES.items():
        groups = hooks.get(event)
        if not isinstance(groups, list):
            issues.append(event)
            continue
        if _owned_handler_count(groups) != 1 or not any(group == desired for group in groups):
            issues.append(event)
    return issues



def _owned_handler_count(groups: list[Any]) -> int:
    count = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            continue
        count += sum(1 for handler in handlers if _is_owned_handler(handler))
    return count


def _reconcile_event(groups: list[Any], event: str) -> tuple[list[Any], bool, bool]:
    desired = CODEX_ENTRIES[event]
    owned_count = _owned_handler_count(groups)
    if owned_count == 1 and any(group == desired for group in groups):
        return groups, False, True

    reconciled: list[Any] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            reconciled.append(group)
            continue
        handlers = group["hooks"]
        retained = [handler for handler in handlers if not _is_owned_handler(handler)]
        if retained:
            updated = dict(group)
            updated["hooks"] = retained
            reconciled.append(updated)
        elif len(retained) == len(handlers) or set(group) - {"matcher", "hooks"}:
            reconciled.append(group)
    reconciled.append(desired)
    return reconciled, True, owned_count > 0


def install_codex_hooks(workspace_root: Path) -> CodexInstallResult:
    """Reconcile only Mothership-owned handlers in `.codex/hooks.json`."""
    path = Path(workspace_root) / CODEX_HOOKS_PATH
    data: dict[str, Any] = {}
    if path.is_file():
        raw = path.read_text()
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return CodexInstallResult(
                "skipped", path,
                "hooks.json is not valid JSON; fix it and re-run initialization",
            )
        if not isinstance(loaded, dict):
            return CodexInstallResult("skipped", path, "hooks.json is not a JSON object")
        data = loaded

    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
        data["hooks"] = hooks
    elif not isinstance(hooks, dict):
        return CodexInstallResult("skipped", path, "hooks.json `hooks` is not an object")

    for event in CODEX_ENTRIES:
        groups = hooks.get(event)
        if groups is not None and not isinstance(groups, list):
            return CodexInstallResult(
                "skipped", path, f"hooks.json `{event}` registration is not a list",
            )
        if groups is not None and any(
            not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
            for group in groups
        ):
            return CodexInstallResult(
                "skipped",
                path,
                f"hooks.json `{event}` contains a malformed matcher group",
            )

    changed = False
    found_owned = False
    for event in CODEX_ENTRIES:
        groups = hooks.get(event, [])
        reconciled, event_changed, event_owned = _reconcile_event(groups, event)
        found_owned = found_owned or event_owned
        if event_changed:
            hooks[event] = reconciled
            changed = True

    if not changed:
        return CodexInstallResult("up to date", path)

    _atomic_write(path, json.dumps(data, indent=2) + "\n")
    return CodexInstallResult("updated" if found_owned else "installed", path)
