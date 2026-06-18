"""Configure git/gh auth from an environment token for cloud sessions (MOS-187).

The token is passed to git only via a subprocess env var read by a github.com-
scoped credential helper — never written to disk, never placed in argv.
"""
from __future__ import annotations

import os

# Credential helper: on a `get` request, emit username/password reading the
# token from the env var git inherits. Single-quote-safe (uses double quotes
# internally) so callers can shlex.quote the whole `-c` value.
_CRED_HELPER = (
    '!f() { test "$1" = get && '
    'printf "username=x-access-token\\npassword=%s\\n" "$MSHIP_GH_TOKEN"; }; f'
)
_TOKEN_ENV_VAR = "MSHIP_GH_TOKEN"


def resolve_token(explicit: str | None) -> str | None:
    """Token precedence: explicit (--token) > GH_TOKEN > GITHUB_TOKEN. Blank → None."""
    for candidate in (explicit, os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def git_cred_args(token: str) -> tuple[list[str], dict[str, str]]:
    """Return (`-c` args for a github.com-scoped credential helper, env carrying
    the token). Splice the args into a `git` invocation and pass the env to it.
    The token appears only in the env, never in the args."""
    keyval = f"credential.https://github.com.helper={_CRED_HELPER}"
    return ["-c", keyval], {_TOKEN_ENV_VAR: token}
