"""Race-safe launchd stderr-capture rollover (#475 review).

launchd has no log rotation: with `KeepAlive.SuccessfulExit=false` a failing
daemon is relaunched every `ThrottleInterval` and appends to the SAME
`StandardErrorPath` forever. Nothing else prunes it.

Deliberately stdlib-only and import-light so it can run as the very first
thing in `mshipd`'s entrypoint — the crash loop this exists for is often a
BROKEN IMPORT, so anything it depends on is something that might not import.
Honest limit: if the failure is importing this module (or the package) itself,
no in-process rollover can run — which is why `mship daemon logs`/`status`
also perform it when an operator inspects a never-starting daemon.
"""
from __future__ import annotations

import os

from pathlib import Path

# Retire an active capture larger than this. launchd opens StandardErrorPath
# with O_APPEND for each job process, so an atomic rename keeps an existing
# writer on the retired inode while later launches create a fresh active path.
LAUNCHD_CAPTURE_MAX_BYTES = 5 * 1024 * 1024


def rotate_launchd_captures(
    log_dir: Path, max_bytes: int = LAUNCHD_CAPTURE_MAX_BYTES
) -> list[str]:
    """Race-safely roll over `launchd.*.log` captures; return changed names.

    An oversized active path is atomically renamed to `.1`, never rewritten,
    so an O_APPEND writer keeps every concurrent byte. If a later launch has
    recreated the active path below the cap, the previous `.1` can no longer
    have that launchd job's writer and is safely compacted in place.
    """
    changed: list[str] = []
    try:
        active_captures = sorted(Path(log_dir).glob("launchd.*.log"))
    except OSError:
        return changed
    for path in active_captures:
        try:
            stat = path.stat()
            retired = path.with_name(f"{path.name}.1")
            if stat.st_size > max_bytes:
                os.replace(path, retired)
                changed.append(path.name)
                continue

            # The active path can only be recreated by a later launch, after
            # the process whose O_APPEND fd followed the prior rename exited.
            retired_stat = retired.stat()
            if retired_stat.st_size <= max_bytes:
                continue
            with open(retired, "r+b") as fh:
                fh.seek(-max_bytes, os.SEEK_END)
                tail = fh.read(max_bytes)
                fh.seek(0)
                fh.write(tail)
                fh.truncate()
            os.utime(
                retired,
                ns=(retired_stat.st_atime_ns, retired_stat.st_mtime_ns),
            )
            changed.append(retired.name)
        except OSError:
            continue
    return changed
