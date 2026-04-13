# Per-Repo PR Base Branch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `mship finish` sends `--base <branch>` to `gh pr create` per repo, driven by `RepoConfig.base_branch` with `--base` and `--base-map` CLI overrides and up-front remote verification.

**Architecture:** Add an optional `base_branch` field on `RepoConfig`. Put precedence logic in a new `core/base_resolver.py` (pure functions). Extend `PRManager` with an optional `base=` kwarg on `create_pr` and a new `verify_base_exists` helper. Wire it all into `mship finish`: parse CLI overrides, resolve effective base per repo, verify each base on the remote before any push, pass `base=` into `create_pr`, and print `repo: head → base ✓ url` per repo.

**Tech Stack:** Python/Typer/Pydantic, existing `ShellRunner` wrapping subprocess, `gh` CLI, `git ls-remote`.

**Spec:** `docs/superpowers/specs/2026-04-13-pr-base-branch-design.md`

---

## File Structure

**Create:**
- `src/mship/core/base_resolver.py` — pure functions: `parse_base_map`, `resolve_base`, + exceptions.
- `tests/core/test_base_resolver.py`

**Modify:**
- `src/mship/core/config.py` — add `base_branch: str | None = None` to `RepoConfig`.
- `src/mship/core/pr.py` — `create_pr(..., base: str | None = None)`; new `verify_base_exists(repo_path, base)`.
- `src/mship/cli/worktree.py` — `finish` command: new `--base` / `--base-map` options, up-front resolve+verify, pass `base=` to `create_pr`, new per-repo line output, `"base"` in JSON.
- `tests/core/test_pr.py` — new cases for `base=` and `verify_base_exists`.
- `tests/core/test_config.py` — one case asserting `RepoConfig.base_branch` parses.
- `tests/test_finish_integration.py` — one new case for `base_branch` flowing into `gh pr create`.

---

## Task 1: Add `base_branch` to `RepoConfig`

**Files:**
- Modify: `src/mship/core/config.py` (RepoConfig class around line 32)
- Test: `tests/core/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_config.py`:
```python
def test_repo_config_accepts_base_branch(tmp_path):
    import yaml
    from mship.core.config import ConfigLoader

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "workspace": "ws",
        "repos": {
            "cli": {"path": "../cli", "type": "service", "base_branch": "main"},
            "api": {"path": "../api", "type": "service"},
        },
    }))
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.repos["cli"].base_branch == "main"
    assert cfg.repos["api"].base_branch is None
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/core/test_config.py::test_repo_config_accepts_base_branch -v`
Expected: FAIL (pydantic extra-field rejection, or AttributeError on `.base_branch`).

- [ ] **Step 3: Add the field**

In `src/mship/core/config.py`, in the `RepoConfig` class body (next to `healthcheck: Healthcheck | None = None`), add:
```python
    base_branch: str | None = None
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/config.py tests/core/test_config.py
git commit -m "feat: add RepoConfig.base_branch field"
```

---

## Task 2: `base_resolver` module — `parse_base_map` + `resolve_base`

**Files:**
- Create: `src/mship/core/base_resolver.py`
- Test: `tests/core/test_base_resolver.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_base_resolver.py`:
```python
import pytest

from mship.core.base_resolver import (
    parse_base_map,
    resolve_base,
    InvalidBaseMapError,
    UnknownRepoInBaseMapError,
)


# --- parse_base_map ---

def test_parse_empty_is_empty():
    assert parse_base_map("") == {}


def test_parse_single_pair():
    assert parse_base_map("cli=main") == {"cli": "main"}


def test_parse_multiple_pairs():
    assert parse_base_map("cli=main,api=release/x") == {"cli": "main", "api": "release/x"}


def test_parse_tolerates_whitespace():
    assert parse_base_map("  cli = main , api=release/x ") == {"cli": "main", "api": "release/x"}


def test_parse_rejects_missing_equals():
    with pytest.raises(InvalidBaseMapError):
        parse_base_map("cli,api=main")


def test_parse_rejects_empty_key():
    with pytest.raises(InvalidBaseMapError):
        parse_base_map("=main")


def test_parse_rejects_empty_value():
    with pytest.raises(InvalidBaseMapError):
        parse_base_map("cli=")


# --- resolve_base ---

class _RepoCfg:
    def __init__(self, base_branch=None):
        self.base_branch = base_branch


KNOWN = {"cli": _RepoCfg("main"), "api": _RepoCfg("cli-refactor"), "schemas": _RepoCfg(None)}


def test_resolve_uses_config_when_no_cli_args():
    assert resolve_base("cli", KNOWN["cli"], cli_base=None, base_map={}, known_repos=KNOWN.keys()) == "main"


def test_resolve_returns_none_when_config_unset_and_no_cli_args():
    assert resolve_base("schemas", KNOWN["schemas"], cli_base=None, base_map={}, known_repos=KNOWN.keys()) is None


def test_resolve_cli_base_overrides_config():
    assert resolve_base("cli", KNOWN["cli"], cli_base="develop", base_map={}, known_repos=KNOWN.keys()) == "develop"


def test_resolve_base_map_overrides_cli_base():
    # most-specific wins
    result = resolve_base("cli", KNOWN["cli"], cli_base="develop", base_map={"cli": "release/7"}, known_repos=KNOWN.keys())
    assert result == "release/7"


def test_resolve_base_map_overrides_config_even_without_cli_base():
    result = resolve_base("cli", KNOWN["cli"], cli_base=None, base_map={"cli": "release/7"}, known_repos=KNOWN.keys())
    assert result == "release/7"


def test_resolve_cli_base_used_when_config_unset():
    result = resolve_base("schemas", KNOWN["schemas"], cli_base="develop", base_map={}, known_repos=KNOWN.keys())
    assert result == "develop"


def test_resolve_rejects_unknown_repo_in_map():
    with pytest.raises(UnknownRepoInBaseMapError) as exc:
        resolve_base("cli", KNOWN["cli"], cli_base=None, base_map={"nope": "main"}, known_repos=KNOWN.keys())
    assert "nope" in str(exc.value)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_base_resolver.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`src/mship/core/base_resolver.py`:
```python
"""Resolve effective PR base branch per repo from config + CLI flags."""
from __future__ import annotations

from typing import Iterable


class InvalidBaseMapError(ValueError):
    pass


class UnknownRepoInBaseMapError(ValueError):
    pass


def parse_base_map(raw: str) -> dict[str, str]:
    """Parse 'repoA=branch,repoB=branch' into a dict.

    Empty input returns {}. Whitespace around keys, values, and separators is
    stripped. Raises InvalidBaseMapError on malformed input.
    """
    if not raw or not raw.strip():
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            raise InvalidBaseMapError(
                f"Invalid --base-map entry {pair!r}: expected repo=branch"
            )
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise InvalidBaseMapError(
                f"Invalid --base-map entry {pair!r}: key and value must be non-empty"
            )
        out[key] = value
    return out


def resolve_base(
    repo_name: str,
    repo_config,
    cli_base: str | None,
    base_map: dict[str, str],
    known_repos: Iterable[str],
) -> str | None:
    """Return the effective base branch for a repo or None for gh default.

    Precedence (most-specific wins): base_map entry > cli_base > repo_config.base_branch > None.
    Raises UnknownRepoInBaseMapError if base_map references a repo not in known_repos.
    """
    known = set(known_repos)
    unknown = [r for r in base_map if r not in known]
    if unknown:
        raise UnknownRepoInBaseMapError(
            f"Unknown repo(s) in --base-map: {', '.join(sorted(unknown))}. "
            f"Known: {sorted(known)}"
        )
    if repo_name in base_map:
        return base_map[repo_name]
    if cli_base is not None:
        return cli_base
    return getattr(repo_config, "base_branch", None)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_base_resolver.py -v`
Expected: PASS (all 12 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/base_resolver.py tests/core/test_base_resolver.py
git commit -m "feat: add base_resolver for per-repo PR base branch precedence"
```

---

## Task 3: `PRManager.create_pr(base=)` + `verify_base_exists`

**Files:**
- Modify: `src/mship/core/pr.py` (create_pr at line 34, add new method)
- Test: `tests/core/test_pr.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_pr.py`:
```python
def test_create_pr_with_base(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="https://github.com/org/repo/pull/42\n",
        stderr="",
    )
    mgr = PRManager(mock_shell)
    mgr.create_pr(
        repo_path=Path("/tmp/repo"),
        branch="feat/test",
        title="t",
        body="b",
        base="release/7",
    )
    cmd = mock_shell.run.call_args.args[0]
    assert "--base 'release/7'" in cmd or "--base release/7" in cmd


def test_create_pr_without_base_omits_flag(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="https://github.com/org/repo/pull/42\n",
        stderr="",
    )
    mgr = PRManager(mock_shell)
    mgr.create_pr(
        repo_path=Path("/tmp/repo"),
        branch="feat/test",
        title="t",
        body="b",
    )
    cmd = mock_shell.run.call_args.args[0]
    assert "--base" not in cmd


def test_verify_base_exists_true(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(
        returncode=0,
        stdout="abc123\trefs/heads/main\n",
        stderr="",
    )
    mgr = PRManager(mock_shell)
    assert mgr.verify_base_exists(Path("/tmp/repo"), "main") is True
    cmd = mock_shell.run.call_args.args[0]
    assert "git ls-remote --heads origin" in cmd
    assert "main" in cmd


def test_verify_base_exists_empty_output_false(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=0, stdout="", stderr="")
    mgr = PRManager(mock_shell)
    assert mgr.verify_base_exists(Path("/tmp/repo"), "nope") is False


def test_verify_base_exists_nonzero_exit_false(mock_shell: MagicMock):
    mock_shell.run.return_value = ShellResult(returncode=128, stdout="", stderr="network err")
    mgr = PRManager(mock_shell)
    assert mgr.verify_base_exists(Path("/tmp/repo"), "main") is False
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/core/test_pr.py -v -k "base"`
Expected: FAIL — `create_pr` has no `base` kwarg; `verify_base_exists` missing.

- [ ] **Step 3: Implement**

In `src/mship/core/pr.py`, replace the existing `create_pr` method and add `verify_base_exists` (keep the rest of the class as-is):

```python
    def create_pr(
        self, repo_path: Path, branch: str, title: str, body: str,
        base: str | None = None,
    ) -> str:
        safe_title = shlex.quote(title)
        safe_body = shlex.quote(body)
        cmd = (
            f"gh pr create --title {safe_title} --body {safe_body} "
            f"--head {shlex.quote(branch)}"
        )
        if base is not None:
            cmd += f" --base {shlex.quote(base)}"
        result = self._shell.run(cmd, cwd=repo_path)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to create PR: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def verify_base_exists(self, repo_path: Path, base: str) -> bool:
        """Return True if `base` exists as a head on origin, else False.

        Network/auth failures are treated as False (fail-closed).
        """
        result = self._shell.run(
            f"git ls-remote --heads origin {shlex.quote(base)}",
            cwd=repo_path,
        )
        if result.returncode != 0:
            return False
        return bool(result.stdout.strip())
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/core/test_pr.py -v`
Expected: PASS (all existing tests + 5 new).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/pr.py tests/core/test_pr.py
git commit -m "feat: PRManager accepts --base and verifies remote existence"
```

---

## Task 4: Wire CLI `--base` / `--base-map` into `mship finish`

**Files:**
- Modify: `src/mship/cli/worktree.py` (finish command around line 100)
- Test: `tests/test_finish_integration.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_finish_integration.py`:
```python
def test_finish_passes_base_from_config(finish_workspace, tmp_path):
    """Config base_branch flows into gh pr create --base."""
    import yaml

    workspace, mock_shell = finish_workspace

    # Rewrite config to set base_branch on `shared`
    cfg_path = workspace / "mothership.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["repos"]["shared"]["base_branch"] = "release/7"
    cfg_path.write_text(yaml.safe_dump(cfg))
    container.config.reset()

    result = runner.invoke(app, ["spawn", "base test", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    call_log: list[str] = []

    def mock_run(cmd, cwd, env=None):
        call_log.append(cmd)
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="abc\trefs/heads/release/7\n", stderr="")
        if "git push" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://github.com/org/shared/pull/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish"])
    assert result.exit_code == 0, result.output

    create_calls = [c for c in call_log if "gh pr create" in c]
    assert len(create_calls) == 1
    assert "--base" in create_calls[0]
    assert "release/7" in create_calls[0]


def test_finish_fails_when_base_missing_on_remote(finish_workspace):
    import yaml

    workspace, mock_shell = finish_workspace
    cfg_path = workspace / "mothership.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["repos"]["shared"]["base_branch"] = "nope"
    cfg_path.write_text(yaml.safe_dump(cfg))
    container.config.reset()

    result = runner.invoke(app, ["spawn", "missing base", "--repos", "shared"])
    assert result.exit_code == 0, result.output

    pushed: list[str] = []

    def mock_run(cmd, cwd, env=None):
        if "gh auth status" in cmd:
            return ShellResult(returncode=0, stdout="Logged in", stderr="")
        if "git ls-remote" in cmd:
            return ShellResult(returncode=0, stdout="", stderr="")  # empty → missing
        if "git push" in cmd:
            pushed.append(cmd)
            return ShellResult(returncode=0, stdout="", stderr="")
        if "gh pr create" in cmd:
            return ShellResult(returncode=0, stdout="https://x/1\n", stderr="")
        return ShellResult(returncode=0, stdout="", stderr="")

    mock_shell.run.side_effect = mock_run

    result = runner.invoke(app, ["finish"])
    assert result.exit_code != 0
    assert "nope" in result.output.lower() or "base" in result.output.lower()
    assert pushed == [], "no repo should be pushed when a base is missing"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_finish_integration.py -v -k "base"`
Expected: FAIL — finish does not yet handle `base_branch` or verification.

- [ ] **Step 3: Extend the `finish` command**

In `src/mship/cli/worktree.py`, modify the `finish` command signature and add the resolve/verify phase before the PR-creation loop. Full current `finish` body is at approximately lines 100–200; apply these changes:

1. Add two new Typer options:
```python
    def finish(
        handoff: bool = typer.Option(False, "--handoff", help="Generate CI handoff manifest"),
        base: Optional[str] = typer.Option(None, "--base", help="Global override of PR base branch for all repos"),
        base_map: Optional[str] = typer.Option(None, "--base-map", help="Per-repo PR base overrides, e.g. 'cli=main,api=release/x'"),
    ):
```
(ensure `Optional` is imported from `typing` at the top of the file; add if missing.)

2. After the `handoff` branch and after `pr_mgr.check_gh_available()`, add this block before the `for i, repo_name in enumerate(ordered, 1):` loop:

```python
        # --- Resolve + verify PR base branches up front ---
        from mship.core.base_resolver import (
            parse_base_map,
            resolve_base,
            InvalidBaseMapError,
            UnknownRepoInBaseMapError,
        )

        try:
            parsed_map = parse_base_map(base_map or "")
        except InvalidBaseMapError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        try:
            effective_bases = {
                repo_name: resolve_base(
                    repo_name,
                    config.repos[repo_name],
                    cli_base=base,
                    base_map=parsed_map,
                    known_repos=config.repos.keys(),
                )
                for repo_name in ordered
            }
        except UnknownRepoInBaseMapError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        missing: list[tuple[str, str]] = []
        for repo_name, eff_base in effective_bases.items():
            if eff_base is None:
                continue
            if repo_name in task.pr_urls:
                continue  # skip repos already done
            repo_path = config.repos[repo_name].path
            if repo_name in task.worktrees:
                from pathlib import Path as _P
                wt = _P(task.worktrees[repo_name])
                if wt.exists():
                    repo_path = wt
            if not pr_mgr.verify_base_exists(repo_path, eff_base):
                missing.append((repo_name, eff_base))

        if missing:
            output.error("Base branch not found on remote:")
            for repo_name, eff_base in missing:
                output.error(f"  {repo_name}: {eff_base}")
            raise typer.Exit(code=1)
```

3. In the existing PR-creation loop (`for i, repo_name in enumerate(ordered, 1):`):
   - Replace the `pr_mgr.create_pr(...)` call with:
```python
            try:
                pr_url = pr_mgr.create_pr(
                    repo_path=repo_path,
                    branch=task.branch,
                    title=task.description,
                    body=task.description,
                    base=effective_bases[repo_name],
                )
            except RuntimeError as e:
                output.error(f"{repo_name}: {e}")
                raise typer.Exit(code=1)
```
   - After storing state (`task.pr_urls[repo_name] = pr_url; state_mgr.save(state)`) and appending to `pr_list`, replace whatever current per-repo TTY print exists with:
```python
            base_label = effective_bases[repo_name] or "(default)"
            if output.is_tty:
                output.print(f"  {repo_name}: {task.branch} → {base_label}  ✓ {pr_url}")
            pr_list[-1]["base"] = effective_bases[repo_name]
```
     (If `pr_list[-1]` was just appended above, this adds `"base"` to the dict. If the existing code already appends before the print, keep this line.)

4. Ensure the JSON-mode output (end of the function; search for `output.json(` in the finish block) serializes `pr_list` unchanged — the added `"base"` key will flow through.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_finish_integration.py -v`
Expected: PASS for all existing tests + the two new ones.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest -q`
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/worktree.py tests/test_finish_integration.py
git commit -m "feat: mship finish resolves and verifies per-repo PR base branches"
```

---

## Task 5: README documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a section under CLI Reference / finish**

Find the `mship finish` section in `README.md` (or the general CLI reference near the finish command). Insert the following:

```markdown
#### PR base branch

Each repo's PR can target a non-default base:

```yaml
repos:
  cli:
    path: ../cli
    base_branch: main
  api:
    path: ../api
    base_branch: cli-refactor
```

Overrides (most-specific wins):

- `--base <branch>` — global override for all repos.
- `--base-map cli=main,api=release/x` — per-repo overrides.

`mship finish` verifies every resolved base exists on `origin` before any push. Repos with no configured or overridden base use the remote default branch.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document per-repo PR base branch"
```

---

## Self-Review

**Spec coverage:**

- Config field `base_branch` on RepoConfig → Task 1. ✓
- CLI `--base` and `--base-map` with precedence (most-specific wins) → Task 2 (`resolve_base`) + Task 4 (wiring). ✓
- `--base-map` parser with whitespace tolerance and error on bad format → Task 2. ✓
- Unknown repo in `--base-map` rejected before network → Task 2 (raise) + Task 4 (catch and exit 1). ✓
- Remote verification before any push, grouped error, no partial state → Task 4 verify block runs before the PR loop; `task.pr_urls` writes happen only after successful `create_pr`. ✓
- `PRManager.create_pr` accepts `base=` and appends `--base <quoted>`; omits flag when `None` → Task 3. ✓
- `PRManager.verify_base_exists` returns True on matching ref, False on empty output or non-zero exit → Task 3. ✓
- TTY output: `repo: branch → base ✓ url`; `(default)` literal for None base → Task 4. ✓
- JSON output: `"base"` key per PR entry, `null` for default → Task 4. ✓
- README documentation → Task 5. ✓
- Non-change for repos without `base_branch` and no overrides → preserved: `resolve_base` returns `None` → `create_pr(base=None)` omits `--base` → `gh` uses default. ✓

**Placeholder scan:** None. All steps have concrete code or concrete README text.

**Type consistency:**
- `resolve_base` signature (`repo_name, repo_config, cli_base, base_map, known_repos`) matches the Task 4 call site.
- `parse_base_map` returns `dict[str, str]`; Task 4 feeds it into `resolve_base` as `base_map=parsed_map`.
- `verify_base_exists(repo_path, base)` returns `bool`; Task 4 uses it as a truthy check.
- `create_pr(..., base: str | None = None)` matches the Task 4 call `base=effective_bases[repo_name]`.
- `InvalidBaseMapError` and `UnknownRepoInBaseMapError` are defined in Task 2 and imported in Task 4.

**Known cosmetic deferrals:**
- The `(default)` label is literal text, not translated. Acceptable.
- No parallel `ls-remote` — typical workspace is small; deferred per spec's Out of Scope.
