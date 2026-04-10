from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

from mship.core.handoff import HandoffManifest, MergeOrderEntry, generate_handoff


def test_handoff_manifest_model():
    entry = MergeOrderEntry(
        order=1,
        repo="shared",
        path=Path("./shared"),
        branch="feat/test",
        depends_on=[],
        pr=None,
    )
    manifest = HandoffManifest(
        task="add-labels",
        branch="feat/add-labels",
        generated_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        merge_order=[entry],
    )
    assert manifest.task == "add-labels"
    assert len(manifest.merge_order) == 1


def test_generate_handoff(tmp_path: Path):
    handoffs_dir = tmp_path / ".mothership" / "handoffs"
    handoffs_dir.mkdir(parents=True)

    ordered_repos = ["shared", "auth-service"]
    repo_paths = {"shared": Path("./shared"), "auth-service": Path("./auth-service")}
    repo_deps = {"shared": [], "auth-service": ["shared"]}

    path = generate_handoff(
        handoffs_dir=handoffs_dir,
        task_slug="add-labels",
        branch="feat/add-labels",
        ordered_repos=ordered_repos,
        repo_paths=repo_paths,
        repo_deps=repo_deps,
    )

    assert path.exists()
    with open(path) as f:
        data = yaml.safe_load(f)
    assert data["task"] == "add-labels"
    assert len(data["merge_order"]) == 2
    assert data["merge_order"][0]["repo"] == "shared"
    assert data["merge_order"][0]["order"] == 1
    assert data["merge_order"][1]["depends_on"] == ["shared"]
