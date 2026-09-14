"""Shared Typer support for the optional-value ``--remote`` flag."""

from __future__ import annotations

from typer.core import TyperCommand


class RemoteFlagCommand(TyperCommand):
    """Accept bare ``--remote`` as auto-role resolution.

    Typer cannot directly represent Click's optional-value option recipe.  The
    command parser therefore rewrites only an exact bare flag to the empty
    value consumed by the normal ``Optional[str]`` command options.
    """

    def parse_args(self, ctx, args):
        args = ["--remote=" if arg == "--remote" else arg for arg in args]
        return super().parse_args(ctx, args)
