from __future__ import annotations
import urllib.parse


def build_pair_link(*, url: str, token: str, workspace: str) -> str:
    """Build a groundcontrol://add? deep-link with percent-encoded params.

    Uses quote (not quote_plus) so spaces become %20, not +, and + becomes
    %2B — this ensures round-trip fidelity for tokens containing spaces,
    slashes, plus signs, and equals signs.
    """
    query = urllib.parse.urlencode(
        {"url": url, "token": token, "workspace": workspace},
        quote_via=urllib.parse.quote,
    )
    return f"groundcontrol://add?{query}"


def build_relay_account_link(*, relay: str, token: str) -> str:
    """Build the `groundcontrol://add-relay?…` deep-link the phone scans once.

    Pairs the app with a whole RELAY rather than one workspace: the app derives
    `https://enroll.<relay>` and reads the host directory from there with the
    fleet token, so a freshly provisioned host needs no address typed (AC1).
    Same `quote` encoding as `build_pair_link` — the token round-trips exactly.
    """
    query = urllib.parse.urlencode(
        {"relay": relay, "token": token},
        quote_via=urllib.parse.quote,
    )
    return f"groundcontrol://add-relay?{query}"


def parse_pair_link(link: str) -> dict:
    """Parse a groundcontrol://add? deep-link, returning {url, token, workspace}.

    Raises ValueError on wrong scheme or missing required keys.
    """
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme != "groundcontrol":
        raise ValueError(f"Expected scheme 'groundcontrol', got {parsed.scheme!r}")
    # netloc is 'add' for groundcontrol://add?...
    host_path = (parsed.netloc + parsed.path).strip("/")
    if host_path != "add":
        raise ValueError(f"Expected host 'add', got {host_path!r}")
    # parse_qs with keep_blank_values; use urllib.parse.unquote (not unquote_plus)
    # so %20 -> space but + is left as + (we encoded with quote, not quote_plus)
    raw_query = parsed.query
    # parse_qsl returns (key, value) pairs; values are already unquoted by
    # parse_qsl using unquote_plus by default, which would turn + -> space.
    # We need unquote (not unquote_plus), so we parse manually.
    params: dict[str, str] = {}
    for part in raw_query.split("&"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        params[urllib.parse.unquote(k)] = urllib.parse.unquote(v)

    required = {"url", "token", "workspace"}
    missing = required - params.keys()
    if missing:
        raise ValueError(f"Missing required keys: {missing}")
    return {k: params[k] for k in required}


def parse_relay_account_link(link: str) -> dict[str, str]:
    """Parse exactly the existing relay-account pairing link, never a direct link."""
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme != "groundcontrol" or (parsed.netloc + parsed.path).strip("/") != "add-relay":
        raise ValueError("expected a groundcontrol://add-relay link")
    if parsed.fragment:
        raise ValueError("relay account link must not contain a fragment")
    pairs = []
    for part in parsed.query.split("&"):
        if not part or "=" not in part:
            raise ValueError("relay account link has malformed query fields")
        key, _, value = part.partition("=")
        pairs.append((urllib.parse.unquote(key), urllib.parse.unquote(value)))
    if {key for key, _ in pairs} != {"relay", "token"} or len(pairs) != 2:
        raise ValueError("relay account link must contain exactly relay and token")
    values = dict(pairs)
    if not values["relay"] or not values["token"]:
        raise ValueError("relay account link has empty fields")
    return values
