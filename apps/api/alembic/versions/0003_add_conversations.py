"""add conversations and messages for multi-turn chat

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-11
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE conversations (
            id UUID PRIMARY KEY,
            title TEXT,
            model TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # ON DELETE CASCADE rather than application-side cleanup: a conversation
    # without its messages is meaningless, and leaving orphan rows behind is
    # the kind of thing nobody notices until the table is large.
    op.execute(
        """
        CREATE TABLE messages (
            id UUID PRIMARY KEY,
            conversation_id UUID NOT NULL
                REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Every read of a conversation is "its messages, oldest first". Without
    # this the planner sorts the whole table once a conversation gets long.
    op.execute(
        "CREATE INDEX messages_conversation_created_idx "
        "ON messages (conversation_id, created_at)"
    )
    # The conversation list is ordered by recency, which is a different
    # question from the one above and needs its own index.
    op.execute("CREATE INDEX conversations_updated_idx ON conversations (updated_at DESC)")


def downgrade() -> None:
    # messages first: the foreign key makes the reverse order fail.
    op.execute("DROP TABLE IF EXISTS messages")
    op.execute("DROP TABLE IF EXISTS conversations")
