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


def test_every_command_is_shell_safe_when_pasted():
    """Greptile, PR #412: an unquoted `<role>` placeholder is INPUT REDIRECTION,
    so pasting the card yields `role: No such file or directory` — and
    `--remote=<role>` / `export VAR=<serve url>` are outright syntax errors.

    Checked by actually running each rendered command through `bash -n` (parse,
    don't execute), which catches the whole class rather than the one instance
    that was reported.
    """
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:                      # pragma: no cover - CI always has bash
        import pytest
        pytest.skip("bash unavailable")

    facts = {"role": "mac-studio", "store_path": "/ws/.mothership/run-hosts.yaml"}
    broken = []
    for code in sorted(_COMMANDS):
        card = command_for({"status": "fail", "code": code, "facts": facts})
        result = subprocess.run(
            [bash, "-n", "-c", card["command"]],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            broken.append((code, card["command"], result.stderr.strip().splitlines()[:1]))
    assert broken == [], f"commands that a shell cannot parse: {broken}"


def test_placeholders_use_no_angle_brackets():
    """The convention that keeps the above true: bare UPPERCASE placeholders.
    Angle brackets are shell metacharacters, so they must not appear at all —
    even quoted, where they are safe but inconsistent."""
    offenders = [
        code for code, (label, tpl) in _COMMANDS.items()
        if "<" in tpl or ">" in tpl or "<" in label or ">" in label
    ]
    assert offenders == [], f"cards using angle-bracket placeholders: {offenders}"


def test_every_card_either_runs_unchanged_or_is_flagged_needs_input():
    """The honest invariant (Greptile, PR #412 round 3).

    "Every card runs unchanged" is NOT achievable: a pair link is a secret minted
    by `mship pair` on another machine and a bearer belongs to a serve this host
    may never have contacted, so no console can pre-fill them. What IS achievable
    is that a card never *pretends* to be turnkey — so each one either contains no
    placeholder, or sets needs_input for the UI to label.
    """
    from mship.webui.actions import _PLACEHOLDERS

    facts = {"role": "mac-studio", "store_path": "/ws/.mothership/run-hosts.yaml"}
    for code in sorted(_COMMANDS):
        card = command_for({"status": "fail", "code": code, "facts": facts})
        has_placeholder = any(tok in card["command"] for tok in _PLACEHOLDERS)
        assert card["needs_input"] == has_placeholder, (
            f"{code}: needs_input={card['needs_input']} but "
            f"placeholder-present={has_placeholder} in {card['command']!r}"
        )


def test_unreadable_store_fills_a_real_role_from_the_declared_list():
    """Reading the store is what failed, so there is no `role` fact — but the
    DECLARED roles are known, so the card names one instead of a literal ROLE."""
    card = command_for({
        "status": "warn", "code": "run_hosts_store_unreadable",
        "facts": {"declared": ["mac-studio", "linux-box"],
                  "store_path": "/ws/.mothership/run-hosts.yaml"},
    })
    assert "mac-studio" in card["command"]
    assert "ROLE" not in card["command"]
    assert card["needs_input"] is True          # PAIR_LINK still operator-supplied


def test_turnkey_cards_are_not_flagged():
    for code in ("relay_not_running", "relay_auth_failed", "probe_skipped"):
        card = command_for({"status": "fail", "code": code, "facts": {}})
        assert card["needs_input"] is False, f"{code} needs no input but is flagged"
