"""Create immutable declared task-result metadata.

Revision ID: 0003_immutable_task_results
Revises: 0002_app_run_targets
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_immutable_task_results"
down_revision: str | None = "0002_app_run_targets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_results",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("task_slug", sa.Text(), nullable=False),
        sa.Column("work_item_id", sa.Text()),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("logical_task", sa.Text(), nullable=False),
        sa.Column("task_key", sa.Text(), nullable=False),
        sa.Column("host_name", sa.Text()),
        sa.Column("host_role", sa.Text()),
        sa.Column("host_endpoint_fingerprint", sa.Text()),
        sa.Column("worktree_identity", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text()),
        sa.Column("snapshot_identity", sa.Text()),
        sa.Column("env_runner_identity", sa.Text()),
        sa.Column("outcome_status", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("finished_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("outcome_status IN ('completed', 'failed', 'cancelled', 'infrastructure_error')", name="ck_task_results_outcome_status"),
        sa.CheckConstraint("(outcome_status = 'completed' AND exit_code = 0) OR (outcome_status = 'failed' AND exit_code IS NOT NULL AND exit_code <> 0) OR (outcome_status IN ('cancelled', 'infrastructure_error') AND exit_code IS NULL)", name="ck_task_results_outcome_exit_code"),
    )
    op.create_index("ix_task_results_workspace_task", "task_results", ["workspace_id", "task_slug", "created_at"])
    op.create_index("ix_task_results_workspace_item", "task_results", ["workspace_id", "work_item_id", "created_at"])
    op.create_table(
        "task_result_artifacts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("result_id", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer()),
        sa.Column("sha256", sa.Text()),
        sa.Column("availability", sa.Text(), nullable=False),
        sa.Column("safe_reason", sa.Text()),
        sa.Column("blob_locator", sa.Text()),
        sa.ForeignKeyConstraint(["result_id"], ["task_results.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("result_id", "ordinal"),
        sa.CheckConstraint("ordinal >= 0", name="ck_task_result_artifacts_ordinal_non_negative"),
        sa.CheckConstraint("availability IN ('published', 'missing', 'rejected', 'expired')", name="ck_task_result_artifacts_availability"),
        sa.CheckConstraint("(availability = 'published' AND byte_size IS NOT NULL AND sha256 IS NOT NULL AND blob_locator IS NOT NULL) OR availability <> 'published'", name="ck_task_result_artifacts_published_fields"),
    )
    op.create_index("ix_task_result_artifacts_blob", "task_result_artifacts", ["blob_locator"])


def downgrade() -> None:
    op.drop_index("ix_task_result_artifacts_blob", table_name="task_result_artifacts")
    op.drop_table("task_result_artifacts")
    op.drop_index("ix_task_results_workspace_item", table_name="task_results")
    op.drop_index("ix_task_results_workspace_task", table_name="task_results")
    op.drop_table("task_results")
