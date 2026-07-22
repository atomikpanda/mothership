import os
import subprocess
from pathlib import Path

from mship.core.bootstrap import bootstrap
from mship.util.shell import ShellRunner


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _make_source_repo(root: Path) -> Path:
    """A real git repo with a Taskfile.yml, usable as a file:// clone source."""
    src = root / "src-lib"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    (src / "Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
    _git(["add", "."], src)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], src)
    return src


def _workspace(root: Path, url: str, *, member="lib") -> Path:
    ws = root / "ws"
    ws.mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\n"
        "repos:\n"
        f"  {member}:\n"
        f"    path: {member}\n"
        "    type: library\n"
        f"    url: {url}\n"
    )
    (ws / ".mothership").mkdir()
    return ws


def test_bootstrap_clones_missing_member(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}")
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    statuses = {m.name: m.status for m in report.members}
    assert statuses["lib"] == "cloned"
    assert (ws / "lib" / "Taskfile.yml").exists()
    assert not report.has_errors


def test_bootstrap_skips_existing_dir(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}")
    (ws / "lib").mkdir()  # already present
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    assert {m.name: m.status for m in report.members}["lib"] == "present"


def test_bootstrap_skips_symlink(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}")
    os.symlink(src, ws / "lib")  # symlink must never be re-pointed
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    assert {m.name: m.status for m in report.members}["lib"] == "present"
    assert (ws / "lib").is_symlink()  # untouched


def test_bootstrap_unresolvable_url_errors_but_others_proceed(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = tmp_path / "ws2"
    ws.mkdir()
    (ws / ".mothership").mkdir()
    # `bad` has no url and there is no default_remote -> unresolvable.
    # `lib` has a file:// url -> clones fine.
    (ws / "mothership.yaml").write_text(
        "workspace: w\n"
        "repos:\n"
        "  bad:\n    path: bad\n    type: library\n"
        f"  lib:\n    path: lib\n    type: library\n    url: file://{src}\n"
    )
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    statuses = {m.name: m.status for m in report.members}
    assert statuses["bad"] == "error"
    assert statuses["lib"] == "cloned"
    assert report.has_errors is True
    # doctor is skipped while a member errored
    assert report.doctor_ok is None


def test_bootstrap_repos_filter(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}", member="lib")
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership", repos=["lib"])
    assert [m.name for m in report.members] == ["lib"]


def test_bootstrap_skips_git_root_repos(tmp_path):
    # A git_root repo is a subdirectory of its parent's checkout — it is
    # materialized when the parent is cloned, never cloned independently.
    src = _make_source_repo(tmp_path)
    ws = tmp_path / "wsg"
    ws.mkdir()
    (ws / ".mothership").mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\n"
        "repos:\n"
        f"  svc:\n    path: svc\n    type: service\n    url: file://{src}\n"
        "  svc_sub:\n    path: sub\n    type: library\n    git_root: svc\n"
    )
    report = bootstrap(ws / "mothership.yaml", ShellRunner(),
                       state_dir=ws / ".mothership")
    names = {m.name for m in report.members}
    assert "svc" in names              # parent is cloned
    assert "svc_sub" not in names      # git_root subdir is skipped, not cloned


def test_bootstrap_unknown_repo_filter_raises(tmp_path):
    src = _make_source_repo(tmp_path)
    ws = _workspace(tmp_path, f"file://{src}", member="lib")
    import pytest
    with pytest.raises(ValueError, match="Unknown repo"):
        bootstrap(ws / "mothership.yaml", ShellRunner(),
                  state_dir=ws / ".mothership", repos=["typo"])


def test_bootstrap_clone_includes_cred_args_when_token(tmp_path):
    from mship.core import bootstrap as bmod
    from mship.util.shell import ShellResult
    calls = []

    class FakeShell:
        def run(self, command, cwd, env=None):
            calls.append((command, env))
            return ShellResult(returncode=0, stdout="", stderr="")
        def run_task(self, *a, **k):
            return ShellResult(returncode=0, stdout="", stderr="")

    ws = tmp_path / "ws"; ws.mkdir(); (ws / ".mothership").mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
        "    url: https://github.com/o/lib\n"
    )
    bmod.bootstrap(ws / "mothership.yaml", FakeShell(),
                   state_dir=ws / ".mothership", token="tok123")
    cmd, env = next((c, e) for c, e in calls if "git" in c and "clone" in c)
    assert "credential.https://github.com.helper" in cmd
    assert "tok123" not in cmd                 # token never in argv
    assert env and env.get("MSHIP_GH_TOKEN") == "tok123"


def test_bootstrap_broker_pull_sends_owner_repo(tmp_path, monkeypatch):
    """bootstrap maps its short repo names to `owner/repo` slugs before the
    broker pull, so the folded serve can resolve the GitHub App installation."""
    from mship.core import bootstrap as bmod
    from mship.util.shell import ShellResult

    captured = {}

    def fake_resolve_token(token, *, broker_url=None, broker_bearer=None,
                           repos=None, **kw):
        captured["repos"] = repos
        return None

    monkeypatch.setattr(bmod, "resolve_token", fake_resolve_token)

    class FakeShell:
        def run(self, command, cwd, env=None):
            return ShellResult(returncode=0, stdout="", stderr="")
        def run_task(self, *a, **k):
            return ShellResult(returncode=0, stdout="", stderr="")

    ws = tmp_path / "wsbrk"; ws.mkdir(); (ws / ".mothership").mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\n"
        "default_remote: https://github.com/acme\n"
        "repos:\n"
        "  widgets:\n    path: widgets\n    type: library\n"
        "  gadgets:\n    path: gadgets\n    type: service\n"
    )
    bmod.bootstrap(ws / "mothership.yaml", FakeShell(),
                   state_dir=ws / ".mothership")
    assert captured["repos"] == ["acme/widgets", "acme/gadgets"]


def test_bootstrap_broker_pull_falls_back_to_short_names(tmp_path, monkeypatch):
    """When no repo resolves to a github.com owner/repo (non-github/unresolvable
    url, or no default_remote), bootstrap falls back to the short names — Broker
    A ignores `repos`, so nothing breaks."""
    from mship.core import bootstrap as bmod
    from mship.util.shell import ShellResult

    captured = {}

    def fake_resolve_token(token, *, broker_url=None, broker_bearer=None,
                           repos=None, **kw):
        captured["repos"] = repos
        return None

    monkeypatch.setattr(bmod, "resolve_token", fake_resolve_token)

    class FakeShell:
        def run(self, command, cwd, env=None):
            return ShellResult(returncode=0, stdout="", stderr="")
        def run_task(self, *a, **k):
            return ShellResult(returncode=0, stdout="", stderr="")

    ws = tmp_path / "wsfb"; ws.mkdir(); (ws / ".mothership").mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
        "    url: file:///some/path\n"
    )
    bmod.bootstrap(ws / "mothership.yaml", FakeShell(),
                   state_dir=ws / ".mothership")
    assert captured["repos"] == ["lib"]


def test_bootstrap_clone_no_cred_args_without_token(tmp_path):
    from mship.core import bootstrap as bmod
    from mship.util.shell import ShellResult
    calls = []

    class FakeShell:
        def run(self, command, cwd, env=None):
            calls.append((command, env))
            return ShellResult(returncode=0, stdout="", stderr="")
        def run_task(self, *a, **k):
            return ShellResult(returncode=0, stdout="", stderr="")

    ws = tmp_path / "ws2"; ws.mkdir(); (ws / ".mothership").mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
        "    url: https://github.com/o/lib\n"
    )
    bmod.bootstrap(ws / "mothership.yaml", FakeShell(),
                   state_dir=ws / ".mothership")  # no token
    clone, clone_env = next((c, e) for c, e in calls if "git" in c and "clone" in c)
    assert "credential.https://github.com.helper" not in clone
    assert clone_env is None  # no token => no injected env at all


def test_bootstrap_relay_configures_git_then_clones_without_token(tmp_path, monkeypatch):
    """Relay mode: git config runs (globally) BEFORE the clone, resolve_token is
    never called, and the clone carries no credential args and no token env."""
    from mship.core import bootstrap as bmod
    from mship.util.shell import ShellResult

    def boom_resolve_token(*a, **k):
        raise AssertionError("resolve_token must not run in relay mode")

    monkeypatch.setattr(bmod, "resolve_token", boom_resolve_token)

    calls = []

    class FakeShell:
        def run(self, command, cwd, env=None):
            calls.append((command, env))
            return ShellResult(returncode=0, stdout="", stderr="")
        def run_task(self, *a, **k):
            return ShellResult(returncode=0, stdout="", stderr="")

    ws = tmp_path / "ws"; ws.mkdir(); (ws / ".mothership").mkdir()
    (ws / "mothership.yaml").write_text(
        "workspace: w\nrepos:\n  lib:\n    path: lib\n    type: library\n"
        "    url: https://github.com/o/lib\n"
    )
    bmod.bootstrap(ws / "mothership.yaml", FakeShell(),
                   state_dir=ws / ".mothership",
                   relay_url="https://relay.example", run_token="rt-123")

    commands = [c for c, _ in calls]
    cfg_idxs = [i for i, c in enumerate(commands) if "git config --global" in c]
    clone_idx = next(i for i, c in enumerate(commands) if "clone" in c)
    # All three config commands ran before the clone.
    assert len(cfg_idxs) == 3
    assert max(cfg_idxs) < clone_idx
    cfg = [commands[i] for i in cfg_idxs]
    assert any("url.https://relay.example/gh/.insteadOf" in c and "https://github.com/" in c for c in cfg)
    assert any("url.https://relay.example/api/.insteadOf" in c and "https://api.github.com/" in c for c in cfg)
    assert any("http.https://relay.example/.extraHeader" in c and "Mship-Run-Token: rt-123" in c for c in cfg)
    # Clone took the NO-token path.
    clone_cmd, clone_env = calls[clone_idx]
    assert "credential.https://github.com.helper" not in clone_cmd
    assert clone_env is None


def test_bootstrap_empty_repos_errors(tmp_path):
    # A workspace with no `repos:` loads since #259; bootstrapping it must error clearly, not
    # silently "succeed" with nothing cloned.
    import pytest
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "mothership.yaml").write_text("workspace: w\n")
    (ws / ".mothership").mkdir()
    with pytest.raises(ValueError, match="no repos configured"):
        bootstrap(ws / "mothership.yaml", ShellRunner(), state_dir=ws / ".mothership")
