"""phase 2b action metadata

Revision ID: 20260312_0003
Revises: 20260312_0002
Create Date: 2026-03-12 23:40:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260312_0003"
down_revision = "20260312_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("action_requests", sa.Column("fetched_tweet_id", sa.Integer(), nullable=True))
    op.add_column("action_requests", sa.Column("ai_draft", sa.Text(), nullable=True))
    op.add_column("action_requests", sa.Column("edited_draft", sa.Text(), nullable=True))
    op.add_column("action_requests", sa.Column("final_draft", sa.Text(), nullable=True))
    op.add_column("action_requests", sa.Column("relevance_score", sa.Integer(), nullable=True))
    op.add_column("action_requests", sa.Column("reply_confidence", sa.Integer(), nullable=True))
    op.add_column("action_requests", sa.Column("llm_provider", sa.String(length=64), nullable=True))
    op.add_column("action_requests", sa.Column("llm_model", sa.String(length=200), nullable=True))
    op.add_column(
        "action_requests",
        sa.Column(
            "learning_status",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "action_requests",
        sa.Column("learning_applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_action_requests_fetched_tweet_id",
        "action_requests",
        ["fetched_tweet_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_action_requests_fetched_tweet_id", table_name="action_requests")
    op.drop_column("action_requests", "learning_applied_at")
    op.drop_column("action_requests", "learning_status")
    op.drop_column("action_requests", "llm_model")
    op.drop_column("action_requests", "llm_provider")
    op.drop_column("action_requests", "reply_confidence")
    op.drop_column("action_requests", "relevance_score")
    op.drop_column("action_requests", "final_draft")
    op.drop_column("action_requests", "edited_draft")
    op.drop_column("action_requests", "ai_draft")
    op.drop_column("action_requests", "fetched_tweet_id")
