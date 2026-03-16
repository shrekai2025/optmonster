"""account follower snapshots

Revision ID: 20260313_0004
Revises: 20260312_0003
Create Date: 2026-03-13 10:20:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260313_0004"
down_revision = "20260312_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_follower_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("follower_count", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_follower_snapshots")),
        sa.UniqueConstraint(
            "account_id",
            "snapshot_date",
            name="uq_account_follower_snapshot_day",
        ),
    )
    op.create_index(
        op.f("ix_account_follower_snapshots_account_id"),
        "account_follower_snapshots",
        ["account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_account_follower_snapshots_account_id"),
        table_name="account_follower_snapshots",
    )
    op.drop_table("account_follower_snapshots")
