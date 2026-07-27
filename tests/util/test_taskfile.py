"""`taskfile_has_target` — moved out of cli/exec.py so core can use it too."""
from mship.util.taskfile import taskfile_has_target

_WITH_SETUP = "version: '3'\ntasks:\n  setup:\n    cmds: [echo hi]\n"


def _taskfile(tmp_path, name="Taskfile.yml", body=_WITH_SETUP):
    (tmp_path / name).write_text(body)
    return tmp_path


def test_finds_a_declared_target(tmp_path):
    assert taskfile_has_target(_taskfile(tmp_path), "setup")


def test_missing_target_is_false(tmp_path):
    assert not taskfile_has_target(_taskfile(tmp_path), "lint")


def test_the_yaml_spelling_is_honoured(tmp_path):
    assert taskfile_has_target(_taskfile(tmp_path, name="Taskfile.yaml"), "setup")


def test_no_taskfile_is_false(tmp_path):
    assert not taskfile_has_target(tmp_path, "setup")


def test_an_unparseable_taskfile_is_false(tmp_path):
    assert not taskfile_has_target(_taskfile(tmp_path, body="{{{not yaml"), "setup")


def test_a_taskfile_without_a_tasks_map_is_false(tmp_path):
    assert not taskfile_has_target(_taskfile(tmp_path, body="version: '3'\n"), "setup")


def test_a_taskfile_whose_tasks_key_is_not_a_map_is_false(tmp_path):
    assert not taskfile_has_target(
        _taskfile(tmp_path, body="version: '3'\ntasks: [setup]\n"), "setup"
    )


def test_a_taskfile_whose_top_level_is_not_a_map_is_false(tmp_path):
    # Valid YAML (no parse error), but a bare list rather than a mapping —
    # distinct from the "tasks key is not a map" case above, which still has
    # a top-level dict.
    assert not taskfile_has_target(
        _taskfile(tmp_path, body="- setup\n- lint\n"), "setup"
    )
