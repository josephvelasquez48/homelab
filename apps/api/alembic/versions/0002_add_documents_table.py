"""add documents table for RAG

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE documents (
            id UUID PRIMARY KEY,
            content TEXT NOT NULL,
            embedding vector(768) NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # HNSW: pgvector's modern index type, better query performance than
    # ivfflat at this scale and doesn't need a training/list-count tuned
    # to the row count up front.
    op.execute(
        "CREATE INDEX documents_embedding_idx ON documents "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE documents")
