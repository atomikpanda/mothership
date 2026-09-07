"""Create transactional Task and WorkItem storage.

Revision ID: 0001_tasks_and_workitems
Revises:
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0001_tasks_and_workitems"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "storage_metadata",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_storage_metadata"),
    )
    op.create_table(
        "tasks",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("blocked_reason", sa.Text()),
        sa.Column("blocked_at", sa.Text()),
        sa.Column("finished_at", sa.Text()),
        sa.Column("phase_entered_at", sa.Text()),
        sa.Column("last_activity_at", sa.Text()),
        sa.Column("active_repo", sa.Text()),
        sa.Column("test_iteration", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("base_branch", sa.Text()),
        sa.Column("base_override", sa.Text()),
        sa.Column("spec_id", sa.Text()),
        sa.Column("work_item_id", sa.Text()),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "phase IN ('plan', 'dev', 'review', 'run')",
            name="ck_tasks_phase",
        ),
        sa.CheckConstraint(
            "test_iteration >= 0",
            name="ck_tasks_test_iteration_non_negative",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_tasks_revision_non_negative",
        ),
        sa.PrimaryKeyConstraint("slug", name="pk_tasks"),
    )
    op.create_table(
        "task_repos",
        sa.Column("task_slug", sa.Text(), nullable=False),
        sa.Column("repo_name", sa.Text(), nullable=False),
        sa.Column("affected_ordinal", sa.Integer()),
        sa.Column("passive", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("worktree_path", sa.Text()),
        sa.CheckConstraint(
            "affected_ordinal IS NULL OR affected_ordinal >= 0",
            name="ck_task_repos_affected_ordinal_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["task_slug"],
            ["tasks.slug"],
            name="fk_task_repos_task_slug_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_slug", "repo_name", name="pk_task_repos"),
        sa.UniqueConstraint(
            "task_slug",
            "affected_ordinal",
            name="uq_task_repos_task_slug",
        ),
    )
    op.create_table(
        "task_test_results",
        sa.Column("task_slug", sa.Text(), nullable=False),
        sa.Column("repo_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pass', 'fail', 'skip')",
            name="ck_task_test_results_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_slug"],
            ["tasks.slug"],
            name="fk_task_test_results_task_slug_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_slug",
            "repo_name",
            name="pk_task_test_results",
        ),
    )
    op.create_table(
        "task_pr_urls",
        sa.Column("task_slug", sa.Text(), nullable=False),
        sa.Column("repo_name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_slug"],
            ["tasks.slug"],
            name="fk_task_pr_urls_task_slug_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_slug", "repo_name", name="pk_task_pr_urls"),
    )
    op.create_table(
        "task_switch_anchors",
        sa.Column("task_slug", sa.Text(), nullable=False),
        sa.Column("source_repo", sa.Text(), nullable=False),
        sa.Column("dependency_repo", sa.Text(), nullable=False),
        sa.Column("sha", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_slug"],
            ["tasks.slug"],
            name="fk_task_switch_anchors_task_slug_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_slug",
            "source_repo",
            "dependency_repo",
            name="pk_task_switch_anchors",
        ),
    )
    op.create_table(
        "task_dependencies",
        sa.Column("task_slug", sa.Text(), nullable=False),
        sa.Column("upstream_slug", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "task_slug <> upstream_slug",
            name="ck_task_dependencies_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["task_slug"],
            ["tasks.slug"],
            name="fk_task_dependencies_task_slug_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["upstream_slug"],
            ["tasks.slug"],
            name="fk_task_dependencies_upstream_slug_tasks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "task_slug",
            "upstream_slug",
            name="pk_task_dependencies",
        ),
    )
    op.create_table(
        "work_items",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("spec_id", sa.Text()),
        sa.Column("plan_path", sa.Text()),
        sa.Column("phase_override", sa.Text()),
        sa.Column("unattended", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("extras_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('feature', 'bug', 'chore', 'question')",
            name="ck_work_items_kind",
        ),
        sa.CheckConstraint(
            "phase_override IS NULL OR "
            "phase_override IN ('inbox', 'shaping', 'ready', 'in_flight', 'review', 'done')",
            name="ck_work_items_phase_override",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_work_items_revision_non_negative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_work_items"),
    )
    op.create_table(
        "workitem_tasks",
        sa.Column("work_item_id", sa.Text(), nullable=False),
        sa.Column("task_slug", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_workitem_tasks_ordinal_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name="fk_workitem_tasks_work_item_id_work_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "work_item_id",
            "task_slug",
            name="pk_workitem_tasks",
        ),
        sa.UniqueConstraint(
            "work_item_id",
            "ordinal",
            name="uq_workitem_tasks_work_item_id",
        ),
        sa.UniqueConstraint("task_slug", name="uq_workitem_tasks_task_slug"),
    )
    op.create_table(
        "workitem_threads",
        sa.Column("work_item_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_workitem_threads_ordinal_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name="fk_workitem_threads_work_item_id_work_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "work_item_id",
            "thread_id",
            name="pk_workitem_threads",
        ),
        sa.UniqueConstraint(
            "work_item_id",
            "ordinal",
            name="uq_workitem_threads_work_item_id",
        ),
        sa.UniqueConstraint("thread_id", name="uq_workitem_threads_thread_id"),
    )
    op.create_table(
        "workitem_external_links",
        sa.Column("work_item_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_workitem_external_links_ordinal_non_negative",
        ),
        sa.CheckConstraint(
            "provider IN ('github', 'linear', 'notion', 'jira', 'url')",
            name="ck_workitem_external_links_provider",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name="fk_workitem_external_links_work_item_id_work_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "work_item_id",
            "ordinal",
            name="pk_workitem_external_links",
        ),
    )
    op.create_table(
        "workitem_affected_repos",
        sa.Column("work_item_id", sa.Text(), nullable=False),
        sa.Column("repo_name", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_workitem_affected_repos_ordinal_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name="fk_workitem_affected_repos_work_item_id_work_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "work_item_id",
            "repo_name",
            name="pk_workitem_affected_repos",
        ),
        sa.UniqueConstraint(
            "work_item_id",
            "ordinal",
            name="uq_workitem_affected_repos_work_item_id",
        ),
    )
    op.create_table(
        "workitem_pr_urls",
        sa.Column("work_item_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_workitem_pr_urls_ordinal_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            name="fk_workitem_pr_urls_work_item_id_work_items",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "work_item_id",
            "url",
            name="pk_workitem_pr_urls",
        ),
        sa.UniqueConstraint(
            "work_item_id",
            "ordinal",
            name="uq_workitem_pr_urls_work_item_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("workitem_pr_urls")
    op.drop_table("workitem_affected_repos")
    op.drop_table("workitem_external_links")
    op.drop_table("workitem_threads")
    op.drop_table("workitem_tasks")
    op.drop_table("work_items")
    op.drop_table("task_dependencies")
    op.drop_table("task_switch_anchors")
    op.drop_table("task_pr_urls")
    op.drop_table("task_test_results")
    op.drop_table("task_repos")
    op.drop_table("tasks")
    op.drop_table("storage_metadata")
