import pytest

from mship.core.config import RepoConfig


def _repo(**extra):
    return {
        "path": ".",
        "type": "service",
        "tasks": {"publish": "write-report"},
        **extra,
    }


def test_task_outputs_must_reference_a_configured_logical_key():
    with pytest.raises(ValueError, match="unknown logical task"):
        RepoConfig.model_validate(_repo(task_outputs={
            "other": {"retention_seconds": 60, "artifacts": [
                {"name": "report", "relative_path": "out/report.txt", "media_type": "text/plain"}
            ]}
        }))


def test_task_outputs_are_strict_and_exact():
    with pytest.raises(ValueError):
        RepoConfig.model_validate(_repo(task_outputs={
            "publish": {"retention_seconds": 60, "artifacts": [
                {"name": "report", "relative_path": "out/report.txt", "media_type": "text/plain", "extra": True}
            ]}
        }))
