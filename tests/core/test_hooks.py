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
