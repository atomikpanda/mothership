"""Line-prefixed, thread-safe printer for `mship run` service output.

Constructed once per `mship run` invocation. Drain threads (one per
service's stdout and stderr PIPE) call `write()` with each line as it
arrives. The printer pads the repo name to a fixed width, applies an
ANSI color when attached to a TTY, acquires a lock, and writes to
stdout so that lines from parallel services never tear.

This module intentionally has no Rich dependency: it emits raw ANSI
escape codes so the output is deterministic and easy to test.
"""
from __future__ import annotations

import sys
import threading


# Fixed palette, cycled in sorted-repo order for deterministic coloring.
_PALETTE = ("36", "32", "33", "35", "34", "31")  # cyan, green, yellow, magenta, blue, red


def _assign_colors(repos: list[str]) -> dict[str, str]:
    """Return {repo: ansi_color_code} cycling _PALETTE in sorted order."""
    return {
        repo: _PALETTE[i % len(_PALETTE)]
        for i, repo in enumerate(sorted(repos))
    }


def _colorize(text: str, ansi_code: str) -> str:
    """Wrap `text` in an ANSI SGR escape sequence."""
    return f"\x1b[{ansi_code}m{text}\x1b[0m"


class StreamPrinter:
    """Thread-safe line-prefixed printer for multi-service output."""

    def __init__(self, repos: list[str], use_color: bool | None = None) -> None:
        self._width = max((len(r) for r in repos), default=0)
        self._colors = _assign_colors(repos)
        self._use_color = (
            sys.stdout.isatty() if use_color is None else use_color
        )
        self._lock = threading.Lock()

    def write(self, repo: str, line: str) -> None:
        # Normalise trailing whitespace: strip any \r\n combinations and
        # re-add exactly one \n so the output is consistent regardless of
        # what the child process emits.
        content = line.rstrip("\r\n")
        prefix = f"{repo:<{self._width}}  | "
        if self._use_color and repo in self._colors:
            prefix = _colorize(prefix, self._colors[repo])
        with self._lock:
            sys.stdout.write(f"{prefix}{content}\n")
            sys.stdout.flush()
