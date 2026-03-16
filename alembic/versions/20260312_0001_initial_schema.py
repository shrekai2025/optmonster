"""initial schema

Revision ID: 20260312_0001
Revises:
Create Date: 2026-03-12 19:30:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260312_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_runtime_states",
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("twitter_handle", sa.String(length=100), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("config_revision", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.String(length=500), nullable=False),
        sa.Column("last_auth_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cookie_freshness", sa.String(length=32), nullable=False),
        sa.Column("proxy_health", sa.String(length=32), nullable=False),
        sa.Column("failure_streak", sa.Integer(), nullable=False),
        sa.Column("pause_reason", sa.String(length=64), nullable=True),
        sa.Column("last_fetch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fetch_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("account_id", name="pk_account_runtime_states"),
    )
    op.create_table(
        "fetch_cursors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("cursor", sa.String(length=255), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_fetch_cursors"),
        sa.UniqueConstraint(
            "account_id",
            "source_type",
            "source_key",
            name="uq_fetch_cursor_source",
        ),
    )
    op.create_index("ix_fetch_cursors_account_id", "fetch_cursors", ["account_id"], unique=False)
    op.create_table(
        "fetched_tweets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("tweet_id", sa.String(length=64), nullable=False),
        sa.Column("author_handle", sa.String(length=100), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("lang", sa.String(length=16), nullable=True),
        sa.Column("created_at_twitter", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_fetched_tweets"),
        sa.UniqueConstraint(
            "account_id",
            "source_type",
            "source_key",
            "tweet_id",
            name="uq_fetched_tweet_dedupe",
        ),
    )
    op.create_index("ix_fetched_tweets_account_id", "fetched_tweets", ["account_id"], unique=False)
    op.create_table(
        "operation_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("operation_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operation_logs"),
    )
    op.create_index("ix_operation_logs_account_id", "operation_logs", ["account_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_operation_logs_account_id", table_name="operation_logs")
    op.drop_table("operation_logs")
    op.drop_index("ix_fetched_tweets_account_id", table_name="fetched_tweets")
    op.drop_table("fetched_tweets")
    op.drop_index("ix_fetch_cursors_account_id", table_name="fetch_cursors")
    op.drop_table("fetch_cursors")
    op.drop_table("account_runtime_states")
