"""launchd stderr-capture trimming (#471-adjacent, #475 review).

launchd has no log rotation: with `KeepAlive.SuccessfulExit=false` a failing
daemon is relaunched every `ThrottleInterval` and appends to the SAME
`StandardErrorPath` forever. Nothing else prunes it.

Deliberately stdlib-only and import-light so it can run as the very first
thing in `mshipd`'s entrypoint — the crash loop this exists for is often a
BROKEN IMPORT, so anything it depends on is something that might not import.
Honest limit: if the failure is importing this module (or the package) itself,
no in-process trim can run — which is why `mship daemon logs`/`status` trim
too, giving a never-starting daemon's capture a bound whenever an operator
looks at it.
"""
from __future__ import annotations
import os

from pathlib import Path

# Compact a capture larger than this, preserving its newest bytes. In place,
# never renamed: launchd holds the fd open across relaunches, so a renamed file
# keeps growing invisibly.
LAUNCHD_CAPTURE_MAX_BYTES = 5 * 1024 * 1024


def trim_launchd_captures(log_dir: Path, max_bytes: int = LAUNCHD_CAPTURE_MAX_BYTES) -> list[str]:
    """Compact oversized `launchd.*.log` captures to their newest bytes.
    Returns the names trimmed. Never raises: this runs on the crash path, where
    failing loudly would replace one problem with a worse one."""
    trimmed: list[str] = []
    try:
        candidates = sorted(Path(log_dir).glob("launchd.*.log"))
    except OSError:
        return trimmed
    for path in candidates:
        try:
            stat = path.stat()
            if stat.st_size <= max_bytes:
                continue
            with open(path, "r+b") as fh:
                fh.seek(-max_bytes, os.SEEK_END)
                tail = fh.read(max_bytes)
                fh.seek(0)
                fh.write(tail)
                fh.truncate()
            # Contents are compacted, not newly produced. Preserve their
            # timestamp so logs_tail's cross-file chronology remains truthful.
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            trimmed.append(path.name)
        except OSError:
            continue
    return trimmed
