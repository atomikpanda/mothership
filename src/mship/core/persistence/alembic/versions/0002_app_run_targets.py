"""Create durable selected-target metadata.

Revision ID: 0002_app_run_targets
Revises: 0001_tasks_and_workitems
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_app_run_targets"
down_revision: str | None = "0001_tasks_and_workitems"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("task_slug", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("profile_revision", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("backend_revision", sa.Text(), nullable=False),
        sa.Column("host_name", sa.Text(), nullable=False),
        sa.Column("host_scope", sa.Text(), nullable=False),
        sa.Column("host_endpoint_fingerprint", sa.Text(), nullable=False),
        sa.Column("safe_target_label", sa.Text(), nullable=False),
        sa.Column("private_binding_ref", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("capabilities_json", sa.Text(), nullable=False),
        sa.Column("owner_ref", sa.Text()),
        sa.Column("owner_generation", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("binary_provenance_json", sa.Text()),
        sa.CheckConstraint(
            "host_scope IN ('user', 'project')", name="ck_app_runs_host_scope"
        ),
        sa.CheckConstraint("protocol_version = 1", name="ck_app_runs_protocol_version"),
        sa.CheckConstraint(
            "status IN ('starting', 'active', 'stopped', 'failed', 'unknown')",
            name="ck_app_runs_status",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND owner_ref IS NOT NULL AND owner_generation IS NOT NULL) "
            "OR (status <> 'active' AND owner_ref IS NULL AND owner_generation IS NULL)",
            name="ck_app_runs_owner_acknowledgement",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_app_runs_revision_non_negative"),
        sa.ForeignKeyConstraint(
            ["task_slug"],
            ["tasks.slug"],
            name="fk_app_runs_task_slug_tasks",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_slug", "repo"],
            ["task_repos.task_slug", "task_repos.repo_name"],
            name="fk_app_runs_task_repo_task_repos",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_app_runs"),
    )
    op.create_index(
        "ix_app_runs_task_repo_status",
        "app_runs",
        ["task_slug", "repo", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_app_runs_task_repo_status", table_name="app_runs")
    op.drop_table("app_runs")
