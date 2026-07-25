"""The command cards must be commands that actually RUN.

A copied remediation that exits with a usage error is worse than no card: the
operator trusts it, runs it, and learns nothing about the real fix.
"""
from mship.webui.actions import _COMMANDS, command_for


def test_every_run_host_add_command_supplies_a_connection():
    """`mship run-host add <role>` alone is rejected — it requires either
    --pair-link or both --url and --token (Greptile, PR #412)."""
    offenders = [
        code for code, (_label, tpl) in _COMMANDS.items()
        if "run-host add" in tpl
        and "--pair-link" not in tpl
        and not ("--url" in tpl and "--token" in tpl)
    ]
    assert offenders == [], (
        f"these cards emit a `run-host add` that the CLI will reject: {offenders}"
    )


def test_store_unreadable_card_names_the_store_and_a_runnable_command():
    card = command_for({
        "status": "warn", "code": "run_hosts_store_unreadable",
        "facts": {"store_path": "/ws/.mothership/run-hosts.yaml", "declared": ["mac"]},
    })
    assert card is not None
    assert "/ws/.mothership/run-hosts.yaml" in card["label"]
    assert "--pair-link" in card["command"]


def test_a_missing_fact_leaves_the_placeholder_rather_than_raising():
    card = command_for({
        "status": "fail", "code": "run_host_unmapped", "facts": {},
    })
    assert card is not None and "{role}" in card["command"]


def test_healthy_and_unknown_codes_yield_no_card():
    assert command_for({"status": "ok", "code": "relay_ok", "facts": {}}) is None
    assert command_for({"status": "fail", "code": "nope", "facts": {}}) is None
