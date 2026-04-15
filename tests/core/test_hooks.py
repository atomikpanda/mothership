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
