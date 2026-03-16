"""phase 2a action requests

Revision ID: 20260312_0002
Revises: 20260312_0001
Create Date: 2026-03-12 22:10:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260312_0002"
down_revision = "20260312_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_runtime_states",
        sa.Column(
            "execution_mode",
            sa.String(length=32),
            nullable=False,
            server_default="read_only",
        ),
    )

    op.create_table(
        "action_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("trigger_source", sa.String(length=64), nullable=False),
        sa.Column("requested_execution_mode", sa.String(length=32), nullable=False),
        sa.Column("applied_execution_mode", sa.String(length=32), nullable=True),
        sa.Column("target_tweet_id", sa.String(length=64), nullable=True),
        sa.Column("target_user_handle", sa.String(length=100), nullable=True),
        sa.Column("content_draft", sa.Text(), nullable=True),
        sa.Column("budget_snapshot", sa.JSON(), nullable=True),
        sa.Column("audit_log", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_action_requests"),
    )
    op.create_index(
        "ix_action_requests_account_id",
        "action_requests",
        ["account_id"],
        unique=False,
    )
    op.create_index("ix_action_requests_status", "action_requests", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_action_requests_status", table_name="action_requests")
    op.drop_index("ix_action_requests_account_id", table_name="action_requests")
    op.drop_table("action_requests")
    op.drop_column("account_runtime_states", "execution_mode")
