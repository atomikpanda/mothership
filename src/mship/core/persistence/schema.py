from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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

app_runs = Table(
    "app_runs",
    metadata,
    Column("id", Text, primary_key=True),
    Column("task_slug", Text, nullable=False),
    Column("repo", Text, nullable=False),
    Column("profile", Text, nullable=False),
    Column("profile_revision", Text, nullable=False),
    Column("backend", Text, nullable=False),
    Column("backend_revision", Text, nullable=False),
    Column("host_name", Text, nullable=False),
    Column("host_scope", Text, nullable=False),
    Column("host_endpoint_fingerprint", Text, nullable=False),
    Column("safe_target_label", Text, nullable=False),
    Column("private_binding_ref", Text, nullable=False),
    Column("operation", Text, nullable=False),
    Column("protocol_version", Integer, nullable=False),
    Column("capabilities_json", Text, nullable=False),
    Column("target_aliases_json", Text, nullable=False, server_default=text("'[]'")),
    Column("owner_ref", Text),
    Column("owner_generation", Text),
    Column("status", Text, nullable=False),
    Column("revision", Integer, nullable=False, server_default=text("0")),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
    Column("binary_provenance_json", Text),
    Column("source_update_receipt_json", Text),
    ForeignKeyConstraint(["task_slug"], ["tasks.slug"], ondelete="RESTRICT"),
    ForeignKeyConstraint(
        ["task_slug", "repo"],
        ["task_repos.task_slug", "task_repos.repo_name"],
        ondelete="RESTRICT",
    ),
    CheckConstraint("host_scope IN ('user', 'project')", name="host_scope"),
    CheckConstraint("protocol_version = 1", name="protocol_version"),
    CheckConstraint(
        "status IN ('starting', 'active', 'updating', 'stopped', 'failed', 'unknown')",
        name="status",
    ),
    CheckConstraint(
        "(owner_ref IS NULL AND owner_generation IS NULL) "
        "OR (owner_ref IS NOT NULL AND owner_generation IS NOT NULL)",
        name="owner_pair",
    ),
    CheckConstraint(
        "status NOT IN ('active', 'updating') OR owner_ref IS NOT NULL",
        name="active_owner_acknowledgement",
    ),
    CheckConstraint(
        "binary_provenance_json IS NULL",
        name="binary_provenance_unavailable",
    ),
    CheckConstraint("revision >= 0", name="revision_non_negative"),
    Index("ix_app_runs_task_repo_status", "task_slug", "repo", "status"),
)

task_results = Table(
    "task_results",
    metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, nullable=False),
    Column("task_slug", Text, nullable=False),
    Column("work_item_id", Text),
    Column("repo", Text, nullable=False),
    Column("logical_task", Text, nullable=False),
    Column("task_key", Text, nullable=False),
    Column("host_name", Text),
    Column("host_role", Text),
    Column("host_endpoint_fingerprint", Text),
    Column("worktree_identity", Text, nullable=False),
    Column("source_revision", Text),
    Column("snapshot_identity", Text),
    Column("env_runner_identity", Text),
    Column("outcome_status", Text, nullable=False),
    Column("exit_code", Integer),
    Column("finished_at", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("expires_at", Text, nullable=False),
    CheckConstraint(
        "outcome_status IN ('completed', 'failed', 'cancelled', 'infrastructure_error')",
        name="outcome_status",
    ),
    CheckConstraint(
        "(outcome_status = 'completed' AND exit_code = 0) OR "
        "(outcome_status = 'failed' AND exit_code IS NOT NULL AND exit_code <> 0) OR "
        "(outcome_status IN ('cancelled', 'infrastructure_error') AND exit_code IS NULL)",
        name="outcome_exit_code",
    ),
    Index("ix_task_results_workspace_task", "workspace_id", "task_slug", "created_at"),
    Index(
        "ix_task_results_workspace_item", "workspace_id", "work_item_id", "created_at"
    ),
)

task_result_artifacts = Table(
    "task_result_artifacts",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "result_id",
        Text,
        ForeignKey("task_results.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("name", Text, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("byte_size", Integer),
    Column("sha256", Text),
    Column("availability", Text, nullable=False),
    Column("safe_reason", Text),
    Column("blob_locator", Text),
    UniqueConstraint("result_id", "ordinal"),
    CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    CheckConstraint(
        "availability IN ('published', 'missing', 'rejected', 'expired')",
        name="availability",
    ),
    CheckConstraint(
        "(availability = 'published' AND byte_size IS NOT NULL AND sha256 IS NOT NULL "
        "AND blob_locator IS NOT NULL) OR availability <> 'published'",
        name="published_fields",
    ),
    Index("ix_task_result_artifacts_blob", "blob_locator"),
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
