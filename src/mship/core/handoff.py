from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel


class MergeOrderEntry(BaseModel):
    order: int
    repo: str
    path: Path
    branch: str
    depends_on: list[str]
    pr: str | None = None


class HandoffManifest(BaseModel):
    task: str
    branch: str
    generated_at: datetime
    merge_order: list[MergeOrderEntry]


def generate_handoff(
    handoffs_dir: Path,
    task_slug: str,
    branch: str,
    ordered_repos: list[str],
    repo_paths: dict[str, Path],
    repo_deps: dict[str, list[str]],
) -> Path:
    """Generate a handoff manifest YAML file."""
    handoffs_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for i, repo in enumerate(ordered_repos, 1):
        entries.append(
            MergeOrderEntry(
                order=i,
                repo=repo,
                path=repo_paths[repo],
                branch=branch,
                depends_on=repo_deps.get(repo, []),
            )
        )

    manifest = HandoffManifest(
        task=task_slug,
        branch=branch,
        generated_at=datetime.now(timezone.utc),
        merge_order=entries,
    )

    path = handoffs_dir / f"{task_slug}.yaml"
    data = manifest.model_dump(mode="json")
    for entry in data["merge_order"]:
        entry["path"] = str(entry["path"])
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return path
