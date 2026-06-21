# mship capture — UI Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `ui-capture` (approved) — `specs/2026-06-19-ui-capture.md`.

**Goal:** Add `mship capture` — a lightweight iteration primitive that grabs the running UI's rendered state (screenshot + structured layout) into files an agent can read, via per-repo go-task delegation.

**Architecture:** mship resolves the task/worktree + platform, runs the repo's canonical `capture` go-task target with `MSHIP_CAPTURE_DIR`/`MSHIP_CAPTURE_PLATFORM`/`MSHIP_CAPTURE_KINDS` set (exactly like `mship test` delegates to `task test`), then discovers the produced artifacts by a kind→filename map and reports them. The repo's Taskfile owns the platform mechanics (adb/simctl), so mship stays env-agnostic. ground-control gets the Android + iOS backends.

**Tech Stack:** Python 3.14 / uv / pytest / Typer / Pydantic (mothership); Kotlin / JUnit4 / Gradle (ground-control). Two affected repos. Run tests with `mship test` per repo. Single Python test during TDD: `uv run --no-sync pytest <path>::<name> -q`.

**Worktrees:**
- mothership: `/home/bailey/development/repos/mship-workspace/.worktrees/ui-capture/mothership`
- ground-control: `/home/bailey/development/repos/mship-workspace/.worktrees/ui-capture/ground-control`

---

## The artifact contract (referenced by several tasks)

`mship capture` runs the repo's `capture` go-task target in the worktree, with these env vars set:
- `MSHIP_CAPTURE_DIR` — absolute output directory (mship creates it).
- `MSHIP_CAPTURE_PLATFORM` — e.g. `android` / `ios` (omitted if the repo declares no platforms).
- `MSHIP_CAPTURE_KINDS` — comma list of requested kinds, e.g. `image,layout`.

The target writes conventionally-named files into `$MSHIP_CAPTURE_DIR`:
- kind `image` → `screen.png`
- kind `layout` → `layout.xml` | `layout.json` | `layout.html` (first found wins)

mship verifies ≥1 requested-kind file exists and is non-empty, then reports `{kind, path}` for each.

## File Structure

- `src/mship/core/config.py` — **modify**: add `CaptureConfig` model + `capture` field on `RepoConfig`. (Task 1)
- `src/mship/core/capture.py` — **create**: pure `discover_artifacts` + `resolve_kinds` + orchestration `run_capture`. (Task 2)
- `src/mship/cli/capture.py` — **create**: the `mship capture` command. (Task 3)
- `src/mship/cli/__init__.py` — **modify**: register the capture module. (Task 3)
- `ground-control/Taskfile.yml` — **modify**: add the `capture` target (android + ios). (Task 4)
- `ground-control/android/app/src/test/java/com/atomikpanda/groundcontrol/CaptureTaskfileTest.kt` — **create**: guard test. (Task 4)
- `src/mship/skills/working-with-mothership/SKILL.md` — **modify**: document `mship capture`. (Task 5)
- Tests: `tests/core/test_config.py` (Task 1), `tests/core/test_capture.py` (Task 2, new), `tests/cli/test_capture.py` (Task 3, new), `tests/skills/test_capture_skill.py` (Task 5, new).
- **After tasks:** workspace `mothership.yaml` gains `capture.platforms: [android, ios]` for ground-control (coordinator-repo commit, not a worktree).

---

<!-- mship:task id=1 -->
### Task 1: `CaptureConfig` on the repo model

**Work in the mothership worktree.**

**Files:**
- Modify: `src/mship/core/config.py`
- Test: `tests/core/test_config.py` (append; create if absent)

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_config.py` (if the file doesn't exist, create it with `from mship.core.config import ConfigLoader` and a tmp-file helper modeled on existing config tests):

```python
def test_repo_capture_config_parses(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        "    capture:\n"
        "      platforms: [android, ios]\n"
    )
    config = ConfigLoader(cfg).load()
    assert config.repos["app"].capture is not None
    assert config.repos["app"].capture.platforms == ["android", "ios"]


def test_repo_without_capture_defaults_none(tmp_path):
    from mship.core.config import ConfigLoader
    cfg = tmp_path / "mothership.yaml"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
    )
    config = ConfigLoader(cfg).load()
    assert config.repos["app"].capture is None
```

Verify the `ConfigLoader` entrypoint name first by reading the top of `src/mship/core/config.py`; if the loader class/method differs, match the existing config-test pattern in the repo.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/core/test_config.py -k capture -q`
Expected: FAIL — `RepoConfig` has no `capture` attribute.

- [ ] **Step 3: Implement**

In `src/mship/core/config.py`, add a model above `RepoConfig`:

```python
class CaptureConfig(BaseModel):
    platforms: list[str] = []
```

Add the field to `RepoConfig` (next to `tasks`):

```python
    capture: CaptureConfig | None = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync pytest tests/core/test_config.py -k capture -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/config.py tests/core/test_config.py
git commit -m "feat(config): add per-repo CaptureConfig (platforms)"
mship journal "CaptureConfig model + tests; passing" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=2 -->
### Task 2: `core/capture.py` — kinds, artifact discovery, orchestration

**Work in the mothership worktree.**

**Files:**
- Create: `src/mship/core/capture.py`
- Test: `tests/core/test_capture.py` (new)

**Context:** Pure logic + a thin orchestration over an injected shell (the `ShellRunner` with `run_task(task_name, actual_task_name, cwd, env_runner, env) -> ShellResult(returncode, stdout, stderr)`).

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_capture.py`:

```python
"""Tests for mship.core.capture."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from mship.core.capture import (
    Artifact, CaptureError, resolve_kinds, discover_artifacts, run_capture,
)


def test_resolve_kinds_all_and_single():
    assert resolve_kinds("all") == ["image", "layout"]
    assert resolve_kinds("image") == ["image"]
    assert resolve_kinds("layout") == ["layout"]


def test_resolve_kinds_unknown_raises():
    with pytest.raises(CaptureError, match="unknown kind"):
        resolve_kinds("video")


def test_discover_artifacts_finds_image_and_layout(tmp_path):
    (tmp_path / "screen.png").write_bytes(b"\x89PNG fake")
    (tmp_path / "layout.xml").write_text("<hierarchy/>")
    arts = discover_artifacts(tmp_path, ["image", "layout"])
    kinds = {a.kind: a.path for a in arts}
    assert kinds["image"] == tmp_path / "screen.png"
    assert kinds["layout"] == tmp_path / "layout.xml"


def test_discover_artifacts_skips_empty_and_missing(tmp_path):
    (tmp_path / "screen.png").write_bytes(b"")          # empty -> ignored
    # no layout file at all
    assert discover_artifacts(tmp_path, ["image", "layout"]) == []


@dataclass
class _FakeResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class _FakeShell:
    """Records run_task calls; optionally writes a file to simulate the target."""
    def __init__(self, returncode=0, writes: dict[str, bytes] | None = None, stderr=""):
        self.returncode = returncode
        self.writes = writes or {}
        self.stderr = stderr
        self.calls = []

    def run_task(self, task_name, actual_task_name, cwd, env_runner=None, env=None):
        self.calls.append(dict(task_name=task_name, actual=actual_task_name, cwd=cwd, env_runner=env_runner, env=env))
        out = Path(env["MSHIP_CAPTURE_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        for name, data in self.writes.items():
            (out / name).write_bytes(data)
        return _FakeResult(returncode=self.returncode, stderr=self.stderr)


def test_run_capture_success_returns_artifacts(tmp_path):
    shell = _FakeShell(returncode=0, writes={"screen.png": b"PNGDATA"})
    arts = run_capture(
        shell=shell, worktree=tmp_path / "wt", actual_task_name="capture",
        env_runner=None, platform="android", kinds=["image"], out_dir=tmp_path / "out",
    )
    assert [a.kind for a in arts] == ["image"]
    # env contract
    env = shell.calls[0]["env"]
    assert env["MSHIP_CAPTURE_PLATFORM"] == "android"
    assert env["MSHIP_CAPTURE_KINDS"] == "image"
    assert env["MSHIP_CAPTURE_DIR"] == str(tmp_path / "out")
    assert shell.calls[0]["actual"] == "capture"


def test_run_capture_target_failure_raises_with_stderr(tmp_path):
    shell = _FakeShell(returncode=1, stderr="adb: no devices")
    with pytest.raises(CaptureError, match="adb: no devices"):
        run_capture(
            shell=shell, worktree=tmp_path, actual_task_name="capture",
            env_runner=None, platform="android", kinds=["image"], out_dir=tmp_path / "o",
        )


def test_run_capture_no_artifact_raises(tmp_path):
    shell = _FakeShell(returncode=0, writes={})  # target "succeeds" but writes nothing
    with pytest.raises(CaptureError, match="no recognized artifact"):
        run_capture(
            shell=shell, worktree=tmp_path, actual_task_name="capture",
            env_runner=None, platform=None, kinds=["image"], out_dir=tmp_path / "o",
        )


def test_run_capture_omits_platform_env_when_none(tmp_path):
    shell = _FakeShell(returncode=0, writes={"screen.png": b"x"})
    run_capture(
        shell=shell, worktree=tmp_path, actual_task_name="capture",
        env_runner=None, platform=None, kinds=["image"], out_dir=tmp_path / "o",
    )
    assert "MSHIP_CAPTURE_PLATFORM" not in shell.calls[0]["env"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/core/test_capture.py -q`
Expected: FAIL — `mship.core.capture` does not exist.

- [ ] **Step 3: Implement**

Create `src/mship/core/capture.py`:

```python
"""Pure logic + thin orchestration for `mship capture`.

mship delegates the actual platform capture to a per-repo go-task `capture`
target (adb/simctl/etc.) and only understands the artifact contract: the
target writes conventionally-named files into MSHIP_CAPTURE_DIR; this module
discovers them by a kind->filename map and validates at least one was produced.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class CaptureError(Exception):
    """Capture could not be completed (target failed, or produced no artifact)."""


# kind -> candidate filenames (first existing non-empty file wins per kind)
KIND_FILENAMES: dict[str, tuple[str, ...]] = {
    "image": ("screen.png",),
    "layout": ("layout.xml", "layout.json", "layout.html"),
}
ALL_KINDS: tuple[str, ...] = tuple(KIND_FILENAMES)


@dataclass(frozen=True)
class Artifact:
    kind: str
    path: Path


def resolve_kinds(kind_flag: str) -> list[str]:
    """Map the --kind value ('all' | a single kind) to a concrete list."""
    if kind_flag == "all":
        return list(ALL_KINDS)
    if kind_flag not in KIND_FILENAMES:
        raise CaptureError(
            f"unknown kind {kind_flag!r}; expected one of: "
            f"{', '.join(ALL_KINDS)} or 'all'"
        )
    return [kind_flag]


def discover_artifacts(out_dir: Path, kinds: list[str]) -> list[Artifact]:
    """Return the non-empty artifact file produced for each requested kind."""
    found: list[Artifact] = []
    for kind in kinds:
        for name in KIND_FILENAMES[kind]:
            p = out_dir / name
            if p.is_file() and p.stat().st_size > 0:
                found.append(Artifact(kind=kind, path=p))
                break
    return found


def run_capture(
    *,
    shell,
    worktree: Path,
    actual_task_name: str,
    env_runner: str | None,
    platform: str | None,
    kinds: list[str],
    out_dir: Path,
) -> list[Artifact]:
    """Run the repo's capture target, then discover + validate artifacts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "MSHIP_CAPTURE_DIR": str(out_dir),
        "MSHIP_CAPTURE_KINDS": ",".join(kinds),
    }
    if platform is not None:
        env["MSHIP_CAPTURE_PLATFORM"] = platform

    result = shell.run_task(
        "capture", actual_task_name, cwd=worktree, env_runner=env_runner, env=env
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise CaptureError(
            f"capture target failed (exit {result.returncode}):\n{tail}"
        )

    artifacts = discover_artifacts(out_dir, kinds)
    if not artifacts:
        tail = (result.stderr or "").strip()[-2000:]
        raise CaptureError(
            f"capture target produced no recognized artifact in {out_dir} "
            f"for kinds {kinds}."
            + (f" target stderr:\n{tail}" if tail else "")
        )
    return artifacts
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync pytest tests/core/test_capture.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mship/core/capture.py tests/core/test_capture.py
git commit -m "feat(capture): core artifact contract (resolve_kinds/discover/run_capture)"
mship journal "core/capture.py + tests; passing" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=3 -->
### Task 3: `mship capture` CLI command

**Work in the mothership worktree.** Depends on Tasks 1 & 2.

**Files:**
- Create: `src/mship/cli/capture.py`
- Modify: `src/mship/cli/__init__.py` (register the module)
- Test: `tests/cli/test_capture.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_capture.py`:

```python
"""Tests for `mship capture` CLI."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

from typer.testing import CliRunner

from mship.cli import app, container
from mship.core.state import StateManager, Task, WorkspaceState

runner = CliRunner()


def _bootstrap(tmp_path: Path, platforms: list[str]):
    state_dir = tmp_path / ".mothership"; state_dir.mkdir()
    wt = tmp_path / "wt"; wt.mkdir()
    cfg = tmp_path / "mothership.yaml"
    plat = "[" + ", ".join(platforms) + "]" if platforms else "[]"
    cfg.write_text(
        "workspace: t\n"
        "repos:\n"
        "  app:\n"
        "    path: ./app\n"
        "    type: service\n"
        f"    capture:\n      platforms: {plat}\n"
    )
    task = Task(
        slug="t", description="d", phase="dev",
        created_at=datetime.now(timezone.utc),
        affected_repos=["app"], worktrees={"app": str(wt)}, branch="feat/t",
        base_branch="main", active_repo="app",
    )
    StateManager(state_dir).save(WorkspaceState(tasks={"t": task}))
    return cfg, state_dir, wt


def _override(cfg, state_dir, shell):
    container.config.reset(); container.state_manager.reset()
    container.config_path.override(cfg)
    container.state_dir.override(state_dir)
    container.shell.override(shell)


def _reset():
    container.config_path.reset_override()
    container.state_dir.reset_override()
    container.config.reset_override(); container.config.reset()
    container.state_manager.reset_override(); container.state_manager.reset()
    container.shell.reset_override()


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode; self.stdout = stdout; self.stderr = stderr


class _FakeShell:
    """Writes screen.png into MSHIP_CAPTURE_DIR to simulate a successful target."""
    def __init__(self, returncode=0, stderr="", write_image=True):
        self.returncode = returncode; self.stderr = stderr; self.write_image = write_image
        self.calls = []

    def run_task(self, task_name, actual_task_name, cwd, env_runner=None, env=None):
        self.calls.append(env)
        out = Path(env["MSHIP_CAPTURE_DIR"]); out.mkdir(parents=True, exist_ok=True)
        if self.write_image:
            (out / "screen.png").write_bytes(b"PNGDATA")
        return _FakeResult(returncode=self.returncode, stderr=self.stderr)


def test_capture_single_platform_implicit(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android"])
    shell = _FakeShell()
    _override(cfg, state_dir, shell)
    try:
        result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["platform"] == "android"
        assert payload["artifacts"][0]["kind"] == "image"
        assert shell.calls[0]["MSHIP_CAPTURE_PLATFORM"] == "android"
    finally:
        _reset()


def test_capture_requires_platform_when_multiple(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android", "ios"])
    shell = _FakeShell()
    _override(cfg, state_dir, shell)
    try:
        result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app"])
        assert result.exit_code != 0
        assert "--platform is required" in result.output
        assert "android" in result.output and "ios" in result.output
    finally:
        _reset()


def test_capture_explicit_platform_and_out(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android", "ios"])
    shell = _FakeShell()
    out = tmp_path / "shots"
    _override(cfg, state_dir, shell)
    try:
        result = runner.invoke(
            app, ["capture", "--task", "t", "--repo", "app", "--platform", "ios", "--out", str(out)]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["platform"] == "ios"
        assert payload["artifacts"][0]["path"] == str(out / "screen.png")
    finally:
        _reset()


def test_capture_unknown_platform_errors(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android", "ios"])
    _override(cfg, state_dir, _FakeShell())
    try:
        result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app", "--platform", "web"])
        assert result.exit_code != 0
        assert "unknown platform" in result.output
    finally:
        _reset()


def test_capture_target_failure_surfaces_stderr(tmp_path):
    cfg, state_dir, wt = _bootstrap(tmp_path, ["android"])
    _override(cfg, state_dir, _FakeShell(returncode=1, stderr="adb: device offline", write_image=False))
    try:
        result = runner.invoke(app, ["capture", "--task", "t", "--repo", "app"])
        assert result.exit_code != 0
        assert "adb: device offline" in result.output
    finally:
        _reset()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/cli/test_capture.py -q`
Expected: FAIL — no `capture` command registered.

- [ ] **Step 3: Implement the command**

Create `src/mship/cli/capture.py`:

```python
"""`mship capture` — capture the running UI for an agent to inspect."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from mship.cli._resolve import resolve_for_command
from mship.cli.output import Output
from mship.core import capture as _cap
from mship.core.dispatch import resolve_repo


def register(app: typer.Typer, get_container):
    @app.command()
    def capture(
        task: Optional[str] = typer.Option(None, "--task", help="Target task slug (defaults to cwd-resolved)."),
        repo: Optional[str] = typer.Option(None, "--repo", help="Which repo's worktree to capture (multi-repo tasks)."),
        platform: Optional[str] = typer.Option(None, "--platform", help="Platform to capture (required when the repo exposes more than one)."),
        kind: str = typer.Option("all", "--kind", help="Artifact kind: image | layout | all."),
        out: Optional[Path] = typer.Option(None, "--out", help="Output directory (default: .mothership/captures/<task>/<ts>-<platform>/)."),
    ):
        """Capture the running UI (screenshot + layout) into files to read."""
        output = Output()
        container = get_container()
        state = container.state_manager().load()
        resolved = resolve_for_command("capture", state, task, output)
        t = resolved.task

        try:
            resolved_repo = resolve_repo(t, repo)
        except ValueError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        try:
            kinds = _cap.resolve_kinds(kind)
        except _cap.CaptureError as e:
            output.error(str(e))
            raise typer.Exit(code=2)

        config = container.config()
        repo_cfg = config.repos[resolved_repo]
        platforms = repo_cfg.capture.platforms if repo_cfg.capture else []

        resolved_platform = platform
        if resolved_platform is None:
            if len(platforms) == 1:
                resolved_platform = platforms[0]
            elif len(platforms) > 1:
                output.error(
                    f"--platform is required for repo {resolved_repo!r}; "
                    f"choose one of: {', '.join(platforms)}."
                )
                raise typer.Exit(code=2)
            # else: no platforms declared -> leave unset
        elif platforms and resolved_platform not in platforms:
            output.error(
                f"unknown platform {resolved_platform!r} for repo {resolved_repo!r}; "
                f"choose one of: {', '.join(platforms)}."
            )
            raise typer.Exit(code=2)

        actual = repo_cfg.tasks.get("capture", "capture")
        worktree = Path(t.worktrees[resolved_repo])

        if out is not None:
            out_dir = out
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            workspace_root = Path(container.config_path()).parent
            label = resolved_platform or "default"
            out_dir = workspace_root / ".mothership" / "captures" / t.slug / f"{ts}-{label}"

        try:
            artifacts = _cap.run_capture(
                shell=container.shell(),
                worktree=worktree,
                actual_task_name=actual,
                env_runner=repo_cfg.env_runner,
                platform=resolved_platform,
                kinds=kinds,
                out_dir=out_dir,
            )
        except _cap.CaptureError as e:
            output.error(str(e))
            raise typer.Exit(code=1)

        if output.is_tty:
            for a in artifacts:
                output.success(f"captured {a.kind} → {a.path}")
        else:
            output.json({
                "platform": resolved_platform,
                "repo": resolved_repo,
                "artifacts": [{"kind": a.kind, "path": str(a.path)} for a in artifacts],
                "resolved_task": resolved.task.slug,
                "resolution_source": resolved.source,
            })
```

- [ ] **Step 4: Register the command**

In `src/mship/cli/__init__.py`, add the import alongside the other `from mship.cli import … as …` lines:

```python
from mship.cli import capture as _capture_mod
```

and the registration alongside the other `_*_mod.register(app, get_container)` calls:

```python
_capture_mod.register(app, get_container)
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run --no-sync pytest tests/cli/test_capture.py -q`
Expected: PASS (5 passed). Also confirm the CLI imports: `uv run --no-sync python -c "from mship.cli import app"`.

- [ ] **Step 6: Commit**

```bash
git add src/mship/cli/capture.py src/mship/cli/__init__.py tests/cli/test_capture.py
git commit -m "feat(capture): mship capture command (platform/kind/out + go-task delegation)"
mship journal "mship capture CLI + tests; passing" --action committed --test-state pass
```
<!-- /mship:task -->

<!-- mship:task id=4 -->
### Task 4: ground-control capture backends (Android + iOS) + guard test

**Work in the ground-control worktree** (`.worktrees/ui-capture/ground-control`).

**Files:**
- Modify: `Taskfile.yml`
- Test: `android/app/src/test/java/com/atomikpanda/groundcontrol/CaptureTaskfileTest.kt` (new)

**Context:** ground-control is one repo with `android`/`ios` subdirs. A single `capture` target switches on `MSHIP_CAPTURE_PLATFORM` and writes into `MSHIP_CAPTURE_DIR`. iOS layout is unsupported (no clean simctl hierarchy dump) — image only. The guard test runs under the JVM suite (`./gradlew testDebugUnitTest`, captured by `mship test`) and needs no device.

- [ ] **Step 1: Write the failing guard test**

Create `android/app/src/test/java/com/atomikpanda/groundcontrol/CaptureTaskfileTest.kt`:

```kotlin
package com.atomikpanda.groundcontrol

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** Guards the `capture` go-task target (image+layout) against drift. No device. */
class CaptureTaskfileTest {

    private fun findTaskfile(): File {
        var dir: File? = File(System.getProperty("user.dir")).absoluteFile
        while (dir != null) {
            val f = File(dir, "Taskfile.yml")
            if (f.isFile) return f
            dir = dir.parentFile
        }
        throw AssertionError(
            "no Taskfile.yml found upward from ${System.getProperty("user.dir")}"
        )
    }

    @Test
    fun capture_target_has_expected_commands() {
        val text = findTaskfile().readText()
        assertTrue("has a capture target", text.contains("capture:"))
        assertTrue("writes to MSHIP_CAPTURE_DIR", text.contains("MSHIP_CAPTURE_DIR"))
        assertTrue("android screenshot via screencap", text.contains("screencap"))
        assertTrue("android layout via uiautomator dump", text.contains("uiautomator dump"))
        assertTrue("ios screenshot via simctl", text.contains("simctl io booted screenshot"))
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run (from the ground-control worktree): `mship test --task ui-capture --repos ground-control`
Expected: FAIL — `CaptureTaskfileTest` fails its assertions (no `capture` target yet). (If you want a tighter loop: `cd android && ./gradlew testDebugUnitTest --tests '*CaptureTaskfileTest*'`.)

- [ ] **Step 3: Add the capture target**

In `ground-control/Taskfile.yml`, add this target (note it is NOT pinned to `dir: android` — it runs from the repo root and shells out to adb/simctl):

```yaml
  capture:
    desc: >
      Capture UI artifacts into $MSHIP_CAPTURE_DIR. Honors MSHIP_CAPTURE_PLATFORM
      (android|ios). Writes screen.png (image) and, on android, layout.xml (layout).
      Assumes the app is already running (see `task run`); does not boot devices.
    cmds:
      - 'mkdir -p "$MSHIP_CAPTURE_DIR"'
      - |
        case "${MSHIP_CAPTURE_PLATFORM:-android}" in
          android)
            adb exec-out screencap -p > "$MSHIP_CAPTURE_DIR/screen.png"
            adb shell uiautomator dump /sdcard/window_dump.xml >/dev/null \
              && adb pull /sdcard/window_dump.xml "$MSHIP_CAPTURE_DIR/layout.xml" >/dev/null
            ;;
          ios)
            xcrun simctl io booted screenshot "$MSHIP_CAPTURE_DIR/screen.png"
            ;;
          *)
            echo "unsupported MSHIP_CAPTURE_PLATFORM: ${MSHIP_CAPTURE_PLATFORM}" >&2
            exit 2
            ;;
        esac
```

(The Android branch always produces both image + layout; mship reports only the requested kinds. This keeps the target simple while honoring the contract. iOS produces image only, which the bundle model handles.)

- [ ] **Step 4: Run to verify it passes**

Run: `mship test --task ui-capture --repos ground-control`
Expected: PASS — `CaptureTaskfileTest` green (and existing tests still pass).

- [ ] **Step 5: Commit**

```bash
git add Taskfile.yml android/app/src/test/java/com/atomikpanda/groundcontrol/CaptureTaskfileTest.kt
git commit -m "feat(capture): ground-control capture target (android+ios) + guard test"
mship journal "ground-control capture target + Kotlin guard test; passing" --action committed --test-state pass --repo ground-control
```
<!-- /mship:task -->

<!-- mship:task id=5 -->
### Task 5: Document `mship capture` in the working-with-mothership skill

**Work in the mothership worktree.**

**Files:**
- Modify: `src/mship/skills/working-with-mothership/SKILL.md`
- Test: `tests/skills/test_capture_skill.py` (new)

- [ ] **Step 1: Write the failing guard test**

Create `tests/skills/test_capture_skill.py`:

```python
"""Guard: working-with-mothership documents mship capture."""
from __future__ import annotations

from mship.core.skill_install import pkg_skills_source


def test_working_with_mothership_documents_capture():
    text = (pkg_skills_source() / "working-with-mothership" / "SKILL.md").read_text()
    assert "mship capture" in text
    # framed as UI self-verification alongside tests
    assert "capture" in text.lower() and "ui" in text.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync pytest tests/skills/test_capture_skill.py -q`
Expected: FAIL — the skill doesn't mention `mship capture` yet.

- [ ] **Step 3: Document it**

In `src/mship/skills/working-with-mothership/SKILL.md`, add `mship capture` to the command reference under the "Working on a task" group (near `mship test`):

```markdown
mship capture [--repo R] [--platform P] [--kind image|layout|all] [--out DIR]
```

And add a short note in the same section:

```markdown
**`capture` is the UI analog of `test`.** For UI work (mobile screens, web), run
`mship capture` to grab the running app's rendered state — a screenshot (`image`)
and/or a structured layout dump (`layout`) — into files you can read, then compare
against intent and iterate. It delegates to the repo's `capture` go-task target
(adb/simctl/etc.), so the app must already be running (`mship run`); it does not
boot emulators/simulators. Use `--platform` when a repo targets more than one.
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync pytest tests/skills/test_capture_skill.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mship/skills/working-with-mothership/SKILL.md tests/skills/test_capture_skill.py
git commit -m "docs(skills): document mship capture in working-with-mothership"
mship journal "skill doc for mship capture + guard; passing" --action committed --test-state pass
```
<!-- /mship:task -->

---

## After all tasks

- [ ] Full suites green: `mship test --task ui-capture` (both repos).
- [ ] **Workspace config (coordinator repo, not a worktree):** from the workspace root `/home/bailey/development/repos/mship-workspace`, add ground-control's platforms to the live `mothership.yaml`:
  ```yaml
  repos:
    ground-control:
      # …existing keys…
      capture:
        platforms: [android, ios]
  ```
  Commit it to the workspace coordinator repo (this enables real `mship capture --repo ground-control` once mship is upgraded). This is the same pattern as the earlier `default_remote` workspace commit.
- [ ] `mship phase review` → write a PR body (Summary + Test plan across the 5 tasks, both repos) → `mship finish --body-file <path> --require-tests`.
- [ ] After merge: `mship close`; mark spec `implemented` (`mship spec implemented ui-capture`).

## Self-Review (completed by plan author)

- **Spec coverage:** ac1→Tasks 2+3 (env contract + delegation); ac2→Tasks 2+3 (bundle discovery + JSON/TTY output); ac3→Task 3 (`--kind`/`--out`/default dir); ac4→Task 3 (platform required when >1, no hard default) + Task 1 (`capture.platforms`); ac5→Tasks 2+3 (error paths); ac6→Task 4 (ground-control targets + guard); ac7→Task 5 (skill doc). All seven covered.
- **Placeholder scan:** every code step shows real code and a runnable command with expected output; no TBD/TODO.
- **Type consistency:** `Artifact{kind,path}`, `resolve_kinds`, `discover_artifacts`, `run_capture(shell, worktree, actual_task_name, env_runner, platform, kinds, out_dir)` defined in Task 2 and called identically in Task 3; `CaptureConfig.platforms` defined in Task 1 and read in Task 3; the env var names (`MSHIP_CAPTURE_DIR/PLATFORM/KINDS`) and artifact filenames (`screen.png`, `layout.xml`) match between core (Task 2), the ground-control target (Task 4), and the guard test (Task 4).
