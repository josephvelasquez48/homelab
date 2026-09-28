# RAG (retrieval-augmented generation)

Roadmap step 8. Two ways to ground the local LLM's answers in real
documents:

```
Your documents:  question -> embed (nomic-embed-text) -> pgvector -> top matches -> LLM -> answer
Wikipedia:       question -> subject words -> Xapian index (zimsearch) -> passages -> LLM -> answer with citations
```

## 1. Your documents: pgvector

- `POST /v1/documents` embeds the text and stores it in a `documents`
  table (pgvector column, HNSW index on cosine distance).
- `POST /v1/rag/query` embeds the question, fetches the `top_k` nearest
  documents, and asks the LLM to answer from them.
- No pgvector Python library needed: vectors go in with a `::vector` cast
  and are only compared in SQL (`<=>`).

**Verified on meaning, not just that it responds:** three documents on
unrelated topics (DNS, GPU, bananas) and three questions - each question
retrieved its own document (distance ~0.2-0.3; the nearest wrong one ~0.46).

**Simplifications:** no chunking (a document is stored whole), no distance
cut-off (it always returns `top_k`, however poor), and no list or delete
endpoints.

## 2. Wikipedia: full-text search, no embeddings

The chat assistant can search a local copy of Wikipedia. Embedding millions
of articles on one 8 GB GPU would take weeks and outgrow the 127 GB
archive, but the archive already ships a Xapian full-text index built by
Kiwix. So `apps/zimsearch` reads it with the libzim bindings and returns
JSON. It runs beside kiwix-serve, because the archive lives on the Pi's
disk and mounting it into the API would pin the API to the Pi.

How it answers:

- **Simple English first, then the full archive.** Simple English articles
  cost a fraction of a 7B model's context. The fallback triggers when the
  top titles don't name the subject - hit counts can't decide it (Xapian
  matches almost anything), and scores can't be compared across two indexes.
- **Only the subject words go to Xapian.** Question words steered it
  wrong: "who was Ada Lovelace" found Federico Menabrea.
- **Opt-in per message**, since most chat turns aren't lookups.
- **Citations:** the model marks borrowed claims `[1]`, and the page links
  each to the article and shows the excerpt the model was actually given -
  the only way to tell a grounded answer from one that just mentions the
  article. Stored in a nullable `sources` column: "didn't search" and
  "searched, found nothing" are different facts.

### Measured

`apps/zimsearch/eval` has 40 labelled questions, run against the real
archives on the Pi (the archives aren't in CI):

```
python3 run.py http://127.0.0.1:8096=deployed http://127.0.0.1:8097=branch
```

| Questions | Right article first - before | After | In top 3 - after |
|---|---|---|---|
| Common topics | 5 of 15 | 11 of 15 | 13 of 15 |
| Obscure topics | 8 of 15 | 15 of 15 | 15 of 15 |
| Unusual phrasing | 3 of 10 | 6 of 10 | 8 of 10 |
| **All 40** | **16** | **32** | **36** |

Two got worse ("capital of Australia" now finds the Australian Capital
Territory; "inflation in economics" ranks Stagflation first). Still wrong
either way: misspellings, questions whose answer isn't their subject
("current president of France"), and non-English questions.

**Load:** about 50 searches/s on its one-CPU limit, no failures, memory
flat at ~130 MiB. A synchronous version was measured too - no faster (the
CPU is the limit) and twice the memory - so it stays async.

### Not done yet

- **No reranking.** Xapian's order is a good shortlist and a poor final
  answer. The API over-fetches `candidates` for a future reranker and says
  `reranked: false`.
- **No query rewriting.** A follow-up like "and what about his brother"
  has almost no searchable words; fixing that needs a model call before the
  first token.
