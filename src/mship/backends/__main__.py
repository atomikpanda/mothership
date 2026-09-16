"""Execute an installed built-in run-target backend.

Usage: ``python -I -m mship.backends <builtin> [discover]``.

The builtin name is resolved only through the fixed core catalog. Backend operations
without ``discover`` receive their sealed request through private input files rather
than command-line arguments.
"""

from __future__ import annotations

import runpy
import sys
from collections.abc import Sequence

from mship.core.run_target.builtins import BUILTIN_BACKENDS


def _usage() -> int:
    print("usage: python -I -m mship.backends <builtin> [discover]", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Run the catalog-selected backend with a sanitized backend argv."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if not arguments or len(arguments) > 2:
        return _usage()
    descriptor = BUILTIN_BACKENDS.get(arguments[0])
    if descriptor is None or (len(arguments) == 2 and arguments[1] != "discover"):
        return _usage()

    previous_argv = sys.argv
    try:
        sys.argv = [descriptor.module, *arguments[1:]]
        runpy.run_module(descriptor.module, run_name="__main__", alter_sys=False)
    except SystemExit as exit_code:
        if exit_code.code is None:
            return 0
        if isinstance(exit_code.code, int):
            return exit_code.code
        return 1
    finally:
        sys.argv = previous_argv
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
