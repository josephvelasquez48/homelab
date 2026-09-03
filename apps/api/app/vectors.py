def to_vector_literal(embedding: list[float]) -> str:
    """pgvector accepts its text input format directly via a ::vector cast -
    no extra codec/driver needed on top of asyncpg's plain string handling."""
    return "[" + ",".join(str(x) for x in embedding) + "]"
