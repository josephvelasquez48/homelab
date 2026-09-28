# Backend (`apps/api`)

The FastAPI service behind `api.home`: the gateway to Ollama, plus jobs,
auth and metrics. Builds on [milestone-1.md](milestone-1.md); the RAG
endpoints are in [rag.md](rag.md).

## Endpoints

| Endpoint | Auth | What |
|---|---|---|
| `GET /health` | open | Real checks: Postgres `SELECT 1`, Redis `PING` |
| `GET /ready` | open | Readiness; also sets `homelab_inference_reachable` ([monitoring.md](monitoring.md)) |
| `GET /metrics` | open | Prometheus metrics (`prometheus-fastapi-instrumentator`) |
| `POST /v1/chat` | API key | Generation through Ollama; answers cached in Redis for an hour |
| `POST /v1/embed` | API key | Embeddings with `nomic-embed-text` (768 dimensions), one string or a batch |
| `POST /v1/documents`, `POST /v1/rag/query` | API key | RAG ([rag.md](rag.md)) |
| `POST /jobs`, `GET /jobs/{id}` | API key | Background jobs |
| `/v1/conversations` (list, create, get, `POST .../messages`) | API key | Stored chat conversations for the chat assistant (`chat.home`), with optional Wikipedia sources ([rag.md](rag.md)) |

`/health` and `/metrics` stay open for probes and Prometheus; the LAN
boundary protects them.

## How it's built

- **Auth:** an `X-API-Key` header with a constant-time compare.
- **Rate limiting:** 60 requests a minute per API key, a fixed window in
  Redis (`INCR` + `EXPIRE`), on the same dependency as auth. Verified: a
  burst of 65 gave exactly 60 successes and five 429s (and 60/15 later
  under k6 - [load-testing.md](load-testing.md)).
- **Background jobs:** stored in Postgres, queued on a Redis list. A
  separate `worker` (same image, different command) runs them, so slow LLM
  generation doesn't hold an HTTP connection open.
- **Retries:** one shared Ollama client (`app/ollama.py`) with `tenacity` -
  3 attempts, exponential backoff, on connection and timeout errors only.
  Separate connect (5 s) and read (120 s) timeouts, after failure testing
  found a dead backend hung requests for minutes ([failure-testing.md](failure-testing.md)).
- **Migrations:** Alembic (synchronous, via `psycopg`; the app itself uses
  async `asyncpg`). They run as an Argo CD `PreSync` hook before new pods start.
- **Logging:** `structlog` JSON, with a request ID on every request
  (`X-Request-ID`) and its method, path, status and duration.

## Verified

Each feature was checked on its failure paths too, not just the happy
path: missing and wrong keys; job 404 and 422; retries firing against an
unreachable URL (2 retries logged, ~3 s); a cache hit returning identical
output in ~40 ms; a metrics counter reading exactly 1 after one request;
three different vectors from a batch of three.

## A bug worth remembering

The worker crashed on every queue poll with a Redis `TimeoutError`: the
client's socket timeout was shorter than its own `BLPOP ... timeout=5`, so
it gave up before Redis's blocking wait could return. A client's socket
timeout must be longer than any blocking command it sends
(`socket_timeout=10`).
