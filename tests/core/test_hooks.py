import os
import stat
from pathlib import Path

import pytest

from mship.core.hooks import (
    HOOK_MARKER_BEGIN, HOOK_MARKER_END,
    install_hook, uninstall_hook, is_installed,
)


def _hook_path(git_root: Path) -> Path:
    return git_root / ".git" / "hooks" / "pre-commit"


def _init_repo(path: Path) -> None:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)


def test_install_creates_hook_when_missing(tmp_path):
    _init_repo(tmp_path)
    install_hook(tmp_path)
    hook = _hook_path(tmp_path)
    assert hook.exists()
    content = hook.read_text()
    assert content.startswith("#!/bin/sh")
    assert HOOK_MARKER_BEGIN in content
    assert HOOK_MARKER_END in content
    # Executable
    mode = hook.stat().st_mode
    assert mode & stat.S_IXUSR


def test_install_is_idempotent(tmp_path):
    _init_repo(tmp_path)
    install_hook(tmp_path)
    first = _hook_path(tmp_path).read_text()
    install_hook(tmp_path)
    second = _hook_path(tmp_path).read_text()
    assert first == second


def test_install_appends_to_existing_hook(tmp_path):
    _init_repo(tmp_path)
    hook = _hook_path(tmp_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n# user hook\necho 'user pre-commit step'\n")
    hook.chmod(0o755)

    install_hook(tmp_path)
    content = hook.read_text()
    assert "user pre-commit step" in content
    assert HOOK_MARKER_BEGIN in content
    assert HOOK_MARKER_END in content
    # MSHIP block appears AFTER the user's content
    assert content.index("user pre-commit step") < content.index(HOOK_MARKER_BEGIN)


def test_is_installed_detects_marker(tmp_path):
    _init_repo(tmp_path)
    assert is_installed(tmp_path) is False
    install_hook(tmp_path)
    assert is_installed(tmp_path) is True


def test_is_installed_false_when_hook_exists_without_marker(tmp_path):
    _init_repo(tmp_path)
    hook = _hook_path(tmp_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho hi\n")
    assert is_installed(tmp_path) is False


def test_uninstall_removes_mship_block_preserves_other_content(tmp_path):
    _init_repo(tmp_path)
    hook = _hook_path(tmp_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'user step'\n")
    install_hook(tmp_path)
    uninstall_hook(tmp_path)
    content = hook.read_text()
    assert "user step" in content
    assert HOOK_MARKER_BEGIN not in content
    assert HOOK_MARKER_END not in content


def test_uninstall_on_file_without_marker_is_noop(tmp_path):
    _init_repo(tmp_path)
    hook = _hook_path(tmp_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    original = "#!/bin/sh\necho hi\n"
    hook.write_text(original)
    uninstall_hook(tmp_path)
    assert hook.read_text() == original


def test_install_when_git_dir_missing_raises(tmp_path):
    # No `.git` at all — hook path's parent doesn't exist and we can't install blindly
    with pytest.raises((FileNotFoundError, OSError)):
        install_hook(tmp_path)


def test_install_creates_all_three_hook_files(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    install_hook(tmp_path)
    hooks = tmp_path / ".git" / "hooks"
    assert (hooks / "pre-commit").exists()
    assert (hooks / "post-checkout").exists()
    assert (hooks / "post-commit").exists()
    for name in ("pre-commit", "post-checkout", "post-commit"):
        content = (hooks / name).read_text()
        assert HOOK_MARKER_BEGIN in content
        assert HOOK_MARKER_END in content


def test_each_hook_has_distinct_body(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    install_hook(tmp_path)
    hooks = tmp_path / ".git" / "hooks"
    pre = (hooks / "pre-commit").read_text()
    post_co = (hooks / "post-checkout").read_text()
    post_ci = (hooks / "post-commit").read_text()
    assert "mship _check-commit" in pre
    assert "mship _post-checkout" in post_co
    assert "mship _journal-commit" in post_ci


def test_is_installed_requires_all_three(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    install_hook(tmp_path)
    assert is_installed(tmp_path) is True

    # Remove post-checkout — is_installed should now be False
    (tmp_path / ".git" / "hooks" / "post-checkout").unlink()
    assert is_installed(tmp_path) is False


def test_uninstall_strips_all_three(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    hooks = tmp_path / ".git" / "hooks"
    # Seed each hook file with user content first
    for name in ("pre-commit", "post-checkout", "post-commit"):
        (hooks / name).write_text(f"#!/bin/sh\n# user {name}\n")
    install_hook(tmp_path)
    uninstall_hook(tmp_path)
    for name in ("pre-commit", "post-checkout", "post-commit"):
        content = (hooks / name).read_text()
        assert f"user {name}" in content
        assert HOOK_MARKER_BEGIN not in content


# --- InstallOutcome + refresh behavior (issue-31 follow-up) ---

from mship.core.hooks import InstallOutcome


def _make_git_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git" / "hooks").mkdir(parents=True)
    return root


def test_install_hook_fresh_returns_installed_for_each(tmp_path: Path):
    root = _make_git_root(tmp_path)
    outcomes = install_hook(root)
    assert outcomes == {
        "pre-commit": InstallOutcome.installed,
        "post-commit": InstallOutcome.installed,
        "post-checkout": InstallOutcome.installed,
    }


def test_install_hook_second_run_is_up_to_date(tmp_path: Path):
    root = _make_git_root(tmp_path)
    install_hook(root)
    hooks_dir = root / ".git" / "hooks"
    mtimes_before = {n: (hooks_dir / n).stat().st_mtime_ns
                     for n in ("pre-commit", "post-commit", "post-checkout")}
    outcomes = install_hook(root)
    assert outcomes == {
        "pre-commit": InstallOutcome.up_to_date,
        "post-commit": InstallOutcome.up_to_date,
        "post-checkout": InstallOutcome.up_to_date,
    }
    mtimes_after = {n: (hooks_dir / n).stat().st_mtime_ns
                    for n in ("pre-commit", "post-commit", "post-checkout")}
    assert mtimes_before == mtimes_after, "up_to_date outcome must not touch file mtimes"


def test_install_hook_refreshes_stale_block(tmp_path: Path):
    root = _make_git_root(tmp_path)
    post_commit = root / ".git" / "hooks" / "post-commit"
    post_commit.write_text(
        "#!/bin/sh\n"
        "# git post-commit hook\n"
        "# MSHIP-BEGIN — managed by mship; edit outside this block is fine\n"
        "if command -v mship >/dev/null 2>&1; then\n"
        "    mship _log-commit || true\n"   # stale
        "fi\n"
        "# MSHIP-END\n"
    )
    outcomes = install_hook(root)
    assert outcomes["post-commit"] == InstallOutcome.refreshed
    assert outcomes["pre-commit"] == InstallOutcome.installed
    assert outcomes["post-checkout"] == InstallOutcome.installed
    assert "_journal-commit" in post_commit.read_text()
    assert "_log-commit" not in post_commit.read_text()


def test_install_hook_preserves_user_content_around_block(tmp_path: Path):
    root = _make_git_root(tmp_path)
    post_commit = root / ".git" / "hooks" / "post-commit"
    post_commit.write_text(
        "#!/bin/sh\n"
        "# user's own pre-existing logic\n"
        "echo 'user before'\n"
        "\n"
        "# MSHIP-BEGIN — managed by mship; edit outside this block is fine\n"
        "if command -v mship >/dev/null 2>&1; then\n"
        "    mship _log-commit || true\n"
        "fi\n"
        "# MSHIP-END\n"
        "echo 'user after'\n"
    )
    install_hook(root)
    content = post_commit.read_text()
    assert "echo 'user before'" in content
    assert "echo 'user after'" in content
    assert "_journal-commit" in content
    assert "_log-commit" not in content


def test_install_hook_skips_corrupt_hook_missing_end_marker(tmp_path: Path):
    root = _make_git_root(tmp_path)
    post_commit = root / ".git" / "hooks" / "post-commit"
    post_commit.write_text(
        "#!/bin/sh\n"
        "# MSHIP-BEGIN — managed by mship; edit outside this block is fine\n"
        "if command -v mship >/dev/null 2>&1; then\n"
        "    mship _log-commit || true\n"
        "# (end marker missing)\n"
    )
    before = post_commit.read_text()
    outcomes = install_hook(root)
    assert outcomes["post-commit"] == InstallOutcome.skipped_corrupt
    assert post_commit.read_text() == before
