"""Persist staged source-update receipts for selected app runs.

Revision ID: 0004_app_run_source_updates
Revises: 0003_immutable_task_results
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004_app_run_source_updates"
down_revision: str | None = "0003_immutable_task_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("app_runs") as batch:
        batch.add_column(sa.Column("source_update_receipt_json", sa.Text(), nullable=True))
        batch.drop_constraint("ck_app_runs_status", type_="check")
        batch.drop_constraint("ck_app_runs_active_owner_acknowledgement", type_="check")
        batch.create_check_constraint(
            "ck_app_runs_status",
            "status IN ('starting', 'active', 'updating', 'stopped', 'failed', 'unknown')",
        )
        batch.create_check_constraint(
            "ck_app_runs_active_owner_acknowledgement",
            "status NOT IN ('active', 'updating') OR owner_ref IS NOT NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("app_runs") as batch:
        batch.drop_constraint("ck_app_runs_active_owner_acknowledgement", type_="check")
        batch.drop_constraint("ck_app_runs_status", type_="check")
        batch.drop_column("source_update_receipt_json")
        batch.create_check_constraint(
            "ck_app_runs_status",
            "status IN ('starting', 'active', 'stopped', 'failed', 'unknown')",
        )
        batch.create_check_constraint(
            "ck_app_runs_active_owner_acknowledgement",
            "status <> 'active' OR owner_ref IS NOT NULL",
        )
