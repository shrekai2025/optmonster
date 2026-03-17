"""add fetched tweet engagement metrics

Revision ID: 20260317_0008
Revises: 20260313_0007
Create Date: 2026-03-17 10:30:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260317_0008"
down_revision = "20260313_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fetched_tweets", sa.Column("view_count", sa.Integer(), nullable=True))
    op.add_column("fetched_tweets", sa.Column("like_count", sa.Integer(), nullable=True))
    op.add_column("fetched_tweets", sa.Column("retweet_count", sa.Integer(), nullable=True))
    op.add_column("fetched_tweets", sa.Column("reply_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("fetched_tweets", "reply_count")
    op.drop_column("fetched_tweets", "retweet_count")
    op.drop_column("fetched_tweets", "like_count")
    op.drop_column("fetched_tweets", "view_count")
