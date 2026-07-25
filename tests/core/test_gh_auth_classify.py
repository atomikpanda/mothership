from mship.core.gh_auth import classify_gh_auth


def test_relay_attach_wins():
    assert classify_gh_auth(
        app_configured=True, relay_url="https://r", run_token="rt",
        explicit_token="ghp_x", broker_url="https://b",
    ) == "relay_attach"


def test_app_beats_env_token_and_broker():
    assert classify_gh_auth(
        app_configured=True, relay_url=None, run_token=None,
        explicit_token="ghp_x", broker_url="https://b",
    ) == "app"


def test_env_token_beats_broker():
    assert classify_gh_auth(
        app_configured=False, relay_url=None, run_token=None,
        explicit_token="ghp_x", broker_url="https://b",
    ) == "env_token"


def test_broker_when_only_broker():
    assert classify_gh_auth(
        app_configured=False, relay_url=None, run_token=None,
        explicit_token=None, broker_url="https://b",
    ) == "broker"


def test_none_when_nothing_configured():
    assert classify_gh_auth(
        app_configured=False, relay_url=None, run_token=None,
        explicit_token=None, broker_url=None,
    ) == "none"


def test_half_configured_relay_is_not_relay_attach():
    # relay-attach needs BOTH url and run token (mirrors relay_flags_error).
    assert classify_gh_auth(
        app_configured=False, relay_url="https://r", run_token=None,
        explicit_token=None, broker_url=None,
    ) == "none"


def test_blank_strings_do_not_count_as_configured():
    assert classify_gh_auth(
        app_configured=False, relay_url=None, run_token=None,
        explicit_token="   ", broker_url="",
    ) == "none"
