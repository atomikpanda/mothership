from mship.core.gh_auth import classify_gh_auth


def test_relay_attach_wins():
    assert classify_gh_auth(
        relay_url="https://r", run_token="rt",
        explicit_token="ghp_x", broker_url="https://b",
    ) == "relay_attach"


def test_an_app_is_not_a_client_model():
    """App creds only back serve's `GET /gh-token` for CALLERS; this host's own
    ops still use the env token. Reporting "app" here would name a model that
    bootstrap/finish never use (Greptile, PR #411)."""
    from mship.core.gh_auth import GH_AUTH_MODELS

    assert "app" not in GH_AUTH_MODELS


def test_precedence_matches_resolve_tokens_documented_order():
    """The classifier is the reporting mirror of `resolve_token`: an env token
    outranks the broker, and nothing outranks relay-attach."""
    from mship.core.gh_auth import GH_AUTH_MODELS

    assert GH_AUTH_MODELS.index("relay_attach") < GH_AUTH_MODELS.index("env_token")
    assert GH_AUTH_MODELS.index("env_token") < GH_AUTH_MODELS.index("broker")


def test_env_token_beats_broker():
    assert classify_gh_auth(
        relay_url=None, run_token=None,
        explicit_token="ghp_x", broker_url="https://b",
    ) == "env_token"


def test_broker_when_only_broker():
    assert classify_gh_auth(
        relay_url=None, run_token=None,
        explicit_token=None, broker_url="https://b",
    ) == "broker"


def test_none_when_nothing_configured():
    assert classify_gh_auth(
        relay_url=None, run_token=None,
        explicit_token=None, broker_url=None,
    ) == "none"


def test_half_configured_relay_is_not_relay_attach():
    # relay-attach needs BOTH url and run token (mirrors relay_flags_error).
    assert classify_gh_auth(
        relay_url="https://r", run_token=None,
        explicit_token=None, broker_url=None,
    ) == "none"


def test_blank_strings_do_not_count_as_configured():
    assert classify_gh_auth(
        relay_url=None, run_token=None,
        explicit_token="   ", broker_url="",
    ) == "none"
