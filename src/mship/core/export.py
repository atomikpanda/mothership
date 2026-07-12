"""`mship export` — bundle assembly (journal/plan/spec/state/diffs) plus an
opt-in `--redacted` regex-based secret-scrubbing pass. See spec
`mship-export-redacted-secret-redaction-mos-102` (MOS-102).

Two independent concerns live here:

- Bundle assembly (`export_task` / `build_export_bundle`): pull a task's
  journal, bound spec, discovered plan doc, state slice, and per-repo
  `base...branch` merge-base diffs from the sources that already own them (LogManager,
  SpecStore, StateManager-backed Task, git) and lay them out under
  `<task>-export/` (or zip that tree).
- Redaction (`BUILTIN_PATTERNS` / `redact_text` / `load_user_patterns`): a
  deterministic, regex-only pass applied only when `--redacted` is requested.
  Never runs otherwise — plain `mship export` is a faithful copy.

KNOWN LIMITATION — custom `redact.patterns` can still hang export: the
built-in patterns above are hand-reviewed and applied directly, but a
user-supplied pattern (from `mothership.yaml#redact.patterns` or
`~/.config/mship/redact.patterns`) is an arbitrary regex we didn't write. It
runs under `_apply_pattern_safe`'s daemon-thread `join(timeout=...)` guard,
but Python's GIL means a thread cannot be preempted mid-`re.sub` — a
pathological pattern with catastrophic backtracking (e.g. nested
repetition like `(a+)+b` against a long non-matching string) keeps running
and holding the GIL even after the timeout "returns" from the caller's
point of view, so it can still burn a core and, in the worst case, make the
process appear to hang. There is no way to truly interrupt a CPU-bound
regex from within the stdlib short of a subprocess/signal-based sandbox,
which is intentionally out of scope for v1 (see `_apply_pattern_safe`'s
docstring). Built-in patterns are exempt from this risk since they're
reviewed for linear-time behavior; only opt-in, operator-authored custom
patterns are exposed to it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mship.core.base_resolver import resolve_base
from mship.core.plan import discover_plan_path

if TYPE_CHECKING:
    from mship.core.config import WorkspaceConfig
    from mship.core.log import LogManager
    from mship.core.spec_store import SpecStore
    from mship.core.state import Task


# ---------------------------------------------------------------------------
# Redaction patterns (v1) — deterministic, regex-only. Mirrors the spec's
# "Redaction patterns (v1)" section verbatim (regex source + whole-vs-value
# decision). Anything not matching one of these shapes passes through
# unredacted; this is not a general secrets-scanning tool.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RedactionPattern:
    kind: str
    regex: "re.Pattern[str]"
    # "whole": the entire match becomes `<REDACTED:kind>`.
    # "group": only `regex.span(value_group)` is replaced, so surrounding
    # context (e.g. the `KEY=` prefix, the `Bearer ` scheme word) survives —
    # keeps the artifact's shape legible, per the spec's own stated goal.
    mode: Literal["whole", "group"] = "whole"
    value_group: int = 1
    # False for user-supplied patterns (mothership.yaml#redact.patterns /
    # ~/.config/mship/redact.patterns) — those run under `_apply_pattern_safe`'s
    # timeout guard since they're arbitrary regexes from a config file, not
    # code we've reviewed. Built-in patterns are trusted and applied directly.
    builtin: bool = True


BUILTIN_PATTERNS: list[RedactionPattern] = [
    # Private-key block runs first: non-greedy `[\s\S]*?` is linear-time (no
    # catastrophic-backtracking risk), and redacting the whole PEM block
    # before the smaller patterns run means base64 key material can't
    # coincidentally trip e.g. the AWS or Stripe patterns.
    RedactionPattern(
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----"),
    ),
    RedactionPattern("stripe_live_key", re.compile(r"sk_live_[a-zA-Z0-9]+")),
    RedactionPattern("stripe_test_key", re.compile(r"sk_test_[a-zA-Z0-9]+")),
    RedactionPattern("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36}")),
    RedactionPattern("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    RedactionPattern(
        "aws_secret_access_key",
        # No required proximity to an AKIA... match (spec q4): any
        # aws_secret_access_key assignment redacts, key id present or not.
        re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"),
        mode="group", value_group=1,
    ),
    RedactionPattern(
        "bearer_token",
        # Value char class is intersected with "isn't already a marker" so a
        # later pattern can never re-swallow an earlier one's
        # `<REDACTED:kind>` output (patterns run in a fixed sequence, each a
        # single pass — without this guard, a permissive \S+-style value
        # group would happily re-match the previous marker text, since it
        # has no whitespace either, clobbering the more specific kind).
        re.compile(r"Bearer ((?:(?!<REDACTED:)[A-Za-z0-9._\-])+)"),
        mode="group", value_group=1,
    ),
    RedactionPattern(
        "env_secret",
        re.compile(r"(?i)(API_KEY|SECRET|PASSWORD|TOKEN|CREDENTIAL)=((?:(?!<REDACTED:)\S)+)"),
        mode="group", value_group=2,
    ),
]

_CUSTOM_PATTERN_TIMEOUT_SECS = 2.0


def _redacted_replacement(m: "re.Match[str]", pattern: RedactionPattern) -> str:
    if pattern.mode == "whole":
        return f"<REDACTED:{pattern.kind}>"
    vstart, vend = m.span(pattern.value_group)
    mstart, mend = m.span(0)
    prefix = m.string[mstart:vstart]
    suffix = m.string[vend:mend]
    return f"{prefix}<REDACTED:{pattern.kind}>{suffix}"


def _apply_pattern(text: str, pattern: RedactionPattern) -> str:
    return pattern.regex.sub(lambda m: _redacted_replacement(m, pattern), text)


def _apply_pattern_safe(text: str, pattern: RedactionPattern) -> tuple[str, str | None]:
    """Apply one pattern; return (result, warning-or-None).

    Built-in patterns are trusted and run directly. User-supplied patterns
    (from `redact.patterns`) are arbitrary regexes from a config file — a
    malformed-but-compilable or catastrophic one could hang `mship export`.
    Bound it with a timeout instead of a blind `re.sub`: on timeout the text
    is left unredacted *for that one pattern* rather than the whole export
    hanging (see spec risks).

    HONEST CAVEAT (not fully solved, by design — see module docstring): this
    timeout is best-effort, not a hard interrupt. `join(timeout=...)` only
    bounds how long the *caller* waits; it cannot stop the daemon thread
    itself from still executing inside `re.sub`, because the GIL means one
    Python thread can't preempt another mid-C-call. A catastrophic pattern
    still burns a core indefinitely in the background after we "time out"
    and move on. We do not chase this further with a subprocess/signal-based
    hard-kill for v1 — that's real complexity for a risk that only exists
    for opt-in, operator-authored patterns (built-ins are reviewed and
    linear-time).

    Uses a daemon thread + `join(timeout=...)` rather than a
    `ThreadPoolExecutor` — the executor's `__exit__` does
    `shutdown(wait=True)`, which blocks until the submitted task actually
    finishes even after `future.result(timeout=...)` raised. A genuinely
    hanging pattern (catastrophic backtracking, infinite loop) would then
    hang the whole export despite the "timeout". A daemon thread has no such
    join-on-exit: on timeout we abandon it (it dies with the process) and
    return immediately.
    """
    if pattern.builtin:
        return _apply_pattern(text, pattern), None

    result_box: dict[str, str] = {}

    def _run() -> None:
        result_box["result"] = _apply_pattern(text, pattern)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=_CUSTOM_PATTERN_TIMEOUT_SECS)
    if thread.is_alive():
        return text, (
            f"custom redact pattern timed out (kind={pattern.kind!r}); "
            "left this artifact unredacted for that pattern"
        )
    return result_box["result"], None


def redact_text(text: str, patterns: list[RedactionPattern]) -> tuple[str, list[str]]:
    """Apply every pattern in order; return (redacted_text, warnings)."""
    warnings: list[str] = []
    for pattern in patterns:
        text, warning = _apply_pattern_safe(text, pattern)
        if warning:
            warnings.append(warning)
    return text, warnings


# ---------------------------------------------------------------------------
# User-configured patterns (optional; unioned with BUILTIN_PATTERNS when
# --redacted is passed and the source exists).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoadedPatterns:
    patterns: list[RedactionPattern]
    warnings: list[str]


def load_user_patterns(config: "WorkspaceConfig", *, home_dir: Path | None = None) -> LoadedPatterns:
    """Load `~/.config/mship/redact.patterns` (one regex per line) and/or
    `mothership.yaml#redact.patterns`. Missing sources are silently skipped —
    export works unchanged when neither is present. Invalid regexes are
    skipped with a warning rather than raising (basic validation; see spec
    risk on arbitrary user regexes)."""
    home_dir = home_dir if home_dir is not None else Path.home()
    patterns: list[RedactionPattern] = []
    warnings: list[str] = []

    user_file = home_dir / ".config" / "mship" / "redact.patterns"
    if user_file.is_file():
        for line in user_file.read_text().splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                compiled = re.compile(raw)
            except re.error as e:
                warnings.append(f"Skipped invalid pattern in {user_file}: {raw!r} ({e})")
                continue
            patterns.append(RedactionPattern("custom", compiled, builtin=False))

    redact_cfg = getattr(config, "redact", None)
    if redact_cfg is not None:
        for entry in redact_cfg.patterns:
            try:
                compiled = re.compile(entry.pattern)
            except re.error as e:
                warnings.append(
                    f"Skipped invalid mothership.yaml redact pattern {entry.pattern!r}: {e}"
                )
                continue
            patterns.append(RedactionPattern(entry.name or "custom", compiled, builtin=False))

    return LoadedPatterns(patterns=patterns, warnings=warnings)


# ---------------------------------------------------------------------------
# Diff assembly + binary-safe redaction over a per-repo diff blob.
# ---------------------------------------------------------------------------

_BINARY_MARKERS = ("Binary files ", "GIT binary patch")


def _chunk_is_binary(chunk: str) -> bool:
    """A chunk is binary only if one of git's own marker LINES is present —
    not merely if the marker phrase appears anywhere in the chunk's body.

    A text file can legitimately add a line that *mentions* "Binary files "
    or "GIT binary patch" as ordinary content (e.g. a comment, doc, or log
    line) sitting right next to a real secret. The old substring-anywhere
    check treated that whole chunk as binary and skipped it from redaction
    entirely, leaking the secret into a "redacted" bundle (MOS-102 Greptile
    fix: "Text Diff Bypasses Redaction"). Git always emits these markers as
    whole lines starting at column 0, so anchoring on `line.startswith(...)`
    only matches the real thing.
    """
    return "\0" in chunk or any(
        line.startswith(marker)
        for line in chunk.splitlines()
        for marker in _BINARY_MARKERS
    )


def _split_diff_chunks(diff_text: str) -> list[str]:
    """Split a `git diff` blob into per-file chunks (each `diff --git ...`)."""
    from mship.core.view.diff_sources import split_diff_by_file

    chunks = [f.body for f in split_diff_by_file(diff_text)]
    return chunks if chunks else ([diff_text] if diff_text else [])


def redact_diff_text(diff_text: str, patterns: list[RedactionPattern]) -> tuple[str, list[str]]:
    """Redact a combined per-repo diff, skipping binary chunks untouched (AC6)."""
    warnings: list[str] = []
    out: list[str] = []
    for chunk in _split_diff_chunks(diff_text):
        if _chunk_is_binary(chunk):
            out.append(chunk)
            continue
        redacted_chunk, warns = redact_text(chunk, patterns)
        warnings.extend(warns)
        out.append(redacted_chunk)
    return "".join(out), warnings


def collect_repo_diff(
    task: "Task", repo_name: str, config: "WorkspaceConfig",
    *, warnings: list[str] | None = None,
) -> str | None:
    """Return the `base..branch` diff for one affected repo, or None.

    None covers every "nothing to bundle" case (no worktree recorded for
    this repo, base ref unresolvable, git failure, or simply no commits on
    the task branch relative to base) — export omits the file rather than
    erroring (AC8).

    Base resolution mirrors the canonical helper used by dispatch/finish/
    context (`_effective_base_for_repo` in context.py): `resolve_base(...)`
    first, then `task.base_branch` as the one documented fallback for
    workspaces that don't declare `base_branch:` in mothership.yaml. It must
    NOT go on to guess a hardcoded "main" after that — `resolve_base`
    returning None means "no configured base", not "the base is main", and a
    literal "main" fallback can silently diff against an unrelated
    same-named branch and produce a materially wrong diff instead of erring
    on the side of omitting it (MOS-102 Greptile fix: "Default Base Becomes
    Main"). When the base truly can't be resolved, skip this repo's diff
    and — if `warnings` is given — append a note so the caller can surface
    it, rather than exporting a diff against a wrong base.
    """
    worktree = task.worktrees.get(repo_name)
    if worktree is None:
        return None
    repo_config = config.repos.get(repo_name)
    base = resolve_base(
        repo_name, repo_config, cli_base=None, base_map={},
        known_repos=config.repos.keys(), task_base=task.base_override,
    )
    if base is None:
        base = task.base_branch
    if base is None:
        if warnings is not None:
            warnings.append(
                f"could not resolve a base branch for repo {repo_name!r}; "
                "skipped its diff"
            )
        return None
    try:
        result = subprocess.run(
            # Three-dot (merge-base) diff, not two-dot: exports only the task
            # branch's own patch. A two-dot `base..branch` diff compares tree
            # snapshots directly, so once `base` has advanced past the fork
            # point it spuriously includes an "undo" of whatever landed on
            # `base` afterward. See MOS-102 Greptile fix.
            ["git", "diff", f"{base}...{task.branch}"],
            cwd=Path(worktree), capture_output=True, check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace")
    return text if text.strip() else None


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------

def _render_journal(task_slug: str, entries: list) -> str:
    lines = [f"# Task Journal: {task_slug}", ""]
    if not entries:
        lines.append("(no journal entries)")
        return "\n".join(lines) + "\n"
    for e in entries:
        lines.append(f"## {e.timestamp.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        meta = []
        if e.repo:
            meta.append(f"repo={e.repo}")
        if e.iteration is not None:
            meta.append(f"iteration={e.iteration}")
        if e.test_state:
            meta.append(f"test_state={e.test_state}")
        if e.action:
            meta.append(f"action={e.action}")
        if e.open_question:
            meta.append(f"open_question={e.open_question}")
        if meta:
            lines.append(", ".join(meta))
        lines.append("")
        lines.append(e.message)
        lines.append("")
    return "\n".join(lines)


def _task_state_data(task: "Task") -> dict:
    """A task's state slice as a plain JSON-serializable dict, the same shape
    StateManager persists to state.yaml (worktrees as plain strings,
    passive_repos sorted)."""
    data = task.model_dump(mode="json")
    data["worktrees"] = {k: str(v) for k, v in task.worktrees.items()}
    if "passive_repos" in data:
        data["passive_repos"] = sorted(data["passive_repos"])
    return data


def _redact_json_leaves(value: object, patterns: list[RedactionPattern]) -> tuple[object, list[str]]:
    """Recursively redact every string leaf of a JSON-serializable structure
    (dict/list/str/anything else passed through), preserving its shape.

    Used for state.json under `--redacted` instead of running `redact_text`
    over the raw serialized JSON string: the built-in patterns' greedy value
    groups (`env_secret`, `bearer_token`) aren't JSON-aware and would happily
    swallow a closing quote/brace past the secret, producing invalid JSON.
    Redacting each string value in isolation instead leaves every non-string
    leaf and all JSON structure/delimiters untouched, so the result is
    always valid JSON.
    """
    warnings: list[str] = []

    def _walk(v: object) -> object:
        if isinstance(v, str):
            redacted, warns = redact_text(v, patterns)
            warnings.extend(warns)
            return redacted
        if isinstance(v, dict):
            return {k: _walk(sub) for k, sub in v.items()}
        if isinstance(v, list):
            return [_walk(sub) for sub in v]
        return v

    return _walk(value), warnings


@dataclass(frozen=True)
class ExportBundle:
    bundle_path: Path
    warnings: list[str] = field(default_factory=list)


def build_export_bundle(
    *,
    task: "Task",
    config: "WorkspaceConfig",
    workspace_root: Path,
    log_manager: "LogManager",
    spec_store: "SpecStore",
    dest_dir: Path,
    redacted: bool,
    home_dir: Path | None = None,
) -> ExportBundle:
    """Assemble the bundle directory tree at `dest_dir`. Never errors on a
    missing-but-optional artifact (AC8) — omits it instead.

    Redaction (when `redacted`) covers journal.md, plan.md, spec.md,
    diffs/*.diff, and state.json. The text artifacts run through
    `redact_text` directly; state.json is redacted leaf-by-leaf (see
    `_redact_json_leaves`) so free-text fields like `description` /
    `blocked_reason` don't leak secrets under `--redacted` while the file
    stays valid JSON either way.
    """
    # Clear any stale prior export at this path first — a `--format dir`
    # export reusing an existing `<task>-export/` dir must not leave behind
    # files this run no longer writes (e.g. a diff for a repo no longer
    # ahead of base). The zip path stages into a fresh tempdir, so this is a
    # no-op there. See MOS-102 Greptile fix.
    #
    # BUT only ever delete a directory WE created: gate the rmtree on our
    # `.mship-export` marker. If `dest_dir` exists without the marker it's an
    # unrelated directory the user happened to have at this path — refuse
    # rather than silently `rmtree` their data (Greptile).
    marker = dest_dir / ".mship-export"
    if dest_dir.exists():
        if not marker.exists():
            raise ValueError(
                f"refusing to overwrite {dest_dir}: it exists and is not a prior "
                f"mship export (no .mship-export marker). Remove it or export elsewhere."
            )
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text("")  # tag this dir as an mship export bundle (safe to clear next time)
    warnings: list[str] = []

    patterns: list[RedactionPattern] = []
    if redacted:
        patterns = list(BUILTIN_PATTERNS)
        loaded = load_user_patterns(config, home_dir=home_dir)
        patterns.extend(loaded.patterns)
        warnings.extend(loaded.warnings)

    def _write_text(rel_path: str, content: str) -> None:
        path = dest_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if redacted:
            content, warns = redact_text(content, patterns)
            warnings.extend(warns)
        path.write_text(content)

    # journal.md
    entries = log_manager.read(task.slug)
    _write_text("journal.md", _render_journal(task.slug, entries))

    # plan.md (optional)
    plan_path = discover_plan_path(workspace_root, task.slug, docs_dir=config.docs_dir)
    if plan_path is not None:
        _write_text("plan.md", plan_path.read_text())

    # spec.md (optional)
    if task.spec_id:
        spec = spec_store.find_by_id(task.spec_id)
        if spec is not None:
            from mship.core.spec_store import serialize_spec
            _write_text("spec.md", serialize_spec(spec))

    # state.json — leaf-redacted under --redacted (see docstring above),
    # otherwise a faithful copy.
    state_data = _task_state_data(task)
    if redacted:
        state_data, warns = _redact_json_leaves(state_data, patterns)
        warnings.extend(warns)
    (dest_dir / "state.json").write_text(json.dumps(state_data, indent=2, sort_keys=False))

    # diffs/<repo>.diff (optional per repo)
    for repo_name in task.affected_repos:
        diff_text = collect_repo_diff(task, repo_name, config, warnings=warnings)
        if diff_text is None:
            continue
        if redacted:
            diff_text, warns = redact_diff_text(diff_text, patterns)
            warnings.extend(warns)
        diff_path = dest_dir / "diffs" / f"{repo_name}.diff"
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(diff_text)

    return ExportBundle(bundle_path=dest_dir, warnings=warnings)


def _is_prior_export_zip(zip_path: Path, bundle_name: str) -> bool:
    """True if `zip_path` is a prior mship export bundle — a readable zip
    containing our `<bundle_name>/.mship-export` marker entry. Gates the zip
    overwrite the same way the directory path gates its rmtree on the marker,
    so `mship export --format zip` never clobbers an unrelated same-named file
    (a corrupt/non-zip file returns False → refuse). See MOS-102 Greptile fix.
    """
    marker_arc = f"{bundle_name}/.mship-export"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            return marker_arc in zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def export_task(
    *,
    task: "Task",
    config: "WorkspaceConfig",
    workspace_root: Path,
    log_manager: "LogManager",
    spec_store: "SpecStore",
    redacted: bool = False,
    format: Literal["dir", "zip"] = "dir",
    output_root: Path | None = None,
    home_dir: Path | None = None,
) -> ExportBundle:
    """Top-level `mship export` entry point: assemble the bundle, then either
    leave it as a directory (`format="dir"`, the default) or zip it
    (`format="zip"`). Both write into `output_root` (default: cwd) as
    `<task-slug>-export/` or `<task-slug>-export.zip`.
    """
    output_root = output_root if output_root is not None else Path.cwd()
    bundle_name = f"{task.slug}-export"

    if format == "dir":
        return build_export_bundle(
            task=task, config=config, workspace_root=workspace_root,
            log_manager=log_manager, spec_store=spec_store,
            dest_dir=output_root / bundle_name, redacted=redacted,
            home_dir=home_dir,
        )

    if format != "zip":
        raise ValueError(f"Unknown export format: {format!r} (use 'dir' or 'zip')")

    # Only ever overwrite a zip WE created: refuse an existing same-named file
    # that isn't a prior mship export (mirrors the directory marker guard, so
    # an unrelated `<task>-export.zip` isn't silently clobbered). See Greptile.
    zip_path = output_root / f"{bundle_name}.zip"
    if zip_path.exists() and not _is_prior_export_zip(zip_path, bundle_name):
        raise ValueError(
            f"refusing to overwrite {zip_path}: it exists and is not a prior "
            f"mship export (no .mship-export marker). Remove it or export elsewhere."
        )

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / bundle_name
        result = build_export_bundle(
            task=task, config=config, workspace_root=workspace_root,
            log_manager=log_manager, spec_store=spec_store,
            dest_dir=staging, redacted=redacted, home_dir=home_dir,
        )
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(staging.rglob("*")):
                if file_path.is_file():
                    arcname = Path(bundle_name) / file_path.relative_to(staging)
                    zf.write(file_path, arcname)
        return ExportBundle(bundle_path=zip_path, warnings=result.warnings)
