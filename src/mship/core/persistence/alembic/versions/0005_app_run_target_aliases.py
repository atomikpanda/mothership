"""Persist safe target aliases for selected app runs.

Revision ID: 0005_app_run_target_aliases
Revises: 0004_app_run_source_updates
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0005_app_run_target_aliases"
down_revision: str | None = "0004_app_run_source_updates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("app_runs") as batch:
        batch.add_column(
            sa.Column(
                "target_aliases_json",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("app_runs") as batch:
        batch.drop_column("target_aliases_json")
