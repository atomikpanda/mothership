"""Rendered units are PARSED, not substring-matched: systemd directives are
section-scoped and a directive in the wrong section is silently ignored (only
an "Unknown lvalue" journal line), so flat `in` assertions would pass CI while
the backoff policy is broken on the host."""
import configparser
import plistlib
import sys
from pathlib import Path

import pytest

import mship.core.daemon.units as units_mod
from mship.core.daemon.units import (
    DaemonExecResolutionError,
    render_launchd_plist,
    render_systemd_unit,
    resolve_mshipd_argv,
)

ARGV = ["/opt/venv/bin/mshipd"]


def _parse_unit(text: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read_string(text)
    return cp


def test_systemd_unit_sections():
    cp = _parse_unit(render_systemd_unit(ARGV))
    assert cp["Service"]["ExecStart"] == "/opt/venv/bin/mshipd"
    assert cp["Service"]["Restart"] == "on-failure"
    assert cp["Service"]["RestartSec"] == "5"
    assert cp["Unit"]["StartLimitIntervalSec"] == "300"
    assert cp["Unit"]["StartLimitBurst"] == "5"
    assert cp["Install"]["WantedBy"] == "default.target"
    # Loser exits 0 (run.py), so no SuccessExitStatus mapping exists — and none
    # exists for launchd anyway.
    assert "SuccessExitStatus" not in cp["Service"]


def test_systemd_unit_has_no_working_directory():
    """The workspace-agnostic assumption, made enforceable: a WorkingDirectory
    would bake one workspace into the one-per-host daemon."""
    text = render_systemd_unit(ARGV)
    assert "WorkingDirectory" not in text


def test_systemd_multiarg_exec_is_joined():
    cp = _parse_unit(render_systemd_unit(["/usr/bin/python3", "-m", "mship.core.daemon"]))
    assert cp["Service"]["ExecStart"] == "/usr/bin/python3 -m mship.core.daemon"


@pytest.fixture
def not_dev_tree(monkeypatch):
    """Resolution-rule tests simulate an installed tool: neutralize the
    dev-tree refusal (itself covered by test_dev_tree_refuses_install)."""
    monkeypatch.setattr(units_mod, "_running_from_dev_tree", lambda: None)


def test_dev_tree_refuses_install(monkeypatch):
    """Running from a checkout (editable mship / venv-in-checkout) must refuse
    BEFORE the sibling shortcut: the checkout venv has its own mshipd sibling,
    which would otherwise be persisted into the unit and defeat upgrades."""
    monkeypatch.setattr(units_mod, "_running_from_dev_tree", lambda: "editable checkout at /x")
    with pytest.raises(DaemonExecResolutionError, match="dev tree"):
        resolve_mshipd_argv(which=lambda name: None)


def test_dev_tree_detector_flags_editable_package(tmp_path, monkeypatch):
    """The real detector: package resolving outside sys.prefix = editable
    checkout (`uv run mship`)."""
    import mship as mship_pkg

    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    assert units_mod._running_from_dev_tree() is not None  # real pkg is outside tmp venv

    # Package inside the prefix and no adjacent checkout: not a dev tree.
    pkg_dir = tmp_path / "venv" / "lib" / "mship"
    pkg_dir.mkdir(parents=True)
    fake_file = pkg_dir / "__init__.py"
    fake_file.touch()
    monkeypatch.setattr(mship_pkg, "__file__", str(fake_file))
    assert units_mod._running_from_dev_tree() is None

    # Adjacent pyproject declaring mothership: venv-in-checkout, refuse.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "mothership"\n')
    assert units_mod._running_from_dev_tree() is not None


def test_execstart_resolves_sibling_first(tmp_path, monkeypatch, not_dev_tree):
    """Same venv bin dir ⇒ provably same dist (the uv-tool-install layout);
    sibling wins even when PATH has a different mshipd."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "python"
    exe.touch()
    sibling = bin_dir / "mshipd"
    sibling.touch()
    monkeypatch.setattr(sys, "executable", str(exe))
    argv = resolve_mshipd_argv(which=lambda name: "/somewhere/else/mshipd")
    assert argv == [str(sibling)]


def test_which_fallback_verified_against_sys_prefix(tmp_path, monkeypatch, not_dev_tree):
    prefix = tmp_path / "venv"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "python"
    exe.touch()
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "prefix", str(prefix))

    inside = bin_dir / "shims" / "mshipd"
    inside.parent.mkdir()
    inside.touch()
    assert resolve_mshipd_argv(which=lambda name: str(inside)) == [str(inside)]

    # A PATH shim from ANOTHER install (stale uv-tool shim while running
    # `uv run mship` from a checkout) must refuse, not silently bake a
    # worktree venv into a persistent unit.
    outside = tmp_path / "other" / "mshipd"
    outside.parent.mkdir()
    outside.touch()
    with pytest.raises(DaemonExecResolutionError, match="dev tree"):
        resolve_mshipd_argv(which=lambda name: str(outside))


def test_module_fallback_when_nothing_resolvable(tmp_path, monkeypatch, not_dev_tree):
    prefix = tmp_path / "venv"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "python"
    exe.touch()
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "prefix", str(prefix))
    argv = resolve_mshipd_argv(which=lambda name: None)
    assert argv == [str(exe), "-m", "mship.core.daemon"]


def test_launchd_plist_escapes_xml_special_chars(tmp_path: Path):
    """A path containing &, <, > must render a plist launchctl can parse."""
    log_dir = tmp_path / "logs & <special>"
    argv = [str(tmp_path / "venv & tools" / "bin" / "mshipd")]
    plist = plistlib.loads(render_launchd_plist(argv, log_dir).encode())
    assert plist["ProgramArguments"] == argv
    assert plist["StandardOutPath"] == str(log_dir / "launchd.out.log")


def test_dev_tree_detector_parses_toml_not_substrings(tmp_path, monkeypatch):
    """`name='mothership'` (single quotes / spacing variants) is still a
    checkout: a substring match would miss it and bake the checkout's mshipd
    into the unit, so upgrades would deploy nothing."""
    import mship as mship_pkg

    prefix = tmp_path / "checkout" / ".venv"
    (prefix / "lib" / "mship").mkdir(parents=True)
    fake = prefix / "lib" / "mship" / "__init__.py"
    fake.touch()
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(mship_pkg, "__file__", str(fake))

    for spelling in ("name = \"mothership\"", "name='mothership'", "name   =    'mothership'"):
        (tmp_path / "checkout" / "pyproject.toml").write_text(f"[project]\n{spelling}\nversion = '1'\n")
        assert units_mod._running_from_dev_tree() is not None, spelling

    # a DIFFERENT project's checkout is not ours → not a dev-tree refusal
    (tmp_path / "checkout" / "pyproject.toml").write_text("[project]\nname = 'something-else'\n")
    assert units_mod._running_from_dev_tree() is None
    # malformed TOML must not raise
    (tmp_path / "checkout" / "pyproject.toml").write_text("[project\nname=")
    assert units_mod._running_from_dev_tree() is None
