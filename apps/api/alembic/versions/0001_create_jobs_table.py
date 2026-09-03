"""create jobs table

Revision ID: 0001
Revises:
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jobs (
            id UUID PRIMARY KEY,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            result JSONB,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX jobs_status_idx ON jobs (status)")


def downgrade() -> None:
    op.execute("DROP TABLE jobs")
