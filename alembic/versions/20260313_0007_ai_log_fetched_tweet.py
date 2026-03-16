"""add fetched tweet id to ai logs

Revision ID: 20260313_0007
Revises: 20260313_0006
Create Date: 2026-03-13 22:45:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260313_0007"
down_revision = "20260313_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_log_records",
        sa.Column("fetched_tweet_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_ai_log_records_fetched_tweet_id"),
        "ai_log_records",
        ["fetched_tweet_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_log_records_fetched_tweet_id"), table_name="ai_log_records")
    op.drop_column("ai_log_records", "fetched_tweet_id")
