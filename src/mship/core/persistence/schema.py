from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


storage_metadata = Table(
    "storage_metadata",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)

tasks = Table(
    "tasks",
    metadata,
    Column("slug", Text, primary_key=True),
    Column("description", Text, nullable=False),
    Column("phase", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("branch", Text, nullable=False),
    Column("blocked_reason", Text),
    Column("blocked_at", Text),
    Column("finished_at", Text),
    Column("phase_entered_at", Text),
    Column("last_activity_at", Text),
    Column("active_repo", Text),
    Column("test_iteration", Integer, nullable=False, server_default=text("0")),
    Column("base_branch", Text),
    Column("base_override", Text),
    Column("spec_id", Text),
    Column("work_item_id", Text),
    Column("revision", Integer, nullable=False, server_default=text("0")),
    CheckConstraint(
        "phase IN ('plan', 'dev', 'review', 'run')",
        name="phase",
    ),
    CheckConstraint("test_iteration >= 0", name="test_iteration_non_negative"),
    CheckConstraint("revision >= 0", name="revision_non_negative"),
)

task_repos = Table(
    "task_repos",
    metadata,
    Column(
        "task_slug",
        Text,
        ForeignKey("tasks.slug", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("repo_name", Text, nullable=False),
    Column("affected_ordinal", Integer),
    Column("passive", Boolean, nullable=False, server_default=text("0")),
    Column("worktree_path", Text),
    PrimaryKeyConstraint("task_slug", "repo_name"),
    UniqueConstraint("task_slug", "affected_ordinal"),
    CheckConstraint(
        "affected_ordinal IS NULL OR affected_ordinal >= 0",
        name="affected_ordinal_non_negative",
    ),
)

task_test_results = Table(
    "task_test_results",
    metadata,
    Column(
        "task_slug",
        Text,
        ForeignKey("tasks.slug", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("repo_name", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("at", Text, nullable=False),
    PrimaryKeyConstraint("task_slug", "repo_name"),
    CheckConstraint("status IN ('pass', 'fail', 'skip')", name="status"),
)

task_pr_urls = Table(
    "task_pr_urls",
    metadata,
    Column(
        "task_slug",
        Text,
        ForeignKey("tasks.slug", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("repo_name", Text, nullable=False),
    Column("url", Text, nullable=False),
    PrimaryKeyConstraint("task_slug", "repo_name"),
)


task_switch_sources = Table(
    "task_switch_sources",
    metadata,
    Column(
        "task_slug",
        Text,
        ForeignKey("tasks.slug", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_repo", Text, nullable=False),
    PrimaryKeyConstraint("task_slug", "source_repo"),
)

task_switch_anchors = Table(
    "task_switch_anchors",
    metadata,
    Column(
        "task_slug",
        Text,
        ForeignKey("tasks.slug", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_repo", Text, nullable=False),
    Column("dependency_repo", Text, nullable=False),
    Column("sha", Text, nullable=False),
    PrimaryKeyConstraint("task_slug", "source_repo", "dependency_repo"),
)

task_dependencies = Table(
    "task_dependencies",
    metadata,
    Column("task_slug", Text, nullable=False),
    Column("upstream_slug", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    PrimaryKeyConstraint("task_slug", "upstream_slug"),
    ForeignKeyConstraint(["task_slug"], ["tasks.slug"], ondelete="CASCADE"),
    ForeignKeyConstraint(["upstream_slug"], ["tasks.slug"], ondelete="CASCADE"),
    CheckConstraint("task_slug <> upstream_slug", name="not_self"),
)

work_items = Table(
    "work_items",
    metadata,
    Column("id", Text, primary_key=True),
    Column("title", Text, nullable=False),
    Column("workspace", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("spec_id", Text),
    Column("plan_path", Text),
    Column("phase_override", Text),
    Column("unattended", Boolean, nullable=False, server_default=text("0")),
    Column("archived", Boolean, nullable=False, server_default=text("0")),
    Column("revision", Integer, nullable=False, server_default=text("0")),
    Column("extras_json", Text, nullable=False, server_default=text("'{}'")),
    CheckConstraint(
        "kind IN ('feature', 'bug', 'chore', 'question')",
        name="kind",
    ),
    CheckConstraint(
        "phase_override IS NULL OR "
        "phase_override IN ('inbox', 'shaping', 'ready', 'in_flight', 'review', 'done')",
        name="phase_override",
    ),
    CheckConstraint("revision >= 0", name="revision_non_negative"),
)

workitem_tasks = Table(
    "workitem_tasks",
    metadata,
    Column(
        "work_item_id",
        Text,
        ForeignKey("work_items.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("task_slug", Text, nullable=False, unique=True),
    Column("ordinal", Integer, nullable=False),
    PrimaryKeyConstraint("work_item_id", "task_slug"),
    UniqueConstraint("work_item_id", "ordinal"),
    CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
)

workitem_threads = Table(
    "workitem_threads",
    metadata,
    Column(
        "work_item_id",
        Text,
        ForeignKey("work_items.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("thread_id", Text, nullable=False, unique=True),
    Column("ordinal", Integer, nullable=False),
    PrimaryKeyConstraint("work_item_id", "thread_id"),
    UniqueConstraint("work_item_id", "ordinal"),
    CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
)

workitem_external_links = Table(
    "workitem_external_links",
    metadata,
    Column(
        "work_item_id",
        Text,
        ForeignKey("work_items.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("provider", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("title", Text, nullable=False, server_default=text("''")),
    PrimaryKeyConstraint("work_item_id", "ordinal"),
    CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    CheckConstraint(
        "provider IN ('github', 'linear', 'notion', 'jira', 'url')",
        name="provider",
    ),
)

workitem_affected_repos = Table(
    "workitem_affected_repos",
    metadata,
    Column(
        "work_item_id",
        Text,
        ForeignKey("work_items.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("repo_name", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    PrimaryKeyConstraint("work_item_id", "repo_name"),
    UniqueConstraint("work_item_id", "ordinal"),
    CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
)

workitem_pr_urls = Table(
    "workitem_pr_urls",
    metadata,
    Column(
        "work_item_id",
        Text,
        ForeignKey("work_items.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("url", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    PrimaryKeyConstraint("work_item_id", "url"),
    UniqueConstraint("work_item_id", "ordinal"),
    CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
)
