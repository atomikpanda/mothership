# `bind_files` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-04-18-bind-files-design.md`

**Goal:** Add `bind_files: list[str]` per-repo config. At `mship spawn`, each entry (literal path or glob) expands against the source repo's `git ls-files --others --ignored --exclude-standard` set; matched files are copied into the new worktree at the same relative path.

**Architecture:** New field + validator on `RepoConfig` in `core/config.py`. Three new methods on `WorktreeManager` in `core/worktree.py` (`_git_ignored_files`, `_match_bind_patterns`, `_copy_bind_files`) paralleling `_create_symlinks`. Integration point: same spawn lifecycle position as `_create_symlinks`, called immediately after it in both the `git_root` branch (`core/worktree.py:121`) and the normal-repo branch (`core/worktree.py:154`).

**Tech Stack:** Python 3.14, Pydantic v2, `pathlib.PurePosixPath` for pattern matching, `shutil.copy2`, `subprocess` via existing `ShellRunner` for `git ls-files`.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `src/mship/core/config.py` | Add `bind_files: list[str] = []` on `RepoConfig` + `model_validator` rejecting absolute paths and `..` segments | modify |
| `src/mship/core/worktree.py` | Add `_git_ignored_files`, `_match_bind_patterns`, `_copy_bind_files` methods + integrate into spawn loop (both branches) | modify |
| `tests/core/test_config.py` | Validation tests for the new field | extend |
| `tests/core/test_worktree.py` | Pure-function pattern-match tests + integration tests using a real git fixture | extend |

No new modules. No CLI-surface changes (the field is declarative YAML; spawn behavior already surfaces warnings from this subsystem).

---

## Task 1: `bind_files` field + validation on `RepoConfig` (TDD)

**Files:**
- Modify: `src/mship/core/config.py` (add field + validator)
- Modify: `tests/core/test_config.py` (add validation tests)

- [ ] **Step 1.1: Write failing validation tests**

Append to `tests/core/test_config.py`:

```python
# --- bind_files validation (issue #39) ---

def test_bind_files_accepts_relative_paths(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - .env\n"
        "      - .vscode/settings.local.json\n"
        "      - apps/**/.env\n"
    )
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.repos["r"].bind_files == [
        ".env",
        ".vscode/settings.local.json",
        "apps/**/.env",
    ]


def test_bind_files_rejects_absolute_path(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - /etc/secrets\n"
    )
    with pytest.raises(Exception) as exc:
        ConfigLoader.load(cfg_path)
    assert "/etc/secrets" in str(exc.value)
    assert "absolute" in str(exc.value).lower()


def test_bind_files_rejects_parent_escape(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - ../other-repo/.env\n"
    )
    with pytest.raises(Exception) as exc:
        ConfigLoader.load(cfg_path)
    assert "../other-repo/.env" in str(exc.value) or ".." in str(exc.value)


def test_bind_files_empty_list_is_default(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")

    cfg_path = tmp_path / "mothership.yaml"
    cfg_path.write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
    )
    cfg = ConfigLoader.load(cfg_path)
    assert cfg.repos["r"].bind_files == []
```

Add to the imports at top of the test file if not already present:
```python
import pytest
from mship.core.config import ConfigLoader
```

Run: `uv run pytest tests/core/test_config.py -k bind_files -v`
Expected: 4 FAIL — `bind_files` is not a field on `RepoConfig` yet.

- [ ] **Step 1.2: Add the field + validator to `RepoConfig`**

In `src/mship/core/config.py`, find `class RepoConfig(BaseModel):` (around line 32). After the `symlink_dirs` field (line 41), add:

```python
    bind_files: list[str] = []
```

Then add a `model_validator` method inside the class (near the other validators — the file already uses `@model_validator(mode="after")` elsewhere; match that style):

```python
    @model_validator(mode="after")
    def validate_bind_files(self) -> "RepoConfig":
        for entry in self.bind_files:
            p = Path(entry)
            if p.is_absolute():
                raise ValueError(
                    f"bind_files entry {entry!r} is absolute; bind_files must be relative paths or globs"
                )
            # Reject any `..` segment (escapes the repo).
            if ".." in p.parts:
                raise ValueError(
                    f"bind_files entry {entry!r} contains '..'; bind_files must stay inside the repo"
                )
        return self
```

If `Path` isn't imported at the top of `config.py`, add `from pathlib import Path` to the imports (check: it likely is — other models use it).

Run: `uv run pytest tests/core/test_config.py -k bind_files -v`
Expected: 4 PASS.

- [ ] **Step 1.3: Full config test suite still passes**

Run: `uv run pytest tests/core/test_config.py -q`
Expected: all pre-existing tests pass plus the 4 new ones.

- [ ] **Step 1.4: Commit (pair with `mship journal`)**

```bash
git add src/mship/core/config.py tests/core/test_config.py
git commit -m "feat(config): bind_files field + validation on RepoConfig"
mship journal "added bind_files field + validator to RepoConfig; 4/4 new tests pass" --action committed
```

---

## Task 2: `_git_ignored_files` helper (TDD, real git fixture)

**Files:**
- Modify: `src/mship/core/worktree.py` (add method)
- Modify: `tests/core/test_worktree.py` (add test)

- [ ] **Step 2.1: Write failing test**

Append to `tests/core/test_worktree.py`:

```python
# --- bind_files helpers (issue #39) ---

import subprocess
from pathlib import PurePosixPath


def _init_repo_with_ignored_files(tmp_path: Path) -> Path:
    """Git-init a repo with a few tracked and ignored files for bind_files testing."""
    import os
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text(
        ".env\n"
        ".env.*\n"
        ".venv/\n"
        "node_modules/\n"
        "apps/*/.env\n"
    )
    (repo / "tracked.txt").write_text("tracked\n")
    (repo / ".env").write_text("ENV=yes\n")
    (repo / ".env.local").write_text("LOCAL=1\n")
    (repo / "apps").mkdir()
    (repo / "apps" / "foo").mkdir()
    (repo / "apps" / "foo" / ".env").write_text("FOO=1\n")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "fake").write_text("pyc\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "pkg.env").write_text("pkg\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True, env=env)
    return repo


def test_git_ignored_files_lists_ignored_leaf_files(tmp_path: Path):
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager

    repo = _init_repo_with_ignored_files(tmp_path)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    # Commit the Taskfile so repo isn't dirty
    import os
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=env)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    files = mgr._git_ignored_files(repo)
    names = {str(p) for p in files}

    # Expected: .env, .env.local, apps/foo/.env are present.
    assert ".env" in names
    assert ".env.local" in names
    assert "apps/foo/.env" in names

    # .venv/ and node_modules/ — git does not descend into ignored dirs.
    # Their CONTENTS must not be present.
    assert not any(n.startswith(".venv/") for n in names), f"should not include .venv contents: {names}"
    assert not any(n.startswith("node_modules/") for n in names), f"should not include node_modules contents: {names}"

    # Tracked files should never appear.
    assert "tracked.txt" not in names
    assert ".gitignore" not in names
```

Run: `uv run pytest tests/core/test_worktree.py -k git_ignored_files -v`
Expected: FAIL — `_git_ignored_files` not defined.

- [ ] **Step 2.2: Implement `_git_ignored_files`**

In `src/mship/core/worktree.py`, add this method on `WorktreeManager` (place it just before `_create_symlinks`):

```python
    def _git_ignored_files(self, source_root: Path) -> list[PurePosixPath]:
        """Return gitignored leaf files in source_root, as relative PurePosixPath.

        Uses `git ls-files --others --ignored --exclude-standard`, which:
        - returns gitignored files at their relative paths,
        - does NOT descend into ignored directories (so .venv/*, node_modules/*, etc. are NOT listed),
        - returns ignored directories themselves as "dir/" entries, which we filter out.
        """
        result = self._shell.run(
            "git ls-files --others --ignored --exclude-standard",
            cwd=source_root,
        )
        if result.returncode != 0:
            return []
        out: list[PurePosixPath] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # Skip directory entries (git lists ignored dirs as "path/").
            if line.endswith("/"):
                continue
            out.append(PurePosixPath(line))
        return out
```

Add `from pathlib import PurePosixPath` to the imports at the top of `worktree.py` if not already present.

Run: `uv run pytest tests/core/test_worktree.py -k git_ignored_files -v`
Expected: PASS.

- [ ] **Step 2.3: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat(worktree): _git_ignored_files helper using git ls-files --ignored"
mship journal "added _git_ignored_files helper; returns gitignored leaf paths only (no .venv/* or node_modules/* contents)" --action committed
```

---

## Task 3: `_match_bind_patterns` pure function (TDD)

**Files:**
- Modify: `src/mship/core/worktree.py` (add method)
- Modify: `tests/core/test_worktree.py` (add tests)

- [ ] **Step 3.1: Write failing tests**

Append to `tests/core/test_worktree.py`:

```python
def _mgr_stub() -> "WorktreeManager":
    """Minimal WorktreeManager just for calling pure methods; no real deps."""
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner
    # Use a dummy-but-valid config (tests only touch pure methods).
    from mship.core.config import WorkspaceConfig, RepoConfig
    from pathlib import Path
    cfg = WorkspaceConfig(
        workspace="t",
        repos={"r": RepoConfig(path=Path("/tmp/x"), type="service")},
    )
    return WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )


def test_match_bind_patterns_literal_match():
    mgr = _mgr_stub()
    candidates = [PurePosixPath(".env"), PurePosixPath(".env.local")]
    out = mgr._match_bind_patterns([".env"], candidates)
    assert out == [PurePosixPath(".env")]


def test_match_bind_patterns_single_segment_glob():
    mgr = _mgr_stub()
    candidates = [PurePosixPath(".env"), PurePosixPath(".env.local"), PurePosixPath("local.env")]
    out = mgr._match_bind_patterns([".env*"], candidates)
    out_set = {str(p) for p in out}
    assert out_set == {".env", ".env.local"}


def test_match_bind_patterns_question_mark_glob():
    mgr = _mgr_stub()
    candidates = [
        PurePosixPath(".env"),
        PurePosixPath(".env.1"),
        PurePosixPath(".env.10"),
    ]
    out = mgr._match_bind_patterns([".env.?"], candidates)
    out_set = {str(p) for p in out}
    assert out_set == {".env.1"}


def test_match_bind_patterns_double_star_recursive():
    mgr = _mgr_stub()
    candidates = [
        PurePosixPath(".env"),
        PurePosixPath("apps/foo/.env"),
        PurePosixPath("services/bar/.env"),
    ]
    out = mgr._match_bind_patterns(["**/.env"], candidates)
    out_set = {str(p) for p in out}
    assert out_set == {".env", "apps/foo/.env", "services/bar/.env"}


def test_match_bind_patterns_single_level_vs_double_star():
    mgr = _mgr_stub()
    candidates = [
        PurePosixPath("apps/foo/.env"),
        PurePosixPath("apps/foo/bar/.env"),
    ]
    single = mgr._match_bind_patterns(["apps/*/.env"], candidates)
    double = mgr._match_bind_patterns(["apps/**/.env"], candidates)
    assert {str(p) for p in single} == {"apps/foo/.env"}
    assert {str(p) for p in double} == {"apps/foo/.env", "apps/foo/bar/.env"}


def test_match_bind_patterns_multi_pattern_dedup():
    mgr = _mgr_stub()
    candidates = [PurePosixPath(".env"), PurePosixPath(".env.local")]
    out = mgr._match_bind_patterns([".env", ".env*"], candidates)
    # Even though .env is matched by both patterns, it should appear exactly once.
    out_list = [str(p) for p in out]
    assert out_list.count(".env") == 1
    assert ".env.local" in out_list


def test_match_bind_patterns_empty_patterns():
    mgr = _mgr_stub()
    assert mgr._match_bind_patterns([], [PurePosixPath(".env")]) == []


def test_match_bind_patterns_zero_matches_silent():
    mgr = _mgr_stub()
    # Glob that matches nothing — no exception, empty list.
    assert mgr._match_bind_patterns(["apps/**/.env"], [PurePosixPath(".env")]) == []
```

Run: `uv run pytest tests/core/test_worktree.py -k match_bind_patterns -v`
Expected: 8 FAIL.

- [ ] **Step 3.2: Implement `_match_bind_patterns`**

In `src/mship/core/worktree.py`, add this method on `WorktreeManager` (place it right after `_git_ignored_files`):

```python
    def _match_bind_patterns(
        self,
        patterns: list[str],
        candidates: list[PurePosixPath],
    ) -> list[PurePosixPath]:
        """Match patterns against candidate paths.

        Supports `*`, `?`, and `**` via pathlib's glob semantics. Dedups across
        patterns while preserving first-seen order.
        """
        seen: set[PurePosixPath] = set()
        out: list[PurePosixPath] = []
        for pattern in patterns:
            for cand in candidates:
                if cand in seen:
                    continue
                if cand.full_match(pattern):
                    seen.add(cand)
                    out.append(cand)
        return out
```

**Note on `PurePosixPath.full_match`:** Python 3.13+ added `PurePath.full_match()` that supports `**` semantics out of the box. If the runtime Python is older or `full_match` misbehaves on a pattern, the test suite will catch it — fallback is a small custom matcher that splits `pattern` and `cand` into segments and matches with `fnmatch.fnmatchcase` per segment, treating a `**` segment as "match zero or more segments." Implement the fallback only if the tests show `full_match` doesn't do what we need.

Run: `uv run pytest tests/core/test_worktree.py -k match_bind_patterns -v`
Expected: 8 PASS.

- [ ] **Step 3.3: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat(worktree): _match_bind_patterns with *, ?, ** and dedup"
mship journal "added _match_bind_patterns; supports full globstar, 8/8 unit tests" --action committed
```

---

## Task 4: `_copy_bind_files` top-level method (TDD)

**Files:**
- Modify: `src/mship/core/worktree.py` (add method)
- Modify: `tests/core/test_worktree.py` (add integration tests)

- [ ] **Step 4.1: Write failing tests**

Append to `tests/core/test_worktree.py`:

```python
import shutil

def test_copy_bind_files_copies_matched_files(tmp_path: Path):
    """End-to-end: given a git repo with ignored files, _copy_bind_files
    copies the listed ones into a fake 'worktree' directory, preserving
    relative paths."""
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    repo = _init_repo_with_ignored_files(tmp_path)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - .env\n"
        "      - apps/**/.env\n"
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    import os, subprocess
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=env)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    worktree = tmp_path / "fake-worktree"
    worktree.mkdir()

    warnings = mgr._copy_bind_files("r", cfg.repos["r"], worktree)
    assert warnings == []

    assert (worktree / ".env").read_text() == "ENV=yes\n"
    assert (worktree / "apps" / "foo" / ".env").read_text() == "FOO=1\n"
    # .env.local NOT copied (pattern was .env and apps/**/.env, not .env.local).
    assert not (worktree / ".env.local").exists()
    # .venv contents NEVER copied.
    assert not (worktree / ".venv").exists()


def test_copy_bind_files_warns_on_missing_literal(tmp_path: Path):
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    repo = _init_repo_with_ignored_files(tmp_path)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - .envv\n"  # typo — does not exist
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    import os, subprocess
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=env)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    worktree = tmp_path / "fake-worktree"
    worktree.mkdir()

    warnings = mgr._copy_bind_files("r", cfg.repos["r"], worktree)
    assert len(warnings) == 1
    assert ".envv" in warnings[0]
    assert "source missing" in warnings[0].lower() or "missing" in warnings[0].lower()


def test_copy_bind_files_zero_glob_matches_silent(tmp_path: Path):
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    repo = _init_repo_with_ignored_files(tmp_path)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - nonexistent/**/.env\n"  # glob matches nothing — silent
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    import os, subprocess
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=env)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    worktree = tmp_path / "fake-worktree"
    worktree.mkdir()

    warnings = mgr._copy_bind_files("r", cfg.repos["r"], worktree)
    assert warnings == []  # No warning: globs that match nothing are silent.


def test_copy_bind_files_preserves_permissions(tmp_path: Path):
    import os, stat, subprocess
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    repo = _init_repo_with_ignored_files(tmp_path)
    # Make .env executable (weird but tests permission preservation).
    env_file = repo / ".env"
    env_file.chmod(env_file.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    bind_files:\n"
        "      - .env\n"
    )
    (repo / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    genv = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "Taskfile.yml"], cwd=repo, check=True, capture_output=True, env=genv)
    subprocess.run(["git", "commit", "-qm", "taskfile"], cwd=repo, check=True, capture_output=True, env=genv)

    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg, graph=None, state_manager=None,
        git=GitRunner(), shell=ShellRunner(), log=None,
    )
    worktree = tmp_path / "fake-worktree"
    worktree.mkdir()
    mgr._copy_bind_files("r", cfg.repos["r"], worktree)

    src_mode = (repo / ".env").stat().st_mode
    dst_mode = (worktree / ".env").stat().st_mode
    assert stat.S_IMODE(src_mode) == stat.S_IMODE(dst_mode)
```

Run: `uv run pytest tests/core/test_worktree.py -k copy_bind_files -v`
Expected: 4 FAIL — `_copy_bind_files` not defined.

- [ ] **Step 4.2: Implement `_copy_bind_files`**

In `src/mship/core/worktree.py`, add this method on `WorktreeManager` (place it right after `_match_bind_patterns`):

```python
    def _copy_bind_files(
        self,
        repo_name: str,
        repo_config,
        worktree_path: Path,
    ) -> list[str]:
        """Copy bind_files matches from source repo into the worktree.

        Returns warnings (non-fatal). Matches `symlink_dirs`'s warnings style
        so spawn's existing warnings-surface handles display.
        """
        warnings: list[str] = []
        if not repo_config.bind_files:
            return warnings

        # Resolve source root (mirror _create_symlinks logic for git_root repos).
        if repo_config.git_root is not None:
            parent = self._config.repos[repo_config.git_root]
            source_root = parent.path / repo_config.path
        else:
            source_root = repo_config.path

        # Warn on missing literals (no glob chars) before running the enum.
        for entry in repo_config.bind_files:
            if any(c in entry for c in "*?["):
                continue   # it's a glob; zero-match handled silently below
            if not (source_root / entry).exists():
                warnings.append(
                    f"{repo_name}: bind_files source missing: {entry} (will not be copied)"
                )

        candidates = self._git_ignored_files(source_root)
        matches = self._match_bind_patterns(repo_config.bind_files, candidates)

        for rel in matches:
            src = source_root / rel
            dst = worktree_path / rel

            if not src.is_file():
                # Glob matched a directory or a broken symlink. Skip + warn.
                warnings.append(
                    f"{repo_name}: bind_files match is not a regular file: {rel} (skipped)"
                )
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(src, dst)

        return warnings
```

Run: `uv run pytest tests/core/test_worktree.py -k copy_bind_files -v`
Expected: 4 PASS.

- [ ] **Step 4.3: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat(worktree): _copy_bind_files — snapshot gitignored matches into worktree"
mship journal "added _copy_bind_files; 4/4 integration tests (happy path, missing literal, zero-match, perms)" --action committed
```

---

## Task 5: Wire `_copy_bind_files` into spawn

**Files:**
- Modify: `src/mship/core/worktree.py` (spawn loop — both branches)
- Modify: `tests/core/test_worktree.py` (end-to-end spawn test)

- [ ] **Step 5.1: Write failing end-to-end test**

Append to `tests/core/test_worktree.py`:

```python
def test_spawn_copies_bind_files_and_coexists_with_symlink_dirs(tmp_path: Path):
    """Regression: bind_files and symlink_dirs run in the same spawn without interfering."""
    import os, subprocess
    from mship.core.config import ConfigLoader
    from mship.core.worktree import WorktreeManager
    from mship.core.graph import DependencyGraph
    from mship.core.state import StateManager
    from mship.core.log import LogManager
    from mship.util.shell import ShellRunner
    from mship.util.git import GitRunner

    # Bare origin + working clone with .gitignore, .env, one tracked file, node_modules/ dir.
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    clone = tmp_path / "repo"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (clone / ".gitignore").write_text(".env\nnode_modules/\n")
    (clone / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    (clone / ".env").write_text("secret=1\n")
    (clone / "node_modules").mkdir()
    (clone / "node_modules" / "pkg.txt").write_text("pkg\n")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", "init"], check=True, capture_output=True, env=env)
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True, capture_output=True)

    (tmp_path / "mothership.yaml").write_text(
        "workspace: t\n"
        "repos:\n"
        "  r:\n"
        "    path: ./repo\n"
        "    type: service\n"
        "    symlink_dirs: [node_modules]\n"
        "    bind_files: [.env]\n"
    )
    state_dir = tmp_path / ".mothership"
    state_dir.mkdir()
    cfg = ConfigLoader.load(tmp_path / "mothership.yaml")
    mgr = WorktreeManager(
        config=cfg,
        graph=DependencyGraph(config=cfg),
        state_manager=StateManager(state_dir=state_dir),
        git=GitRunner(),
        shell=ShellRunner(),
        log=LogManager(logs_dir=state_dir / "logs"),
    )
    result = mgr.spawn(description="add labels", skip_setup=True)
    wt = result.worktrees["r"]

    # bind_files: .env is copied byte-identical.
    assert (wt / ".env").read_text() == "secret=1\n"
    # symlink_dirs: node_modules is a symlink, not a copy.
    assert (wt / "node_modules").is_symlink()
    # Both succeeded with no warnings.
    bind_warnings = [w for w in result.setup_warnings if "bind_files" in w]
    assert bind_warnings == [], f"unexpected bind_files warnings: {bind_warnings}"
```

Run: `uv run pytest tests/core/test_worktree.py -k spawn_copies_bind_files -v`
Expected: FAIL — spawn doesn't call `_copy_bind_files` yet.

- [ ] **Step 5.2: Wire `_copy_bind_files` into spawn (both branches)**

In `src/mship/core/worktree.py`, there are two integration sites today that call `_create_symlinks`. Add a `_copy_bind_files` call immediately after each.

Around **line 121** (the `git_root` branch), currently:

```python
                symlink_warnings = self._create_symlinks(repo_name, repo_config, effective)
                setup_warnings.extend(symlink_warnings)
```

Change to:

```python
                symlink_warnings = self._create_symlinks(repo_name, repo_config, effective)
                setup_warnings.extend(symlink_warnings)
                bind_warnings = self._copy_bind_files(repo_name, repo_config, effective)
                setup_warnings.extend(bind_warnings)
```

Around **line 154** (the normal-repo branch), currently:

```python
            symlink_warnings = self._create_symlinks(repo_name, repo_config, wt_path)
            setup_warnings.extend(symlink_warnings)
```

Change to:

```python
            symlink_warnings = self._create_symlinks(repo_name, repo_config, wt_path)
            setup_warnings.extend(symlink_warnings)
            bind_warnings = self._copy_bind_files(repo_name, repo_config, wt_path)
            setup_warnings.extend(bind_warnings)
```

Run: `uv run pytest tests/core/test_worktree.py -k spawn_copies_bind_files -v`
Expected: PASS.

- [ ] **Step 5.3: Full test suite**

Run: `uv run pytest -x -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 5.4: Commit**

```bash
git add src/mship/core/worktree.py tests/core/test_worktree.py
git commit -m "feat(spawn): copy bind_files after symlink step in both spawn branches"
mship journal "wired _copy_bind_files into spawn's git_root and normal branches; regression test coexists with symlink_dirs" --action committed
```

---

## Task 6: Manual smoke test

**No files modified. Validates end-to-end UX.**

- [ ] **Step 6.1: Smoke in a scratch workspace**

```bash
cd /tmp && rm -rf bind-files-smoke && mkdir bind-files-smoke && cd bind-files-smoke
git init --bare -b main -q origin.git
git init -b main -q repo && cd repo
git remote add origin ../origin.git
git config user.email t@t && git config user.name t
cat > .gitignore <<'EOF'
.env
apps/*/.env
.venv/
EOF
mkdir -p apps/foo apps/bar
cat > Taskfile.yml <<'EOF'
version: '3'
tasks: {}
EOF
cat > .env <<'EOF'
ROOT=1
EOF
cat > apps/foo/.env <<'EOF'
FOO=1
EOF
cat > apps/bar/.env <<'EOF'
BAR=1
EOF
mkdir .venv && touch .venv/should-not-copy
git add .gitignore Taskfile.yml apps/foo/.gitkeep 2>/dev/null; touch apps/foo/.gitkeep apps/bar/.gitkeep
git add -A && git commit -qm init && git push -q -u origin main

cd ..
cat > mothership.yaml <<'EOF'
workspace: smoke
repos:
  app:
    path: ./repo
    type: service
    bind_files:
      - .env
      - apps/**/.env
EOF

WT_PROJ=/home/bailey/development/repos/mothership/.worktrees/feat/bindfiles-per-worktree-file-copy-on-spawn-39
uv run --project "$WT_PROJ" mship spawn "test bind" 2>&1 | tail -10

WT=$(uv run --project "$WT_PROJ" mship status | jq -r '.worktrees.app')
echo "Worktree: $WT"
echo "--- contents ---"
ls -la "$WT/.env" "$WT/apps/foo/.env" "$WT/apps/bar/.env" 2>&1
echo "--- should NOT exist ---"
ls "$WT/.venv" 2>&1 || echo "(correctly absent)"
```

Expected:
- `.env`, `apps/foo/.env`, `apps/bar/.env` all present in the worktree with content matching source.
- `.venv/` NOT present in the worktree.

- [ ] **Step 6.2: Cleanup**

```bash
cd /home/bailey/development/repos/mothership/.worktrees/feat/bindfiles-per-worktree-file-copy-on-spawn-39
rm -rf /tmp/bind-files-smoke
```

No commit.

---

## Task 7: Final verification + PR

- [ ] **Step 7.1: Spec coverage check**

| Spec section | Task(s) |
|---|---|
| Field `bind_files: list[str]` on RepoConfig | 1 |
| Validation (absolute path, `..` segment rejection at load) | 1 |
| `_git_ignored_files` using `git ls-files --others --ignored --exclude-standard` | 2 |
| `_match_bind_patterns` with `*`, `?`, `**` + dedup | 3 |
| `_copy_bind_files` with source-root resolution (incl. git_root case) + copy2 | 4 |
| Missing literal → warn, zero-match glob → silent | 4 (`test_copy_bind_files_warns_on_missing_literal`, `test_copy_bind_files_zero_glob_matches_silent`) |
| Permission preservation | 4 (`test_copy_bind_files_preserves_permissions`) |
| Spawn integration (both git_root and normal branches) | 5 |
| Coexistence with `symlink_dirs` | 5 (regression test) |
| `.venv/` / `node_modules/` contents NEVER copied even with `**` | 2 + 6 |

- [ ] **Step 7.2: Full pytest**

```bash
uv run pytest -x -q
```

Expected: all pass. Prior baseline + ~19 new tests (4 config validation, 1 `_git_ignored_files`, 8 `_match_bind_patterns`, 4 `_copy_bind_files`, 1 spawn regression, +1 any).

- [ ] **Step 7.3: Open the PR**

```bash
cat > /tmp/bind-files-body.md <<'EOF'
## Summary

New per-repo config `bind_files: list[str]` copies gitignored files from the source repo into the new worktree at the same relative path on `mship spawn`. Closes #39.

- Entries are literal paths or globs (`*`, `?`, `**`). Patterns match against the source's gitignored-leaf-file set (output of `git ls-files --others --ignored --exclude-standard`), which crucially excludes the contents of ignored directories — so `**/.env` does NOT sweep `.venv/`, `node_modules/`, or `.worktrees/`.
- Missing literals warn; zero-match globs silent (matches "this pattern may or may not apply to this repo" semantics).
- Absolute paths and `..` segments rejected at config load time.
- Integrates after `_create_symlinks` in spawn's lifecycle, in both the normal-repo and `git_root` branches. Preserves mtime + permissions via `shutil.copy2`.
- No interaction with `mship close` (worktree teardown removes the copies) and no refresh on `switch`/`sync` (snapshot-at-spawn).

## Test plan

- [x] Config validation: `bind_files` accepts relative paths + globs; rejects absolute paths; rejects `..` segments.
- [x] `_git_ignored_files` returns gitignored leaf files only; does NOT include `.venv/*` or `node_modules/*` contents (regression for the "`**` blast radius" concern).
- [x] `_match_bind_patterns` covers literals, `*`, `?`, `**`, multi-pattern dedup, empty-patterns, zero-matches-silent.
- [x] `_copy_bind_files` end-to-end: happy path, missing-literal warn, zero-match-glob silent, permission preservation.
- [x] Spawn integration regression: `symlink_dirs` + `bind_files` coexist without interference.
- [x] Manual smoke: scratch workspace with apps/foo/.env + apps/bar/.env + .venv/ verifies `**` pulls in the apps files and skips .venv.
- [x] Full pytest green.

## Design notes

Spec: `docs/superpowers/specs/2026-04-18-bind-files-design.md`.
Plan: `docs/superpowers/plans/2026-04-18-bind-files.md`.

Not in scope (separate follow-ups): symlink mode, `mship doctor` hint for unlisted gitignored files, documentation of `symlink_dirs` sharp edges.
EOF
mship finish --body-file /tmp/bind-files-body.md
rm /tmp/bind-files-body.md
```
