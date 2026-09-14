"""Narrow, target-pinned Android platform helpers.

These helpers deliberately accept a private serial only from a sealed session binding.
They never discover a replacement target, create a process group, or expose adb output.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import os
import re
import select
import stat
import subprocess
import threading
import time
from typing import Sequence

from mship.core.session_inputs import SessionError
from mship.util.shell import tool_runtime_environment

_MAX_OUTPUT = 256 * 1024
_TIMEOUT = 20.0
_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")


def _failure(code: str, message: str) -> SessionError:
    return SessionError(code, message)


def _run(
    argv: Sequence[str],
    *,
    cancel: threading.Event | None = None,
    timeout: float = _TIMEOUT,
    stdin: bytes | None = None,
    accepted_exit_codes: tuple[int, ...] = (0,),
) -> bytes:
    """Run one bounded child in the inherited supervisor process group."""
    if cancel is not None and cancel.is_set():
        raise _failure("cancelled", "Android operation was cancelled")
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=tool_runtime_environment(),
            start_new_session=False,
        )
    except (OSError, ValueError) as error:
        raise _failure("unavailable", "Declared Android tool is unavailable") from error
    started = time.monotonic()
    output = bytearray()
    errors = bytearray()
    try:
        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin)
            proc.stdin.close()
        assert proc.stdout is not None and proc.stderr is not None
        streams = {proc.stdout: output, proc.stderr: errors}
        while streams or proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                raise _failure("cancelled", "Android operation was cancelled")
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                proc.terminate()
                raise _failure("unavailable", "Android tool did not respond in time")
            ready, _, _ = select.select(list(streams), [], [], min(remaining, 0.1))
            for stream in ready:
                chunk = os.read(stream.fileno(), 8192)
                if not chunk:
                    del streams[stream]
                    continue
                streams[stream].extend(chunk)
                if len(output) + len(errors) > _MAX_OUTPUT:
                    proc.terminate()
                    raise _failure(
                        "unavailable", "Android tool returned excessive output"
                    )
        code = proc.wait(timeout=1)
        # A declared no-match exit is valid only without a tool/transport error.
        if code not in accepted_exit_codes or (code != 0 and errors):
            raise _failure("unhealthy", "Android target operation failed")
        return bytes(output)
    except subprocess.TimeoutExpired as error:
        proc.terminate()
        raise _failure("unknown", "Android child cleanup is incomplete") from error
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def _text(output: bytes) -> str:
    try:
        return output.decode("utf-8", "strict").strip()
    except UnicodeDecodeError as error:
        raise _failure("unhealthy", "Android tool returned invalid text") from error


def _adb(
    adb: str,
    serial: str,
    *args: str,
    cancel: threading.Event | None = None,
    timeout: float = _TIMEOUT,
    accepted_exit_codes: tuple[int, ...] = (0,),
) -> bytes:
    if (
        not isinstance(adb, str)
        or not os.path.isabs(adb)
        or not _SERIAL.fullmatch(serial)
    ):
        raise _failure("invalid", "Invalid declared Android binding")
    return _run(
        (adb, "-s", serial, *args),
        cancel=cancel,
        timeout=timeout,
        accepted_exit_codes=accepted_exit_codes,
    )


def _device_line(
    adb: str, serial: str, cancel: threading.Event | None
) -> tuple[str, str]:
    if (
        not isinstance(adb, str)
        or not os.path.isabs(adb)
        or not _SERIAL.fullmatch(serial)
    ):
        raise _failure("invalid", "Invalid declared Android binding")
    lines = _text(_run((adb, "devices", "-l"), cancel=cancel)).splitlines()
    for line in lines[1:]:
        fields = line.split()
        if fields and fields[0] == serial:
            return (fields[1] if len(fields) > 1 else "", line)
    raise _failure("unavailable", "Recorded Android target is unavailable")


def probe_android(adb: str, serial: str, transport: str) -> dict[str, object]:
    """Return private exact identity for one authorized adb target.

    ``transport`` is a sealed binding value: ``emulator`` or ``usb``.  A target
    in any other adb state, or with a missing required identity, is not usable.
    """
    if transport not in {"emulator", "usb"}:
        raise _failure("invalid", "Invalid declared Android transport")
    state, line = _device_line(adb, serial, None)
    if state != "device":
        raise _failure("unavailable", "Recorded Android target is not authorized")
    if transport == "usb" and "usb:" not in line:
        raise _failure(
            "identity-unknown", "Recorded Android USB identity is unavailable"
        )
    if transport == "emulator" and not (
        serial.startswith("emulator-") or "product:" in line
    ):
        raise _failure(
            "identity-unknown", "Recorded Android emulator identity is unavailable"
        )
    properties = {}
    for key in (
        "ro.serialno",
        "ro.build.fingerprint",
        "ro.product.device",
        "ro.build.version.sdk",
        "ro.kernel.qemu.avd_name",
    ):
        value = _text(_adb(adb, serial, "shell", "getprop", key))
        properties[key] = value or None
    required = (
        "ro.serialno",
        "ro.build.fingerprint",
        "ro.product.device",
        "ro.build.version.sdk",
    )
    if any(properties[key] is None for key in required):
        raise _failure(
            "identity-unknown", "Recorded Android target identity is unavailable"
        )
    try:
        api_level = int(str(properties["ro.build.version.sdk"]))
    except ValueError as error:
        raise _failure(
            "identity-unknown", "Recorded Android API identity is unavailable"
        ) from error
    if api_level <= 0:
        raise _failure(
            "identity-unknown", "Recorded Android API identity is unavailable"
        )
    avd_name = properties["ro.kernel.qemu.avd_name"]
    if transport == "emulator" and avd_name is None:
        raise _failure(
            "identity-unknown", "Recorded Android emulator identity is unavailable"
        )
    usb_match = re.search(r"(?:^|\s)usb:([^\s]+)", line)
    return {
        "serial": serial,
        "transport": transport,
        "ro_serialno": properties["ro.serialno"],
        "build_fingerprint": properties["ro.build.fingerprint"],
        "product_device": properties["ro.product.device"],
        "api_level": api_level,
        "avd_name": avd_name if transport == "emulator" else None,
        "usb_transport": usb_match.group(1)
        if transport == "usb" and usb_match
        else None,
    }


def foreground_android(adb: str, serial: str) -> str | None:
    """Return only the foreground package for this exact serial, if observable."""
    output = _text(_adb(adb, serial, "shell", "dumpsys", "window", "windows"))
    match = re.search(r"mCurrentFocus=.*?\s([A-Za-z][\w.]+)/(?:[\w.$]+)", output)
    if match and _PACKAGE.fullmatch(match.group(1)):
        return match.group(1)
    output = _text(_adb(adb, serial, "shell", "dumpsys", "activity", "activities"))
    match = re.search(r"mResumedActivity:.*?\s([A-Za-z][\w.]+)/(?:[\w.$]+)", output)
    return match.group(1) if match and _PACKAGE.fullmatch(match.group(1)) else None


def _stream_adb_file(
    adb: str,
    serial: str,
    args: Sequence[str],
    destination: Path,
    cancel: threading.Event,
    *,
    timeout: float = 30.0,
) -> None:
    """Stream a binary artifact to its granted file without the text-output cap."""
    if not os.path.isabs(adb) or not _SERIAL.fullmatch(serial):
        raise _failure("invalid", "Invalid declared Android binding")
    try:
        proc = subprocess.Popen(
            (adb, "-s", serial, *args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=tool_runtime_environment(),
            start_new_session=False,
        )
    except OSError as error:
        raise _failure("unavailable", "Declared Android tool is unavailable") from error
    started, total = time.monotonic(), 0
    try:
        with destination.open("xb") as output:
            assert proc.stdout is not None
            while True:
                if cancel.is_set():
                    proc.terminate()
                    raise _failure("cancelled", "Android operation was cancelled")
                if time.monotonic() - started >= timeout:
                    proc.terminate()
                    raise _failure(
                        "unavailable", "Android capture did not respond in time"
                    )
                ready, _, _ = select.select([proc.stdout], [], [], 0.1)
                if ready:
                    chunk = os.read(proc.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        if proc.poll() is not None:
                            break
                        continue
                    total += len(chunk)
                    if total > 32 * 1024 * 1024:
                        proc.terminate()
                        raise _failure(
                            "unhealthy", "Android capture artifact is too large"
                        )
                    output.write(chunk)
                elif proc.poll() is not None:
                    break
        if proc.wait(timeout=1) != 0 or total < 8:
            raise _failure("unhealthy", "Android image capture was empty")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1)


def capture_android(
    adb: str,
    serial: str,
    directory: Path,
    kinds: tuple[str, ...],
    cancel: threading.Event,
) -> None:
    """Capture recognized Android files into a server-created private directory."""
    if not isinstance(directory, Path) or not directory.is_dir() or not kinds:
        raise _failure("invalid", "Invalid Android capture capability")
    if any(kind not in {"image", "layout"} for kind in kinds) or len(set(kinds)) != len(
        kinds
    ):
        raise _failure("invalid", "Invalid Android capture kinds")
    created: list[Path] = []
    try:
        if "image" in kinds:
            image_path = directory / "screen.png"
            _stream_adb_file(
                adb, serial, ("exec-out", "screencap", "-p"), image_path, cancel
            )
            with image_path.open("rb") as image:
                if image.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise _failure("unhealthy", "Android image capture was empty")
            created.append(image_path)
        if "layout" in kinds:
            layout = _adb(
                adb,
                serial,
                "exec-out",
                "uiautomator",
                "dump",
                "/dev/tty",
                cancel=cancel,
                timeout=30,
            )
            if not layout.strip() or b"<" not in layout:
                raise _failure("unhealthy", "Android layout capture was empty")
            layout_path = directory / "layout.xml"
            layout_path.write_bytes(layout)
            created.append(layout_path)
    except OSError, SessionError:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def file_digest(path: Path, *, size: int) -> str:
    """Validate a staged regular file without following a replacement link."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise _failure("unknown", "Staged Android artifact is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
            raise _failure("unknown", "Staged Android artifact identity changed")
        digest = sha256()
        while chunk := os.read(descriptor, 128 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        raise _failure("unknown", "Staged Android artifact is unavailable") from error
    finally:
        os.close(descriptor)
