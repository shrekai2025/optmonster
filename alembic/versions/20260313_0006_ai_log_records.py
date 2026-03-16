"""ai log records

Revision ID: 20260313_0006
Revises: 20260313_0005
Create Date: 2026-03-13 22:10:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260313_0006"
down_revision = "20260313_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_log_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=True),
        sa.Column("log_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("model_id", sa.String(length=200), nullable=True),
        sa.Column("request_payload", sa.JSON(), nullable=True),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_log_records")),
    )
    op.create_index(op.f("ix_ai_log_records_account_id"), "ai_log_records", ["account_id"], unique=False)
    op.create_index(op.f("ix_ai_log_records_log_type"), "ai_log_records", ["log_type"], unique=False)
    op.create_index(op.f("ix_ai_log_records_status"), "ai_log_records", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_log_records_status"), table_name="ai_log_records")
    op.drop_index(op.f("ix_ai_log_records_log_type"), table_name="ai_log_records")
    op.drop_index(op.f("ix_ai_log_records_account_id"), table_name="ai_log_records")
    op.drop_table("ai_log_records")
