from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import Connection, delete, select

from mship.core.persistence.schema import (
    task_dependencies,
    task_pr_urls,
    task_repos,
    task_switch_anchors,
    task_switch_sources,
    task_test_results,
    tasks,
)
from mship.core.persistence.serialization import (
    ConcurrentUpdateError,
    PersistenceDecodeError,
    decode_datetime,
    encode_datetime,
    encode_path,
)
from mship.core.state import DependencyEdge, Task, TestResult

_TASK_CHILD_TABLES = (
    task_dependencies,
    task_switch_anchors,
    task_switch_sources,
    task_pr_urls,
    task_test_results,
    task_repos,
)


class TaskRepository:
    def list(self, conn: Connection) -> dict[str, Task]:
        slugs = conn.execute(select(tasks.c.slug).order_by(tasks.c.slug)).scalars()
        return {slug: self._load(conn, slug) for slug in slugs}

    def get(self, conn: Connection, slug: str) -> Task | None:
        row = conn.execute(
            select(tasks).where(tasks.c.slug == slug)
        ).mappings().one_or_none()
        if row is None:
            return None
        return self._decode(conn, row)

    def insert(self, conn: Connection, task: Task) -> None:
        conn.execute(tasks.insert().values(**self._scalar_values(task)))
        self._insert_children(conn, task)

    def replace(
        self,
        conn: Connection,
        task: Task,
        *,
        expected_revision: int | None = None,
    ) -> int:
        predicate = tasks.c.slug == task.slug
        if expected_revision is not None:
            predicate &= tasks.c.revision == expected_revision
        statement = (
            tasks.update()
            .where(predicate)
            .values(
                **self._scalar_values(task),
                revision=tasks.c.revision + 1,
            )
            .returning(tasks.c.revision)
        )
        new_revision = conn.execute(statement).scalar_one_or_none()
        if new_revision is None:
            if expected_revision is not None:
                raise ConcurrentUpdateError("tasks", task.slug, expected_revision)
            raise KeyError(task.slug)
        self._delete_children(conn, task.slug)
        self._insert_children(conn, task)
        return int(new_revision)

    def delete(self, conn: Connection, slug: str) -> bool:
        result = conn.execute(delete(tasks).where(tasks.c.slug == slug))
        return bool(result.rowcount)

    def _load(self, conn: Connection, slug: str) -> Task:
        task = self.get(conn, slug)
        if task is None:
            raise KeyError(slug)
        return task

    def _scalar_values(self, task: Task) -> dict[str, object]:
        return {
            "slug": task.slug,
            "description": task.description,
            "phase": task.phase,
            "created_at": encode_datetime(task.created_at),
            "branch": task.branch,
            "blocked_reason": task.blocked_reason,
            "blocked_at": encode_datetime(task.blocked_at),
            "finished_at": encode_datetime(task.finished_at),
            "phase_entered_at": encode_datetime(task.phase_entered_at),
            "last_activity_at": encode_datetime(task.last_activity_at),
            "active_repo": task.active_repo,
            "test_iteration": task.test_iteration,
            "base_branch": task.base_branch,
            "base_override": task.base_override,
            "spec_id": task.spec_id,
            "work_item_id": task.work_item_id,
        }

    def _delete_children(self, conn: Connection, slug: str) -> None:
        for table in _TASK_CHILD_TABLES:
            conn.execute(delete(table).where(table.c.task_slug == slug))

    def _insert_children(self, conn: Connection, task: Task) -> None:
        repo_names = list(dict.fromkeys(task.affected_repos))
        repo_names.extend(name for name in task.worktrees if name not in repo_names)
        repo_names.extend(name for name in sorted(task.passive_repos) if name not in repo_names)
        affected_ordinals = {
            repo_name: ordinal
            for ordinal, repo_name in enumerate(task.affected_repos)
        }
        self._insert_many(
            conn,
            task_repos,
            (
                {
                    "task_slug": task.slug,
                    "repo_name": repo_name,
                    "affected_ordinal": affected_ordinals.get(repo_name),
                    "passive": repo_name in task.passive_repos,
                    "worktree_path": encode_path(task.worktrees.get(repo_name)),
                }
                for repo_name in repo_names
            ),
        )
        self._insert_many(
            conn,
            task_test_results,
            (
                {
                    "task_slug": task.slug,
                    "repo_name": repo_name,
                    "status": result.status,
                    "at": encode_datetime(result.at),
                }
                for repo_name, result in task.test_results.items()
            ),
        )
        self._insert_many(
            conn,
            task_pr_urls,
            (
                {
                    "task_slug": task.slug,
                    "repo_name": repo_name,
                    "url": url,
                }
                for repo_name, url in task.pr_urls.items()
            ),
        )
        self._insert_many(
            conn,
            task_switch_sources,
            (
                {
                    "task_slug": task.slug,
                    "source_repo": source_repo,
                }
                for source_repo in task.last_switched_at_sha
            ),
        )
        self._insert_many(
            conn,
            task_switch_anchors,
            (
                {
                    "task_slug": task.slug,
                    "source_repo": source_repo,
                    "dependency_repo": dependency_repo,
                    "sha": sha,
                }
                for source_repo, dependencies in task.last_switched_at_sha.items()
                for dependency_repo, sha in dependencies.items()
            ),
        )
        self._insert_many(
            conn,
            task_dependencies,
            (
                {
                    "task_slug": task.slug,
                    "upstream_slug": edge.upstream_slug,
                    "created_at": encode_datetime(edge.created_at),
                }
                for edge in task.depends_on
            ),
        )

    def _insert_many(
        self,
        conn: Connection,
        table,
        rows: Iterable[dict[str, object]],
    ) -> None:
        values = list(rows)
        if values:
            conn.execute(table.insert(), values)

    def _decode(self, conn: Connection, row) -> Task:
        slug = str(row["slug"])
        try:
            repo_rows = conn.execute(
                select(task_repos)
                .where(task_repos.c.task_slug == slug)
                .order_by(task_repos.c.repo_name)
            ).mappings().all()
            affected_repos = [
                str(repo["repo_name"])
                for repo in sorted(
                    (repo for repo in repo_rows if repo["affected_ordinal"] is not None),
                    key=lambda repo: int(repo["affected_ordinal"]),
                )
            ]
            worktrees = {
                str(repo["repo_name"]): Path(str(repo["worktree_path"]))
                for repo in repo_rows
                if repo["worktree_path"] is not None
            }
            passive_repos = {
                str(repo["repo_name"]) for repo in repo_rows if repo["passive"]
            }
            test_results = {
                str(result["repo_name"]): TestResult(
                    status=result["status"],
                    at=decode_datetime(result["at"]),
                )
                for result in conn.execute(
                    select(task_test_results)
                    .where(task_test_results.c.task_slug == slug)
                    .order_by(task_test_results.c.repo_name)
                ).mappings()
            }
            pr_urls = {
                str(pr["repo_name"]): str(pr["url"])
                for pr in conn.execute(
                    select(task_pr_urls)
                    .where(task_pr_urls.c.task_slug == slug)
                    .order_by(task_pr_urls.c.repo_name)
                ).mappings()
            }
            switched = {
                str(source["source_repo"]): {}
                for source in conn.execute(
                    select(task_switch_sources)
                    .where(task_switch_sources.c.task_slug == slug)
                    .order_by(task_switch_sources.c.source_repo)
                ).mappings()
            }
            for anchor in conn.execute(
                select(task_switch_anchors)
                .where(task_switch_anchors.c.task_slug == slug)
                .order_by(
                    task_switch_anchors.c.source_repo,
                    task_switch_anchors.c.dependency_repo,
                )
            ).mappings():
                switched.setdefault(str(anchor["source_repo"]), {})[
                    str(anchor["dependency_repo"])
                ] = str(anchor["sha"])
            depends_on = [
                DependencyEdge(
                    upstream_slug=dependency["upstream_slug"],
                    created_at=decode_datetime(dependency["created_at"]),
                )
                for dependency in conn.execute(
                    select(task_dependencies)
                    .where(task_dependencies.c.task_slug == slug)
                    .order_by(
                        task_dependencies.c.created_at,
                        task_dependencies.c.upstream_slug,
                    )
                ).mappings()
            ]
            return Task.model_validate(
                {
                    "slug": slug,
                    "description": row["description"],
                    "phase": row["phase"],
                    "created_at": decode_datetime(row["created_at"]),
                    "affected_repos": affected_repos,
                    "worktrees": worktrees,
                    "branch": row["branch"],
                    "test_results": test_results,
                    "blocked_reason": row["blocked_reason"],
                    "blocked_at": decode_datetime(row["blocked_at"]),
                    "pr_urls": pr_urls,
                    "finished_at": decode_datetime(row["finished_at"]),
                    "phase_entered_at": decode_datetime(row["phase_entered_at"]),
                    "last_activity_at": decode_datetime(row["last_activity_at"]),
                    "active_repo": row["active_repo"],
                    "last_switched_at_sha": switched,
                    "test_iteration": row["test_iteration"],
                    "base_branch": row["base_branch"],
                    "base_override": row["base_override"],
                    "passive_repos": passive_repos,
                    "spec_id": row["spec_id"],
                    "depends_on": depends_on,
                    "work_item_id": row["work_item_id"],
                }
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise PersistenceDecodeError("tasks", slug, error) from error
