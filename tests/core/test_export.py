"""Tests for mship.core.export — bundle assembly + --redacted patterns (MOS-102)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mship.core import export as export_mod
from mship.core.config import ConfigLoader
from mship.core.export import (
    BUILTIN_PATTERNS,
    ExportBundle,
    RedactionPattern,
    build_export_bundle,
    collect_repo_diff,
    discover_plan_path,
    export_task,
    load_user_patterns,
    redact_diff_text,
    redact_text,
)
from mship.core.log import LogManager
from mship.core.spec_draft import new_spec
from mship.core.spec_store import SpecStore
from mship.core.state import Task


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _sh(*args, cwd):
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, env=_GIT_ENV)


def _make_task(**overrides) -> Task:
    defaults = dict(
        slug="add-labels",
        description="Add labels",
        phase="dev",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        affected_repos=["shared"],
        branch="feat/add-labels",
        worktrees={},
        base_branch="main",
    )
    defaults.update(overrides)
    return Task(**defaults)


# --------------------------------------------------------------------------
# Built-in redaction patterns — one dedicated case per documented pattern (AC4)
# --------------------------------------------------------------------------

def test_stripe_live_key_redacted():
    text, warnings = redact_text("key=sk_live_ABC123xyz789 end", BUILTIN_PATTERNS)
    assert "sk_live_ABC123xyz789" not in text
    assert "<REDACTED:stripe_live_key>" in text
    assert warnings == []


def test_stripe_test_key_redacted():
    text, _ = redact_text("key=sk_test_ABC123xyz789 end", BUILTIN_PATTERNS)
    assert "sk_test_ABC123xyz789" not in text
    assert "<REDACTED:stripe_test_key>" in text


def test_github_token_redacted():
    token = "ghp_" + "a" * 36
    text, _ = redact_text(f"export GH_TOKEN={token}", BUILTIN_PATTERNS)
    assert token not in text
    assert "<REDACTED:github_token>" in text


@pytest.mark.parametrize("prefix", ["ghp", "gho", "ghu", "ghs"])
def test_github_token_all_prefixes_redacted(prefix):
    token = f"{prefix}_" + "b" * 36
    text, _ = redact_text(token, BUILTIN_PATTERNS)
    assert token not in text
    assert "<REDACTED:github_token>" in text


def test_aws_access_key_id_redacted():
    key = "AKIA" + "A" * 16
    text, _ = redact_text(f"aws_access_key_id = {key}", BUILTIN_PATTERNS)
    assert key not in text
    assert "<REDACTED:aws_access_key_id>" in text


def test_aws_secret_access_key_redacted_value_only():
    secret = "abcdEFGH1234abcdEFGH1234abcdEFGH1234abcd"[:40]  # 40 chars
    assert len(secret) == 40
    line = f"aws_secret_access_key: {secret}"
    text, _ = redact_text(line, BUILTIN_PATTERNS)
    assert secret not in text
    assert "<REDACTED:aws_secret_access_key>" in text
    # prefix survives — only the value is replaced (value-only per spec).
    assert text.startswith("aws_secret_access_key")


def test_aws_secret_access_key_redacted_without_nearby_access_key_id():
    """No proximity requirement to an AKIA... match (spec q4)."""
    secret = "abcdEFGH1234abcdEFGH1234abcdEFGH1234abcd"[:40]  # 40 chars
    text, _ = redact_text(f"aws_secret_access_key={secret}", BUILTIN_PATTERNS)
    assert "<REDACTED:aws_secret_access_key>" in text


def test_private_key_block_redacted_whole_block():
    block = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBOgIBAAJBAKj34GkxFhD91aM3wIVA\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )
    text, _ = redact_text(block, BUILTIN_PATTERNS)
    assert "MIIBOgIBAAJBAKj34GkxFhD91aM3wIVA" not in text
    assert "<REDACTED:private_key>" in text
    assert "before" in text and "after" in text


def test_bearer_token_redacted_keeps_scheme_word():
    text, _ = redact_text("Authorization: Bearer abc123.def-456_ghi", BUILTIN_PATTERNS)
    assert "abc123.def-456_ghi" not in text
    assert "Bearer <REDACTED:bearer_token>" in text


@pytest.mark.parametrize("key", ["API_KEY", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL"])
def test_env_secret_redacted_keeps_key_name(key):
    text, _ = redact_text(f"{key}=supersecretvalue123", BUILTIN_PATTERNS)
    assert "supersecretvalue123" not in text
    assert f"{key}=<REDACTED:env_secret>" in text


def test_env_secret_case_insensitive_key():
    text, _ = redact_text("api_key=lowercasevalue", BUILTIN_PATTERNS)
    assert "<REDACTED:env_secret>" in text


def test_unredacted_text_untouched_by_redact_text_with_no_patterns():
    text, warnings = redact_text("nothing secret here", [])
    assert text == "nothing secret here"
    assert warnings == []


def test_plain_text_with_no_secret_shapes_passes_through():
    original = "ordinary journal note, no secrets"
    text, _ = redact_text(original, BUILTIN_PATTERNS)
    assert text == original


# --------------------------------------------------------------------------
# redact_diff_text — binary chunks skipped (AC6)
# --------------------------------------------------------------------------

def test_redact_diff_text_skips_binary_chunk_redacts_text_chunk():
    diff_text = (
        "diff --git a/config.env b/config.env\n"
        "index 111..222 100644\n"
        "--- a/config.env\n"
        "+++ b/config.env\n"
        "@@ -1 +1 @@\n"
        "+API_KEY=abcdef123456\n"
        "diff --git a/blob.bin b/blob.bin\n"
        "index 333..444 100644\n"
        "Binary files a/blob.bin and b/blob.bin differ\n"
    )
    result, warnings = redact_diff_text(diff_text, BUILTIN_PATTERNS)
    assert "API_KEY=<REDACTED:env_secret>" in result
    assert "abcdef123456" not in result
    # Binary chunk copied through completely unmodified.
    assert "Binary files a/blob.bin and b/blob.bin differ" in result
    assert warnings == []


def test_chunk_is_binary_detects_markers():
    assert export_mod._chunk_is_binary("Binary files a/x and b/y differ\n")
    assert export_mod._chunk_is_binary("GIT binary patch\nliteral 10\n")
    assert not export_mod._chunk_is_binary("+some text change\n")


# --------------------------------------------------------------------------
# User-configured patterns (optional; MOS-102 AC7)
# --------------------------------------------------------------------------

def test_load_user_patterns_absent_sources_returns_empty(tmp_path, workspace):
    config = ConfigLoader.load(workspace / "mothership.yaml")
    loaded = load_user_patterns(config, home_dir=tmp_path / "no-home-here")
    assert loaded.patterns == []
    assert loaded.warnings == []


def test_load_user_patterns_from_home_file(tmp_path, workspace):
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "mship"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "redact.patterns").write_text(
        "\n".join(["ACME-[0-9]+", "", "  ", "#comment line", "[unterminated("])
    )
    config = ConfigLoader.load(workspace / "mothership.yaml")
    loaded = load_user_patterns(config, home_dir=home)
    kinds = [p.kind for p in loaded.patterns]
    assert kinds == ["custom"]
    assert not any(p.builtin for p in loaded.patterns)
    assert any("unterminated" in w or "[unterminated(" in w for w in loaded.warnings)

    text, _ = redact_text("case ACME-1234 opened", loaded.patterns)
    assert "<REDACTED:custom>" in text
    assert "ACME-1234" not in text


def test_load_user_patterns_from_config_named_and_bare(tmp_path, workspace):
    cfg_path = workspace / "mothership.yaml"
    cfg_path.write_text(
        cfg_path.read_text()
        + "\nredact:\n  patterns:\n    - name: client_id\n      pattern: 'CLIENT-[0-9]+'\n"
        "    - 'BARE-[a-z]+'\n"
    )
    config = ConfigLoader.load(cfg_path)
    loaded = load_user_patterns(config, home_dir=tmp_path / "no-home")
    kinds = {p.kind for p in loaded.patterns}
    assert kinds == {"client_id", "custom"}
    assert loaded.warnings == []

    text, _ = redact_text("CLIENT-42 and BARE-xyz", loaded.patterns)
    assert "<REDACTED:client_id>" in text
    assert "<REDACTED:custom>" in text


def test_apply_pattern_safe_timeout_leaves_text_unredacted(monkeypatch):
    """A pathological/slow custom pattern degrades to 'leave unredacted', not a hang."""

    class _SlowRegex:
        def sub(self, repl, text):
            time.sleep(0.2)
            return text

    monkeypatch.setattr(export_mod, "_CUSTOM_PATTERN_TIMEOUT_SECS", 0.01)
    pattern = RedactionPattern("custom", _SlowRegex(), builtin=False)
    result, warning = export_mod._apply_pattern_safe("hello world", pattern)
    assert result == "hello world"
    assert warning is not None
    assert "timed out" in warning


# --------------------------------------------------------------------------
# Plan-doc discovery (v1: exact slug-in-filename match)
# --------------------------------------------------------------------------

def test_discover_plan_path_returns_none_when_no_docs_dir(tmp_path):
    assert discover_plan_path(tmp_path, "add-labels") is None


def test_discover_plan_path_matches_slug_in_filename(tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-07-01-add-labels.md").write_text("plan body")
    (plans / "2026-07-01-unrelated.md").write_text("other")
    found = discover_plan_path(tmp_path, "add-labels")
    assert found is not None
    assert found.name == "2026-07-01-add-labels.md"


def test_discover_plan_path_no_match_returns_none(tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-07-01-unrelated.md").write_text("other")
    assert discover_plan_path(tmp_path, "add-labels") is None


def test_discover_plan_path_picks_newest_on_multiple_matches(tmp_path):
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    older = plans / "2026-01-01-add-labels-v1.md"
    newer = plans / "2026-02-01-add-labels-v2.md"
    older.write_text("old")
    newer.write_text("new")
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))
    found = discover_plan_path(tmp_path, "add-labels")
    assert found == newer


def test_discover_plan_path_honors_custom_docs_dir(tmp_path):
    plans = tmp_path / "documentation" / "plans"
    plans.mkdir(parents=True)
    (plans / "add-labels.md").write_text("plan")
    assert discover_plan_path(tmp_path, "add-labels", docs_dir="docs") is None
    found = discover_plan_path(tmp_path, "add-labels", docs_dir="documentation")
    assert found is not None


# --------------------------------------------------------------------------
# collect_repo_diff — real git repos (workspace_with_git fixture)
# --------------------------------------------------------------------------

def test_collect_repo_diff_returns_diff_with_commits_ahead(workspace_with_git):
    repo_dir = workspace_with_git / "shared"
    _sh("git", "checkout", "-b", "feat/x", cwd=repo_dir)
    (repo_dir / "secret.env").write_text("API_KEY=abcdef123456\n")
    _sh("git", "add", ".", cwd=repo_dir)
    _sh("git", "commit", "-m", "add secret file", cwd=repo_dir)

    config = ConfigLoader.load(workspace_with_git / "mothership.yaml")
    task = _make_task(worktrees={"shared": repo_dir}, branch="feat/x")
    diff = collect_repo_diff(task, "shared", config)
    assert diff is not None
    assert "API_KEY=abcdef123456" in diff
    assert "secret.env" in diff


def test_collect_repo_diff_none_when_no_worktree(workspace_with_git):
    config = ConfigLoader.load(workspace_with_git / "mothership.yaml")
    task = _make_task(worktrees={}, branch="feat/x")
    assert collect_repo_diff(task, "shared", config) is None


def test_collect_repo_diff_none_when_no_commits_ahead(workspace_with_git):
    repo_dir = workspace_with_git / "shared"
    config = ConfigLoader.load(workspace_with_git / "mothership.yaml")
    # branch == base: no diff.
    task = _make_task(worktrees={"shared": repo_dir}, branch="main")
    assert collect_repo_diff(task, "shared", config) is None


def test_collect_repo_diff_none_on_unresolvable_base(workspace_with_git):
    repo_dir = workspace_with_git / "shared"
    config = ConfigLoader.load(workspace_with_git / "mothership.yaml")
    task = _make_task(
        worktrees={"shared": repo_dir}, branch="main", base_branch="does-not-exist",
    )
    assert collect_repo_diff(task, "shared", config) is None


# --------------------------------------------------------------------------
# build_export_bundle / export_task — full assembly (AC1, AC2, AC5, AC8)
# --------------------------------------------------------------------------

@pytest.fixture
def bundle_env(workspace_with_git, tmp_path):
    """A task with a journal entry, a bound spec, a plan doc, and one repo
    with real commits ahead of base — everything build_export_bundle reads."""
    state_dir = tmp_path / "state"
    logs_dir = state_dir / "logs"
    log_mgr = LogManager(logs_dir)
    log_mgr.create("add-labels")
    log_mgr.append("add-labels", "planted secret API_KEY=abcdef123456 in a note")

    specs_dir = workspace_with_git / "specs"
    spec_store = SpecStore(specs_dir)
    spec = new_spec("Add labels", now=datetime(2026, 7, 1, tzinfo=timezone.utc),
                     spec_id="add-labels", task_slug="add-labels")
    spec.body += "\nsecret in spec: sk_live_SPECSECRET123\n"
    spec_store.save(spec)

    plans_dir = workspace_with_git / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-07-01-add-labels.md").write_text(
        "# Plan\nsecret in plan: ghp_" + "c" * 36 + "\n"
    )

    repo_dir = workspace_with_git / "shared"
    _sh("git", "checkout", "-b", "feat/add-labels", cwd=repo_dir)
    (repo_dir / "secret.env").write_text("API_KEY=abcdef123456\n")
    _sh("git", "add", ".", cwd=repo_dir)
    _sh("git", "commit", "-m", "add secret file", cwd=repo_dir)

    config = ConfigLoader.load(workspace_with_git / "mothership.yaml")
    task = _make_task(
        worktrees={"shared": repo_dir}, branch="feat/add-labels", spec_id="add-labels",
        description="task description with API_KEY=taskdescsecret123",
    )
    return {
        "workspace_root": workspace_with_git,
        "config": config,
        "task": task,
        "log_manager": log_mgr,
        "spec_store": spec_store,
    }


def test_bundle_contains_all_expected_artifacts(bundle_env, tmp_path):
    dest = tmp_path / "out"
    result = build_export_bundle(
        task=bundle_env["task"], config=bundle_env["config"],
        workspace_root=bundle_env["workspace_root"],
        log_manager=bundle_env["log_manager"], spec_store=bundle_env["spec_store"],
        dest_dir=dest, redacted=False,
    )
    assert result.bundle_path == dest
    assert (dest / "journal.md").is_file()
    assert (dest / "plan.md").is_file()
    assert (dest / "spec.md").is_file()
    assert (dest / "state.json").is_file()
    assert (dest / "diffs" / "shared.diff").is_file()


def test_unredacted_export_is_faithful(bundle_env, tmp_path):
    """Without --redacted, every planted secret survives unchanged (AC5)."""
    dest = tmp_path / "out"
    build_export_bundle(
        task=bundle_env["task"], config=bundle_env["config"],
        workspace_root=bundle_env["workspace_root"],
        log_manager=bundle_env["log_manager"], spec_store=bundle_env["spec_store"],
        dest_dir=dest, redacted=False,
    )
    assert "API_KEY=abcdef123456" in (dest / "journal.md").read_text()
    assert "sk_live_SPECSECRET123" in (dest / "spec.md").read_text()
    assert "ghp_" + "c" * 36 in (dest / "plan.md").read_text()
    assert "API_KEY=abcdef123456" in (dest / "diffs" / "shared.diff").read_text()
    assert "API_KEY=taskdescsecret123" in (dest / "state.json").read_text()


def test_redacted_export_scrubs_journal_plan_spec_diffs(bundle_env, tmp_path):
    dest = tmp_path / "out"
    build_export_bundle(
        task=bundle_env["task"], config=bundle_env["config"],
        workspace_root=bundle_env["workspace_root"],
        log_manager=bundle_env["log_manager"], spec_store=bundle_env["spec_store"],
        dest_dir=dest, redacted=True,
    )
    journal = (dest / "journal.md").read_text()
    spec_text = (dest / "spec.md").read_text()
    plan_text = (dest / "plan.md").read_text()
    diff_text = (dest / "diffs" / "shared.diff").read_text()

    assert "abcdef123456" not in journal and "<REDACTED:env_secret>" in journal
    assert "SPECSECRET123" not in spec_text and "<REDACTED:stripe_live_key>" in spec_text
    assert "ghp_" + "c" * 36 not in plan_text and "<REDACTED:github_token>" in plan_text
    assert "abcdef123456" not in diff_text and "<REDACTED:env_secret>" in diff_text


def test_redacted_export_does_not_scrub_state_json(bundle_env, tmp_path):
    """AC3 + Approach scope --redacted to journal/plan/spec/diffs only; state.json
    is mship's own structured metadata slice, not scanned (see report note on
    the spec's differing 'every text file above' phrasing in Bundle contents)."""
    dest = tmp_path / "out"
    build_export_bundle(
        task=bundle_env["task"], config=bundle_env["config"],
        workspace_root=bundle_env["workspace_root"],
        log_manager=bundle_env["log_manager"], spec_store=bundle_env["spec_store"],
        dest_dir=dest, redacted=True,
    )
    state_text = (dest / "state.json").read_text()
    assert "API_KEY=taskdescsecret123" in state_text
    json.loads(state_text)  # still valid JSON


def test_bundle_omits_missing_optional_artifacts(workspace_with_git, tmp_path):
    """No spec_id, no matching plan doc, one repo with no worktree at all —
    export still succeeds and simply omits those pieces (AC8)."""
    state_dir = tmp_path / "state"
    log_mgr = LogManager(state_dir / "logs")
    log_mgr.create("no-extras")
    spec_store = SpecStore(workspace_with_git / "specs")
    config = ConfigLoader.load(workspace_with_git / "mothership.yaml")
    task = _make_task(
        slug="no-extras", branch="feat/no-extras",
        affected_repos=["shared", "auth-service"],
        worktrees={"shared": workspace_with_git / "shared"},  # no worktree for auth-service
        spec_id=None,
    )
    dest = tmp_path / "out"
    result = build_export_bundle(
        task=task, config=config, workspace_root=workspace_with_git,
        log_manager=log_mgr, spec_store=spec_store, dest_dir=dest, redacted=False,
    )
    assert (dest / "journal.md").is_file()
    assert (dest / "state.json").is_file()
    assert not (dest / "plan.md").exists()
    assert not (dest / "spec.md").exists()
    assert not (dest / "diffs" / "auth-service.diff").exists()
    assert not (dest / "diffs" / "shared.diff").exists()  # branch == main: no commits ahead
    assert result.warnings == []


# --------------------------------------------------------------------------
# export_task — --format dir|zip (AC2)
# --------------------------------------------------------------------------

def test_export_task_format_dir_default(bundle_env, tmp_path):
    out_root = tmp_path / "cwd"
    out_root.mkdir()
    result = export_task(
        task=bundle_env["task"], config=bundle_env["config"],
        workspace_root=bundle_env["workspace_root"],
        log_manager=bundle_env["log_manager"], spec_store=bundle_env["spec_store"],
        redacted=False, output_root=out_root,
    )
    assert result.bundle_path == out_root / "add-labels-export"
    assert result.bundle_path.is_dir()
    assert (result.bundle_path / "journal.md").is_file()


def test_export_task_format_zip(bundle_env, tmp_path):
    out_root = tmp_path / "cwd"
    out_root.mkdir()
    result = export_task(
        task=bundle_env["task"], config=bundle_env["config"],
        workspace_root=bundle_env["workspace_root"],
        log_manager=bundle_env["log_manager"], spec_store=bundle_env["spec_store"],
        redacted=True, format="zip", output_root=out_root,
    )
    assert result.bundle_path == out_root / "add-labels-export.zip"
    assert result.bundle_path.is_file()
    with zipfile.ZipFile(result.bundle_path) as zf:
        names = set(zf.namelist())
        assert "add-labels-export/journal.md" in names
        assert "add-labels-export/state.json" in names
        assert "add-labels-export/diffs/shared.diff" in names
        journal_bytes = zf.read("add-labels-export/journal.md")
        assert b"abcdef123456" not in journal_bytes


def test_export_task_invalid_format_raises(bundle_env, tmp_path):
    with pytest.raises(ValueError):
        export_task(
            task=bundle_env["task"], config=bundle_env["config"],
            workspace_root=bundle_env["workspace_root"],
            log_manager=bundle_env["log_manager"], spec_store=bundle_env["spec_store"],
            format="yaml", output_root=tmp_path,
        )
