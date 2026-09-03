# RAG

Roadmap step 8. Implements the pipeline from the project plan:

```
Question -> FastAPI -> Embedding model -> pgvector -> Relevant documents -> Local LLM -> Answer
```

## Log

- 2026-09-03: `documents` table (pgvector column, HNSW index on cosine
  distance) via Alembic migration. `POST /v1/documents` embeds content
  through `nomic-embed-text` and stores it; `POST /v1/rag/query` embeds
  the question, retrieves the `top_k` nearest documents by cosine
  distance, builds a context-grounded prompt, and generates through the
  same Ollama gateway `/v1/chat` uses.

  No pgvector Python codec (e.g. the `pgvector` package's asyncpg
  integration) was needed - embeddings are only ever written via a
  `::vector` cast and compared via the `<=>` operator in SQL, never
  selected back out as a value, so a plain string literal
  (`[0.1,0.2,...]`) is sufficient on both sides.

  **Verified semantic retrieval, not just that the endpoints respond**:
  ingested three documents on unrelated topics (DNS/Pi, GPU/desktop,
  bananas), asked three different questions, and each one correctly
  retrieved and answered from its matching document - the banana
  document never surfaced for the GPU or DNS questions and vice versa.
  Distances were also sane (correct match: ~0.2-0.3 cosine distance;
  wrong-topic document in the GPU query's top-2: ~0.46).

## Known simplifications (fine for now, worth knowing about)

- **No chunking.** `POST /v1/documents` embeds and stores whatever
  content it's given as one unit. Fine for short facts (what this was
  tested with); a long document would need splitting into overlapping
  chunks before ingestion, which doesn't exist yet.
- **No distance threshold.** `/v1/rag/query` always returns `top_k`
  documents regardless of how irrelevant they are to the question - if
  the corpus has nothing related, it'll still hand the LLM the closest
  (but bad) matches rather than saying "no relevant documents." A
  threshold on `distance` would fix this if it becomes a real problem.
- **No delete/list endpoint** for documents yet - only insert and query.
