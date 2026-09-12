"""record which passages an answer was given

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-12
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retrieved passages were first appended to the reply as a text footer,
    # which avoided a migration and was wrong for two reasons. The citations
    # replayed into the model's context on every following turn, spending
    # budget on a list it had already used. And text cannot hold the passage
    # itself, so there was no way to show what the model was actually given
    # rather than just which article it came from.
    #
    # JSONB rather than a sources table with a foreign key: these are a
    # snapshot of one answer, never queried across messages, and never
    # updated. A join table would model a relationship that does not exist.
    #
    # Nullable rather than defaulting to an empty array, because "this turn
    # did not search" and "this turn searched and found nothing" are
    # different facts and the column is the only place that distinction
    # survives.
    op.execute("ALTER TABLE messages ADD COLUMN sources JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS sources")
