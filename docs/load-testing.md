# Load testing

Roadmap step 16. Four k6 scripts under `load-testing/`, each answering a
different question rather than one generic "hammer it and see" run:
correctness first, then real throughput scaling, then the rate limiter's
own correctness, then whether load held for minutes (not seconds) reveals
anything a short burst wouldn't.

## Why k6

Chosen over Locust/wrk/hey for real overlap with the tools this project
already runs: it's the tool Grafana Labs builds, and results below are
cross-checked against this project's own Prometheus/Grafana stack rather
than taken purely from k6's own summary - genuine integration, not just a
CLI that happens to print numbers.

## Where the load generator runs

**Not** from the WSL2 desktop worker node, despite it being right there and
already used for other cluster work this session. Testing surfaced
intermittent TCP connection failures specific to that node's mirrored
networking (`curl` to `api.home` would hang or time out roughly half the
time, while ICMP to the same host was instantly reliable) - a client-side
networking artifact, unrelated to the flannel VXLAN issue fixed in
[docs/kubernetes.md](kubernetes.md), and not something a load test should
be measuring by accident. Running the load generator on a flaky client
would silently mix "test harness dropped the connection" into "the API
failed under load" - two completely different findings. Moved k6 to the
Windows host directly (`winget install GrafanaLabs.k6`), confirmed
reliable, and ran everything from there instead.

## Scripts (`load-testing/`)

- **`smoke.js`** - one VU, one pass through every endpoint (`/health`,
  `/v1/chat`, `/v1/embed`, `/v1/documents`, `/v1/rag/query`, `/jobs`
  POST+GET) plus a deliberate unauthenticated request expecting a 401.
  Correctness gate before any real load - a load test result is
  meaningless if the pipeline itself is broken.
- **`health-load.js`** - ramps to 50 concurrent VUs against `/health`
  only. The only endpoint that's both unauthenticated and unrate-limited,
  so it's the only one that can show genuine HTTP-layer throughput
  scaling (2 `api` replicas, Postgres/Redis connection pools, 100m/500m
  CPU + 128Mi/256Mi memory limits per pod).
- **`rate-limit.js`** - a correctness test, not a throughput test. The
  app currently has exactly one configured `API_KEY`, and the limiter is
  keyed per-API-key with a fixed 60s window (see
  [docs/secrets.md](secrets.md) and `apps/api/app/rate_limit.py`) - so
  there's no way to generate genuine multi-tenant load against a
  rate-limited endpoint right now. What's worth verifying instead: does
  the limit trigger at exactly the right request, and is the response
  the documented shape. Uses `/v1/embed` (single forward pass, no
  generation) rather than `/v1/chat` so a 75-request burst completes in
  well under a minute.
- **`soak.js`** - 15 VUs against `/health`, held for 5.5 minutes rather
  than a short spike. A brief burst can't show a slow memory leak or
  connection pool exhaustion; only sustained load can.

## Results

### Smoke test

All 16 checks passed, including the negative case (missing `X-API-Key`
correctly returns 401). Nothing further to report here - this test exists
to fail loudly if something's broken, and it didn't.

### Health load test (ramp to 50 VUs)

| Metric | Value |
|---|---|
| Total requests | 65,316 |
| Duration | 2m20s (20s ramp / 40s ramp / 60s hold / 20s down) |
| Throughput | 466.5 req/s average |
| Error rate | 0.00% |
| p90 latency | 202.0ms |
| p95 latency | 220.8ms (threshold: <500ms) |
| p99 latency | 328.3ms (threshold: <1500ms) |
| Max latency | 673.4ms |

Zero failed requests across all 65k - both thresholds passed comfortably,
not just barely. For 2 replicas on modest resource limits, this is a
solid result for the "cheap path" (DB + Redis ping, no AI inference).

### Rate-limit correctness test

Burst of 75 requests to `/v1/embed` with the single configured API key,
aligned to start right after a fresh 60-second window boundary:

| Metric | Value |
|---|---|
| Succeeded (200) | 60 |
| Rate-limited (429) | 15 |
| Unexpected status codes | 0 |
| First 429 | request #61 |

Exactly correct - the limit is 60/min, and the 61st request in the same
window was the first one rejected. No off-by-one, no requests silently
dropped instead of returning 429, no leakage past the limit under a fast
burst (a real risk if the increment-and-check in the rate limiter weren't
atomic under concurrent access - it evidently is). `/v1/embed` itself
responded in 30-77ms per request throughout, confirming the limiter is
the actual bottleneck here, not the endpoint.

### Soak test (15 VUs, 5.5 minutes)

k6 client-side:

| Metric | Value |
|---|---|
| Total requests | 157,009 |
| Throughput | 475.8 req/s average |
| Error rate | 0.00% |
| p95 latency | 73.8ms (threshold: <500ms) |

Cross-checked against Prometheus/Grafana rather than trusting the client
number alone:

- **Request rate** (server-side, `sum(rate(http_requests_total[1m]))`):
  ramped 0 -> 152 -> ~470-484 req/s and held there for the full hold
  period - matches k6's client-observed rate closely.
- **p95 latency** (`histogram_quantile(0.95, ...)`): one brief spike to
  ~410-460ms during the VU ramp-up (two data points, ~2 minutes in),
  then flat at ~95ms for the entire rest of the run. The ramp-up spike
  is expected - it's when both `api` replicas' CPU usage jumps from
  idle to their steady-state level; once that settles, latency does too.
- **Memory** (`process_resident_memory_bytes`, both `api` pods): one
  step up from ~80MB baseline to ~82-83MB when load started, then
  **completely flat** for the remaining ~9 minutes of data at 60s
  resolution - no slow growth, no leak. This is real evidence, not an
  assumption - a leak at this request volume over 5.5 minutes would have
  shown a visible upward slope.
- **CPU** (`rate(process_cpu_seconds_total[1m])`, both `api` pods): idle
  (~0.2%) before load, steady ~48-50% of one core per replica throughout
  the hold period.

**The CPU number is the most actionable finding here.** At 475 req/s
against `/health` split across 2 replicas, each pod is running at
essentially its full 500m CPU limit (0.48-0.50 of 500m) with zero
throttling-related errors observed - but there's no headroom left in this
configuration. The next load increase would very plausibly hit CPU
throttling before any other failure mode. If this needs to scale further,
raising the `api` Deployment's CPU limit or adding a third replica is the
first lever to pull, not something further down the stack (Postgres/Redis
weren't under meaningful load at all during this test - `/health` barely
touches them).

## Known gaps

- No real load test exists yet for the AI-backed endpoints
  (`/v1/chat`, `/v1/embed`, `/v1/rag/query`) beyond correctness checks -
  the single-API-key limitation caps meaningful concurrency there at the
  rate limiter itself. A genuine throughput/GPU-saturation test would
  need either a multi-tenant API key model (a real product change, not a
  test-script change) or deliberately bypassing the rate limiter for a
  controlled internal test - neither done here.
- No test of behavior when Postgres or Redis itself is under load or
  unavailable (that's roadmap step 17, failure testing, not this step).
- `container_memory_working_set_bytes` / `container_cpu_usage_seconds_total`
  (kubelet/cAdvisor metrics) aren't scraped by this Prometheus setup -
  see [docs/monitoring.md](monitoring.md) - so the CPU/memory numbers
  above are the FastAPI process's own reported usage
  (`process_resident_memory_bytes` / `process_cpu_seconds_total`), not
  the cgroup/container view. Close enough for this app (each pod runs
  one process), but worth knowing if reproducing this against something
  with multiple processes per container.
