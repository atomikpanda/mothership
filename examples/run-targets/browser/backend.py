#!/usr/bin/env python3
"""Configured Playwright engine and managed-instance backend example.

The browser control endpoint stays only in a per-run private receipt. Discovery reads
engine fingerprints and configured instance records; it never launches an
engine or creates a page.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import select
import signal
import selectors
import secrets
import stat
import subprocess
import sys
import time
from typing import Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (
    ExampleError,
    emit_inventory,
    load_bindings,
    load_context,
    load_request,
)  # noqa: E402
from mship.core.session_channel import OwnerContext
from mship.core.session_inputs import SessionError

_LIMIT = 1024 * 1024
_TOOL_LIMIT = 64 * 1024
_TIMEOUT = 30.0
_ENGINES = frozenset(("chromium", "firefox", "webkit"))
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class BackendError(RuntimeError):
    """A safe browser backend failure."""


def _fail(message: str) -> BackendError:
    return BackendError(message)


@dataclass(frozen=True)
class ManagedInstance:
    token: str
    engine: str
    headless: bool
    page_url: str | None
    capture_layout: bool
    fingerprint: str


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _regular_file(path: Path) -> os.stat_result:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise _fail("configured browser tool is unavailable") from error
    if not stat.S_ISREG(info.st_mode):
        raise _fail("configured browser tool is invalid")
    return info


def _private_regular(path: Path) -> os.stat_result:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise _fail("configured browser input is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise _fail("configured browser input is not private")
    return info


def _private_directory(path: Path) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise _fail("configured browser instance directory is unavailable") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise _fail("configured browser instance directory is not private")


def _private_control_socket(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise _fail("selected browser receipt is invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or len(os.fsencode(path)) > 103:
        raise _fail("selected browser receipt is invalid")
    try:
        parent = path.parent.lstat()
        info = path.lstat()
    except OSError as error:
        raise _fail("selected browser receipt is invalid") from error
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
        or not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise _fail("selected browser receipt is invalid")
    return str(path)


def _private_control_token(value: object) -> str:
    if not isinstance(value, str) or _CONTROL_TOKEN.fullmatch(value) is None:
        raise _fail("selected browser receipt is invalid")
    try:
        decoded = base64.urlsafe_b64decode(f"{value}=")
    except ValueError as error:
        raise _fail("selected browser receipt is invalid") from error
    if (
        len(decoded) != 32
        or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value
    ):
        raise _fail("selected browser receipt is invalid")
    return value


def _absolute_file(value: object, *, executable: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise _fail("configured browser tool is invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise _fail("configured browser tool is invalid")
    _regular_file(path)
    if executable and not os.access(path, os.X_OK):
        raise _fail("configured browser tool is unavailable")
    return str(path)


def _browser_config(bindings: Mapping[str, object]) -> dict[str, str]:
    paths = bindings.get("paths")
    browser = paths.get("browser") if isinstance(paths, dict) else None
    if not isinstance(browser, dict) or set(browser) != {
        "node",
        "playwright_module",
        "instances_dir",
    }:
        raise _fail("browser host bindings are unavailable")
    node = _absolute_file(browser["node"], executable=True)
    module = _absolute_file(browser["playwright_module"])
    value = browser["instances_dir"]
    if not isinstance(value, str) or "\x00" in value:
        raise _fail("configured browser instance directory is invalid")
    instances = Path(value)
    if not instances.is_absolute() or ".." in instances.parts:
        raise _fail("configured browser instance directory is invalid")
    _private_directory(instances)
    _private_directory(instances / "runs")
    return {"node": node, "playwright_module": module, "instances_dir": str(instances)}


def _aliases(bindings: Mapping[str, object]) -> dict[str, str]:
    raw = bindings.get("aliases")
    browser = raw.get("browser", {}) if isinstance(raw, dict) else None
    if not isinstance(browser, dict):
        raise _fail("browser aliases are invalid")
    aliases: dict[str, str] = {}
    for alias, token in browser.items():
        if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
            raise _fail("browser aliases are invalid")
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise _fail("browser aliases are invalid")
        aliases[alias] = token
    return aliases


def _record(raw: object) -> ManagedInstance:
    if not isinstance(raw, dict) or set(raw) not in (
        {"version", "token", "engine", "headless"},
        {"version", "token", "engine", "headless", "page_url"},
        {"version", "token", "engine", "headless", "page_url", "capture_layout"},
    ):
        raise _fail("managed browser instance record is invalid")
    token, engine, headless = raw["token"], raw["engine"], raw["headless"]
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        raise _fail("managed browser instance record is invalid")
    if engine not in _ENGINES:
        raise _fail("managed browser instance uses an unsupported browser engine")
    if raw["version"] != 1 or type(headless) is not bool:
        raise _fail("managed browser instance record is invalid")
    page_url = raw.get("page_url")
    if page_url is not None and (
        not isinstance(page_url, str)
        or len(page_url) > 2048
        or not (page_url.startswith("https://") or page_url.startswith("http://"))
        or any(character.isspace() for character in page_url)
    ):
        raise _fail("managed browser page URL is invalid")
    capture_layout = raw.get("capture_layout", False)
    if type(capture_layout) is not bool or (capture_layout and page_url is None):
        raise _fail("managed browser instance record is invalid")
    return ManagedInstance(
        token, engine, headless, page_url, capture_layout, _digest(raw)
    )


def _read_private_json(path: Path) -> object:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_size > _LIMIT
            ):
                raise _fail("managed browser instance record is invalid")
            payload = stream.read(_LIMIT + 1)
    except OSError as error:
        raise _fail("managed browser instance record is unavailable") from error
    if len(payload) > _LIMIT:
        raise _fail("managed browser instance record is too large")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail("managed browser instance record is invalid") from error


def _load_records(config: Mapping[str, str]) -> list[ManagedInstance]:
    directory = Path(config["instances_dir"])
    _private_directory(directory)
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        raise _fail("configured browser instance directory is unavailable") from error
    records: list[ManagedInstance] = []
    for path in entries:
        if path.name == "runs" or path.suffix != ".json":
            continue
        record = _record(_read_private_json(path))
        if path.stem != record.token:
            raise _fail("managed browser instance record identity is invalid")
        records.append(record)
    if len({record.token for record in records}) != len(records):
        raise _fail("managed browser instance records duplicate an identity")
    return sorted(records, key=lambda item: item.token)


def _load_record(config: Mapping[str, str], token: str) -> ManagedInstance:
    if _TOKEN.fullmatch(token) is None:
        raise _fail("selected browser identity is invalid")
    record = _record(
        _read_private_json(Path(config["instances_dir"]) / f"{token}.json")
    )
    if record.token != token:
        raise _fail("selected browser identity changed")
    return record


def _environment() -> dict[str, str]:
    return {
        "HOME": str(Path.home()),
        "PATH": os.defpath,
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }


def _stop_bounded_driver(process: subprocess.Popen[bytes]) -> None:
    """Reap a finite driver helper without affecting the caller's process group."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _bounded_driver(
    argv: Sequence[str], payload: Mapping[str, object]
) -> dict[str, object]:
    encoded = _canonical(payload).encode("utf-8")
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=False,
            env=_environment(),
        )
    except OSError as error:
        raise _fail("configured browser automation tool is unavailable") from error
    stdout, stderr = bytearray(), bytearray()
    selector = selectors.DefaultSelector()
    try:
        assert (
            process.stdin is not None
            and process.stdout is not None
            and process.stderr is not None
        )
        process.stdin.write(encoded)
        process.stdin.close()
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        deadline = time.monotonic() + _TIMEOUT
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _event in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                destination: bytearray = key.data
                destination.extend(chunk)
                if len(destination) > _TOOL_LIMIT:
                    raise ValueError
        if process.wait(timeout=1) != 0:
            raise _fail("configured browser automation operation failed")
    except (OSError, TimeoutError, ValueError, subprocess.TimeoutExpired) as error:
        _stop_bounded_driver(process)
        if isinstance(error, BackendError):
            raise
        raise _fail("configured browser automation operation is unavailable") from error
    except BaseException:
        _stop_bounded_driver(process)
        raise
    finally:
        selector.close()
        _stop_bounded_driver(process)
    try:
        value = json.loads(bytes(stdout).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _fail("configured browser automation response is invalid") from error
    if not isinstance(value, dict):
        raise _fail("configured browser automation response is invalid")
    return value


def _driver(
    action: str, config: Mapping[str, str], **payload: object
) -> dict[str, object]:
    if action not in {"probe", "observe", "capture"}:
        raise _fail("browser automation operation is invalid")
    return _bounded_driver(
        (config["node"], str(Path(__file__).with_name("driver.mjs")), action),
        {"playwright_module": config["playwright_module"], **payload},
    )


def _foreground(action: str, config: Mapping[str, str], **payload: object) -> None:
    if action not in {"launch", "logs"}:
        raise _fail("browser automation operation is invalid")
    process: subprocess.Popen[bytes] | None = None
    handlers: dict[int, object] = {}
    cancelled = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal cancelled
        cancelled = True
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, request_stop)
        process = subprocess.Popen(
            (config["node"], str(Path(__file__).with_name("driver.mjs")), action),
            stdin=subprocess.PIPE,
            start_new_session=True,
            env=_environment(),
        )
        assert process.stdin is not None
        process.stdin.write(
            _canonical(
                {"playwright_module": config["playwright_module"], **payload}
            ).encode("utf-8")
        )
        process.stdin.close()
        while True:
            try:
                result = process.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancelled:
                    _stop_owned_driver(process)
        if cancelled:
            raise _fail("configured browser automation operation stopped")
        if result:
            raise _fail("configured browser automation operation failed")
    except OSError as error:
        raise _fail("configured browser automation operation is unavailable") from error
    finally:
        if process is not None:
            _stop_owned_driver(process)
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def _stop_owned_driver(process: subprocess.Popen[bytes]) -> None:
    """Reap a driver in its own process group, escalating only when necessary."""
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _cleanup_control(process: subprocess.Popen[bytes]) -> bool:
    if process.stdout is None:
        return False
    control = process.stdout.readline(4097)
    if len(control) > 4096:
        return False
    try:
        return json.loads(control.decode("utf-8")) == {"cleanup": True}
    except UnicodeDecodeError, json.JSONDecodeError:
        return False


def _launch_foreground(
    config: Mapping[str, str], context: Mapping[str, object], **payload: object
) -> None:
    """Own the generic supervised lifecycle around the exact browser server."""
    del context
    process: subprocess.Popen[bytes] | None = None
    owner: OwnerContext | None = None
    handlers: dict[int, object] = {}
    cancelled = False
    cleanup_known = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal cancelled
        cancelled = True
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    try:
        owner = OwnerContext.from_environ()
        owner.begin()
        cleanup_known = True
        process = subprocess.Popen(
            (config["node"], str(Path(__file__).with_name("driver.mjs")), "launch"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,
            env=_environment(),
        )
        cleanup_known = False
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, request_stop)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            _canonical(
                {"playwright_module": config["playwright_module"], **payload}
            ).encode()
        )
        process.stdin.close()
        ready, _, _ = select.select((process.stdout,), (), (), _TIMEOUT)
        if not ready:
            raise TimeoutError
        control_line = process.stdout.readline(4097)
        if len(control_line) > 4096:
            raise ValueError
        control = json.loads(control_line.decode("utf-8"))
        if control != {"ready": True}:
            cleanup_known = control == {"cleanup": True}
            raise ValueError
        owner.ready()
        while True:
            try:
                result = process.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancelled:
                    _stop_owned_driver(process)
        cleanup_known = _cleanup_control(process)
        if result != 0 or cancelled:
            raise _fail("configured browser automation operation stopped")
    except Exception as error:
        if process is not None and not cleanup_known:
            _stop_owned_driver(process)
            cleanup_known = _cleanup_control(process)
        if isinstance(error, BackendError):
            raise
        raise _fail("configured browser automation operation is unavailable") from error
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if owner is not None:
            owner.finish(cleanup_known=cleanup_known)


def _probe_engine(value: object) -> tuple[bool, int, str] | None:
    if not isinstance(value, dict) or set(value) != {"ready", "version", "fingerprint"}:
        return None
    ready, version, fingerprint = value["ready"], value["version"], value["fingerprint"]
    if (
        type(ready) is not bool
        or not isinstance(version, str)
        or len(version) > 256
        or not isinstance(fingerprint, str)
        or _DIGEST.fullmatch(fingerprint) is None
    ):
        return None
    numbers = re.findall(r"\d+", version)
    return ready, int(numbers[0]) if numbers else 0, fingerprint


def _binding(
    record: ManagedInstance, engine: object, tool_fingerprint: str
) -> dict[str, object]:
    status = _probe_engine(engine)
    if status is None:
        raise _fail("configured browser automation response is invalid")
    _ready, _rank, engine_fingerprint = status
    return {
        "target_key": "browser-" + _digest([record.token, record.fingerprint])[:24],
        "platform": "browser",
        "engine": record.engine,
        "instance_token": record.token,
        "record_fingerprint": record.fingerprint,
        "engine_fingerprint": engine_fingerprint,
        "tool_fingerprint": tool_fingerprint,
        "page_url": record.page_url,
        "capture_layout": record.capture_layout,
    }


def _revalidate_engine(
    config: Mapping[str, str], binding: Mapping[str, object]
) -> None:
    response = _driver("probe", config)
    engines = response.get("engines")
    if (
        not isinstance(engines, dict)
        or set(engines) != _ENGINES
        or _digest(response) != binding["tool_fingerprint"]
    ):
        raise _fail("selected browser tool identity changed")
    status = _probe_engine(engines.get(str(binding["engine"])))
    if status is None or not status[0] or status[2] != binding["engine_fingerprint"]:
        raise _fail("selected browser engine identity changed")


def discover(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    try:
        config = _browser_config(bindings)
        aliases = _aliases(bindings)
        records = _load_records(config)
        response = _driver("probe", config)
        engines = response.get("engines")
        if not isinstance(engines, dict) or set(engines) != _ENGINES:
            raise _fail("configured browser automation response is invalid")
        tool_fingerprint = _digest(response)
    except BackendError as error:
        emit_inventory(
            request,
            (),
            ("engine_available", "engine_version"),
            (
                {
                    "code": "browser_unavailable",
                    "message": str(error),
                    "remediation": "Install or repair the configured Playwright module and managed instance records without rerunning discovery",
                },
            ),
        )
        return
    candidates: list[dict[str, object]] = []
    for record in records:
        status = _probe_engine(engines.get(record.engine))
        if status is None:
            continue
        engine_ready, version_rank, _fingerprint = status
        bound_aliases = sorted(
            alias for alias, token in aliases.items() if token == record.token
        )
        binding = _binding(record, engines[record.engine], tool_fingerprint)
        configured = record.page_url is not None
        candidate_ready = engine_ready and configured
        if not engine_ready:
            reason, remediation = (
                "engine-unavailable",
                "Install the configured browser engine without rerunning discovery",
            )
        elif not configured:
            reason, remediation = (
                "page-unconfigured",
                "Configure the managed browser instance with its application page",
            )
        else:
            reason = remediation = None
        candidates.append(
            {
                "target_key": binding["target_key"],
                "label": f"{record.engine.capitalize()} managed browser",
                "tags": ["browser", record.engine],
                "roles": ["browser"],
                "aliases": bound_aliases,
                "capabilities": ["run", "logs", "capture"] if candidate_ready else [],
                "ready": candidate_ready,
                "reason": reason,
                "remediation": remediation,
                "preparation": [],
                "rank": [1 if candidate_ready else 0, version_rank],
                "binding": binding,
            }
        )
    emit_inventory(request, candidates, ("engine_available", "engine_version"))


def _context_binding() -> tuple[str, dict[str, object]]:
    context = load_context()
    run_id, binding = context.get("run_id"), context.get("private_binding")
    if (
        not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
        or not isinstance(binding, dict)
    ):
        raise _fail("selected browser context is unavailable")
    required = {
        "target_key",
        "platform",
        "engine",
        "instance_token",
        "record_fingerprint",
        "engine_fingerprint",
        "tool_fingerprint",
        "page_url",
        "capture_layout",
    }
    if set(binding) != required or binding.get("platform") != "browser":
        raise _fail("selected browser context is unavailable")
    if (
        binding.get("engine") not in _ENGINES
        or not isinstance(binding.get("instance_token"), str)
        or _TOKEN.fullmatch(binding["instance_token"]) is None
    ):
        raise _fail("selected browser context is unavailable")
    if any(
        not isinstance(binding.get(name), str)
        or _DIGEST.fullmatch(binding[name]) is None
        for name in ("record_fingerprint", "engine_fingerprint", "tool_fingerprint")
    ):
        raise _fail("selected browser context is unavailable")
    if binding["page_url"] is not None and not isinstance(binding["page_url"], str):
        raise _fail("selected browser context is unavailable")
    if type(binding["capture_layout"]) is not bool:
        raise _fail("selected browser context is unavailable")
    return run_id, binding


def _receipt_path(config: Mapping[str, str], run_id: str) -> Path:
    return (
        Path(config["instances_dir"])
        / "runs"
        / f"{sha256(run_id.encode()).hexdigest()}.json"
    )


def _read_receipt(config: Mapping[str, str], run_id: str) -> dict[str, object]:
    value = _read_private_json(_receipt_path(config, run_id))
    required = {
        "version",
        "run_id",
        "instance_token",
        "record_fingerprint",
        "engine",
        "control_path",
        "control_token",
        "page_url",
        "page_marker",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("version") != 2
    ):
        raise _fail("selected browser receipt is invalid")
    if (
        value.get("run_id") != run_id
        or not isinstance(value.get("instance_token"), str)
        or _TOKEN.fullmatch(value["instance_token"]) is None
    ):
        raise _fail("selected browser receipt is invalid")
    if (
        value.get("engine") not in _ENGINES
        or not isinstance(value.get("record_fingerprint"), str)
        or _DIGEST.fullmatch(value["record_fingerprint"]) is None
    ):
        raise _fail("selected browser receipt is invalid")
    _private_control_socket(value.get("control_path"))
    _private_control_token(value.get("control_token"))
    if (
        not isinstance(value.get("page_url"), str)
        or len(value["page_url"]) > 2048
        or not (
            value["page_url"].startswith("https://")
            or value["page_url"].startswith("http://")
        )
        or any(character.isspace() for character in value["page_url"])
        or not isinstance(value.get("page_marker"), str)
        or not value["page_marker"]
        or len(value["page_marker"]) > 512
    ):
        raise _fail("selected browser receipt is invalid")
    return value


def _selected(
    request: Mapping[str, object], bindings: Mapping[str, object]
) -> tuple[dict[str, str], dict[str, object], dict[str, object]]:
    del request
    config = _browser_config(bindings)
    run_id, binding = _context_binding()
    record = _load_record(config, str(binding["instance_token"]))
    if (
        record.fingerprint != binding["record_fingerprint"]
        or record.engine != binding["engine"]
        or record.page_url != binding["page_url"]
        or record.capture_layout != binding["capture_layout"]
    ):
        raise _fail("selected browser managed instance identity changed")
    receipt = _read_receipt(config, run_id)
    if (
        receipt["instance_token"] != record.token
        or receipt["record_fingerprint"] != record.fingerprint
        or receipt["engine"] != record.engine
        or receipt["page_url"] != record.page_url
    ):
        raise _fail("selected browser managed instance identity changed")
    _revalidate_engine(config, binding)
    if record.page_url is None:
        raise _fail("selected browser instance has no configured page for observation")
    return config, binding, receipt


def _observe(receipt: Mapping[str, object], config: Mapping[str, str]) -> None:
    response = _driver("observe", config, receipt=dict(receipt))
    if response != {
        "page_url": receipt["page_url"],
        "page_marker": receipt["page_marker"],
    }:
        raise _fail("selected browser page changed")


def run(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    del request
    context = load_context()
    config = _browser_config(bindings)
    run_id, binding = _context_binding()
    record = _load_record(config, str(binding["instance_token"]))
    if (
        record.fingerprint != binding["record_fingerprint"]
        or record.engine != binding["engine"]
        or record.page_url != binding["page_url"]
        or record.capture_layout != binding["capture_layout"]
    ):
        raise _fail("selected browser managed instance identity changed")
    _revalidate_engine(config, binding)
    receipt = _receipt_path(config, run_id)
    if receipt.exists():
        raise _fail("selected browser instance has an existing or stale run receipt")
    if record.page_url is None:
        raise _fail("selected browser instance has no configured application page")
    _launch_foreground(
        config,
        context,
        record={
            "token": record.token,
            "engine": record.engine,
            "headless": record.headless,
            "page_url": record.page_url,
            "record_fingerprint": record.fingerprint,
        },
        run_id=run_id,
        receipt_path=str(receipt),
        page_marker=secrets.token_urlsafe(32),
    )


def logs(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    config, _binding, receipt = _selected(request, bindings)
    _observe(receipt, config)
    _foreground("logs", config, receipt=receipt)


def _capture_request() -> tuple[Path, tuple[str, ...]]:
    directory = os.environ.get("MSHIP_CAPTURE_DIR")
    if not directory:
        raise _fail("browser capture output directory is unavailable")
    path = Path(directory)
    if not path.is_absolute() or ".." in path.parts or not path.is_dir():
        raise _fail("browser capture output directory is unavailable")
    kinds = tuple(
        item for item in os.environ.get("MSHIP_CAPTURE_KINDS", "").split(",") if item
    )
    if not kinds or set(kinds) - {"image", "layout"}:
        raise _fail("browser capture kinds are unavailable")
    if os.environ.get("MSHIP_CAPTURE_PLATFORM") not in {None, "", "browser"}:
        raise _fail("browser capture platform does not match selected target")
    return path, kinds


def capture(request: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    config, binding, receipt = _selected(request, bindings)
    directory, kinds = _capture_request()
    if "layout" in kinds and not binding["capture_layout"]:
        raise _fail("selected browser instance does not support layout capture")
    response = _driver(
        "capture",
        config,
        receipt=receipt,
        directory=str(directory),
        kinds=list(kinds),
        capture_layout=bool(binding["capture_layout"]),
    )
    if response != {
        "page_url": receipt["page_url"],
        "page_marker": receipt["page_marker"],
    }:
        raise _fail("selected browser page changed")


def main(argv: Sequence[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] != "discover"):
        print("usage: backend.py [discover]", file=sys.stderr)
        return 2
    try:
        request, bindings = load_request(), load_bindings()
        action = "discover" if len(argv) == 2 else request["operation"]
        if action == "discover":
            discover(request, bindings)
        elif action == "run":
            run(request, bindings)
        elif action == "logs":
            logs(request, bindings)
        elif action == "capture":
            capture(request, bindings)
        else:
            raise _fail("requested browser operation is unavailable")
    except BackendError, ExampleError, SessionError:
        print("browser backend operation is unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
