"""fetch schedule runtime state

Revision ID: 20260313_0005
Revises: 20260313_0004
Create Date: 2026-03-13 18:35:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260313_0005"
down_revision = "20260313_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_runtime_states",
        sa.Column("next_fetch_not_before_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("account_runtime_states", "next_fetch_not_before_at")
