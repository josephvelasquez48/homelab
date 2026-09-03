# Backend (apps/api)

Tracks work on the FastAPI service beyond Milestone 1 - auth, validation,
rate limiting, caching, retries, migrations, structured logging, `/metrics`,
`/v1/embed` - per the original backend feature list. See
[milestone-1.md](milestone-1.md) for the initial FastAPI/Postgres/Redis/
Ollama setup this builds on.

## Log

- 2026-09-02: **Structured logging + module layout + background jobs.**
  Split the single `main.py` into `app/routers/{health,chat,jobs}.py` with
  shared `config.py`/`db.py`, since a flat file wasn't going to survive
  auth/rate-limiting/metrics being added on top. Logging switched to
  `structlog` (JSON output) with a request-logging middleware that stamps
  each request with an ID (`X-Request-ID` response header) and logs
  method/path/status/duration.

  Added `POST /jobs` + `GET /jobs/{id}`: jobs are persisted in Postgres
  (`jobs` table - id, type, status, payload, result, error, timestamps)
  and queued via a Redis list; a separate `worker` container (same image,
  different command) blocks on the queue and processes them. First job
  type is `chat` - runs the same Ollama generation as `/v1/chat`, but off
  the request path, which is the actual point: LLM generation is slow
  enough that it shouldn't hold an HTTP connection open.

  Schema is now managed with **Alembic** instead of hand-run SQL - a
  `migrate` one-shot service runs `alembic upgrade head` and must exit 0
  before `api`/`worker` start (`depends_on: condition:
  service_completed_successfully`). Alembic runs synchronously via
  `psycopg`, separate from the app's async `asyncpg` pool - normal split,
  migrations don't need to be async.

  **Debugging note:** the worker crashed on every single queue poll with a
  `redis.exceptions.TimeoutError`, immediately after logging
  `worker_started`. Root cause: the Redis client's default socket timeout
  was shorter than the `BLPOP ... timeout=5` blocking wait - the client
  gave up on the socket read before Redis's own blocking wait could
  return, even with nothing wrong on the Redis side. Fixed by setting
  `socket_timeout=10` on the client (must exceed any blocking command
  timeout used against it). Verified with a full job lifecycle
  (`pending` -> `running` -> `done`, result populated) plus the 404 and
  422 error paths, not just the happy path.

- 2026-09-03: **API-key auth + rate limiting.** `X-API-Key` header,
  constant-time compare, applied to `/v1/chat` and `/jobs` -
  `/health` stays open since container healthchecks and uncredentialed
  monitoring need to hit it. Rate limiting (60 req/min default, fixed
  window, Redis `INCR`+`EXPIRE`) is keyed on the API key rather than IP,
  so it rides the same auth dependency rather than being separate
  middleware. Verified: no key / wrong key / correct key / unauthenticated
  health, and a 65-request burst that produced exactly 60 successes then
  five `429`s.

- 2026-09-03: **Retries + caching.** Consolidated the Ollama-call logic
  `chat.py` and `worker.py` had each duplicated into `app/ollama.py` -
  the one place that needed retry logic was duplicated, so it would have
  needed retrying twice. `tenacity`, 3 attempts, exponential backoff, on
  connect/timeout errors only (not on e.g. a 4xx from Ollama itself,
  which retrying wouldn't fix). `/v1/chat` caches responses in Redis for
  an hour keyed on `(model, message)`. Verified retries actually fire
  (not just trusted the decorator) against a deliberately unreachable
  URL - 2 logged attempts, ~3.1s before final failure - and verified a
  cache hit returns identical output in ~40ms.

## Next

`GET /metrics` (Prometheus format), `POST /v1/embed`.
