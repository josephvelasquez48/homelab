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

## A second retrieval path: Wikipedia, without embeddings

Added 2026-09-12. The chat assistant can search a local copy of Wikipedia,
and it deliberately does not go through the pipeline above.

Embedding it was never realistic. The full archive is millions of articles;
generating that many vectors on one 8GB card would take weeks and produce a
pgvector table larger than the 127GB source. But the archive already ships a
Xapian full-text index built by Kiwix, so the retrieval problem was solved
before it started - the work was exposing it, not building it.

```
Question -> zimsearch -> Xapian index -> passages -> conversation context -> LLM
```

`apps/zimsearch` reads the archive through the libzim Python bindings and
returns JSON. It runs beside kiwix-serve rather than inside the api, because
the archive is a hostPath on `joe` and mounting it into the api would pin
the api to that node and cost its second replica.

Two archives are tried in order rather than merged. Simple English answers
when it has the topic, since its articles cost a fraction of the context on
a 7B model, and the full archive covers the long tail it misses. Falling
through is decided on hit count, not on relevance score, because Xapian
scores are not comparable across separate indexes.

Retrieval is opt-in per message. Most turns in a conversation are not
lookups, and searching an encyclopedia for "say that again shorter" returns
articles about rewriting and shortness that then crowd out the actual
conversation.

Citations are appended to the reply text rather than stored as metadata, so
a conversation reopened later still shows where its answers came from
without a schema change.

### What this does not do

- **No reranking.** Xapian ranks on term statistics and has no idea what the
  question means, so its ordering is a good shortlist and a poor final
  answer. The `candidates` parameter over-fetches for a reranking step that
  does not exist yet; the response reports `reranked: false` rather than
  implying otherwise.
- **No query rewriting.** The user's message is sent to Xapian as written,
  so a follow-up like "and what about his brother" carries almost no usable
  terms. Retrieval is weakest exactly where a conversation is most
  conversational. Fixing it means a model round trip before the first token.

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
