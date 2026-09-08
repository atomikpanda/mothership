from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError
from sqlalchemy import Connection, delete, select

from mship.core.persistence.schema import (
    work_items,
    workitem_affected_repos,
    workitem_external_links,
    workitem_pr_urls,
    workitem_tasks,
    workitem_threads,
)
from mship.core.persistence.serialization import (
    ConcurrentUpdateError,
    PersistenceDecodeError,
    decode_datetime,
    decode_json,
    encode_datetime,
    encode_json,
    workitem_extras,
)
from mship.core.workitem import WorkItem

_WORKITEM_CHILD_TABLES = (
    workitem_pr_urls,
    workitem_affected_repos,
    workitem_external_links,
    workitem_threads,
    workitem_tasks,
)


class WorkItemRepository:
    def list(
        self,
        conn: Connection,
        *,
        include_archived: bool = False,
    ) -> list[WorkItem]:
        statement = select(work_items).order_by(
            work_items.c.updated_at.desc(),
            work_items.c.id,
        )
        if not include_archived:
            statement = statement.where(work_items.c.archived.is_(False))
        return [self._decode(conn, row) for row in conn.execute(statement).mappings()]

    def list_tolerant_with_uncertainty(
        self,
        conn: Connection,
        *,
        include_archived: bool = False,
    ) -> tuple[list[WorkItem], bool]:
        statement = select(work_items).order_by(
            work_items.c.updated_at.desc(),
            work_items.c.id,
        )
        if not include_archived:
            statement = statement.where(work_items.c.archived.is_(False))
        items: list[WorkItem] = []
        uncertain = False
        for row in conn.execute(statement).mappings():
            try:
                items.append(self._decode(conn, row))
            except PersistenceDecodeError:
                uncertain = True
        return items, uncertain

    def list_tolerant(
        self,
        conn: Connection,
        *,
        include_archived: bool = False,
    ) -> list[WorkItem]:
        return self.list_tolerant_with_uncertainty(
            conn,
            include_archived=include_archived,
        )[0]

    def get(self, conn: Connection, item_id: str) -> WorkItem | None:
        row = conn.execute(
            select(work_items).where(work_items.c.id == item_id)
        ).mappings().one_or_none()
        if row is None:
            return None
        return self._decode(conn, row)

    def insert(self, conn: Connection, item: WorkItem) -> None:
        conn.execute(work_items.insert().values(**self._scalar_values(item)))
        self._insert_children(conn, item)

    def replace(
        self,
        conn: Connection,
        item: WorkItem,
        *,
        expected_revision: int | None = None,
    ) -> int:
        predicate = work_items.c.id == item.id
        if expected_revision is not None:
            predicate &= work_items.c.revision == expected_revision
        statement = (
            work_items.update()
            .where(predicate)
            .values(
                **self._scalar_values(item),
                revision=work_items.c.revision + 1,
            )
            .returning(work_items.c.revision)
        )
        new_revision = conn.execute(statement).scalar_one_or_none()
        if new_revision is None:
            if expected_revision is not None:
                raise ConcurrentUpdateError("work_items", item.id, expected_revision)
            raise KeyError(item.id)
        self._delete_children(conn, item.id)
        self._insert_children(conn, item)
        return int(new_revision)

    def delete(self, conn: Connection, item_id: str) -> bool:
        result = conn.execute(delete(work_items).where(work_items.c.id == item_id))
        return bool(result.rowcount)

    def _scalar_values(self, item: WorkItem) -> dict[str, object]:
        return {
            "id": item.id,
            "title": item.title,
            "workspace": item.workspace,
            "kind": item.kind,
            "created_at": encode_datetime(item.created_at),
            "updated_at": encode_datetime(item.updated_at),
            "spec_id": item.spec_id,
            "plan_path": item.plan_path,
            "phase_override": item.phase_override,
            "unattended": item.unattended,
            "archived": item.archived,
            "extras_json": encode_json(workitem_extras(item)),
        }

    def _delete_children(self, conn: Connection, item_id: str) -> None:
        for table in _WORKITEM_CHILD_TABLES:
            conn.execute(delete(table).where(table.c.work_item_id == item_id))

    def _insert_children(self, conn: Connection, item: WorkItem) -> None:
        self._insert_many(
            conn,
            workitem_tasks,
            (
                {
                    "work_item_id": item.id,
                    "task_slug": task_slug,
                    "ordinal": ordinal,
                }
                for ordinal, task_slug in enumerate(item.task_slugs)
            ),
        )
        self._insert_many(
            conn,
            workitem_threads,
            (
                {
                    "work_item_id": item.id,
                    "thread_id": thread_id,
                    "ordinal": ordinal,
                }
                for ordinal, thread_id in enumerate(item.thread_ids)
            ),
        )
        self._insert_many(
            conn,
            workitem_external_links,
            (
                {
                    "work_item_id": item.id,
                    "ordinal": ordinal,
                    "provider": link.provider,
                    "url": link.url,
                    "title": link.title,
                }
                for ordinal, link in enumerate(item.external_links)
            ),
        )
        self._insert_many(
            conn,
            workitem_affected_repos,
            (
                {
                    "work_item_id": item.id,
                    "repo_name": repo_name,
                    "ordinal": ordinal,
                }
                for ordinal, repo_name in enumerate(item.affected_repos)
            ),
        )
        self._insert_many(
            conn,
            workitem_pr_urls,
            (
                {
                    "work_item_id": item.id,
                    "url": url,
                    "ordinal": ordinal,
                }
                for ordinal, url in enumerate(item.pr_urls)
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

    def _decode(self, conn: Connection, row) -> WorkItem:
        item_id = str(row["id"])
        try:
            extras = decode_json(row["extras_json"])
            if not isinstance(extras, dict):
                raise TypeError("extras_json must decode to an object")
            payload = dict(extras)
            payload.update(
                {
                    "id": item_id,
                    "title": row["title"],
                    "workspace": row["workspace"],
                    "kind": row["kind"],
                    "created_at": decode_datetime(row["created_at"]),
                    "updated_at": decode_datetime(row["updated_at"]),
                    "spec_id": row["spec_id"],
                    "plan_path": row["plan_path"],
                    "task_slugs": list(
                        conn.execute(
                            select(workitem_tasks.c.task_slug)
                            .where(workitem_tasks.c.work_item_id == item_id)
                            .order_by(workitem_tasks.c.ordinal)
                        ).scalars()
                    ),
                    "thread_ids": list(
                        conn.execute(
                            select(workitem_threads.c.thread_id)
                            .where(workitem_threads.c.work_item_id == item_id)
                            .order_by(workitem_threads.c.ordinal)
                        ).scalars()
                    ),
                    "external_links": [
                        {
                            "provider": link["provider"],
                            "url": link["url"],
                            "title": link["title"],
                        }
                        for link in conn.execute(
                            select(workitem_external_links)
                            .where(workitem_external_links.c.work_item_id == item_id)
                            .order_by(workitem_external_links.c.ordinal)
                        ).mappings()
                    ],
                    "phase_override": row["phase_override"],
                    "unattended": row["unattended"],
                    "archived": row["archived"],
                    "affected_repos": list(
                        conn.execute(
                            select(workitem_affected_repos.c.repo_name)
                            .where(workitem_affected_repos.c.work_item_id == item_id)
                            .order_by(workitem_affected_repos.c.ordinal)
                        ).scalars()
                    ),
                    "pr_urls": list(
                        conn.execute(
                            select(workitem_pr_urls.c.url)
                            .where(workitem_pr_urls.c.work_item_id == item_id)
                            .order_by(workitem_pr_urls.c.ordinal)
                        ).scalars()
                    ),
                }
            )
            return WorkItem.model_validate(payload)
        except (TypeError, ValueError, ValidationError) as error:
            raise PersistenceDecodeError("work_items", item_id, error) from error
