# Load testing

Roadmap step 16. Four k6 scripts in `load-testing/`, each answering one
question. Results were cross-checked against the cluster's own
Prometheus, not just k6's summary.

k6 ran on the Windows host, not the WSL2 node: connections from there
hung about half the time, and a load test shouldn't measure its own flaky
client.

## Results

| Script | Question | Result |
|---|---|---|
| `smoke.js` | Does every endpoint work, including a 401 without a key? | 16 of 16 checks passed |
| `health-load.js` | How much can 2 `api` replicas take? (ramp to 50 users, `/health`) | **466 req/s, 0% errors** over 65,316 requests; p95 221 ms, p99 328 ms |
| `rate-limit.js` | Does the 60/min limit trip at exactly the right request? | 60 succeeded, 15 got 429, **first 429 at request 61** - no off-by-one, nothing leaked past |
| `soak.js` | Does anything drift under 5.5 minutes of load? (15 users) | **476 req/s, 0% errors** over 157,009 requests; p95 74 ms; memory flat |

`/health` is the only endpoint with no key and no rate limit, so it's the
only one that shows raw HTTP throughput. The rate limiter is keyed per API
key, and there's only one key, so the limit test checks correctness rather
than load.

## Checked in Prometheus

- **Request rate** matched k6: ~470-484 req/s during the hold.
- **p95 latency** spiked to ~450 ms for two samples while load ramped up,
  then held at ~95 ms.
- **Memory** stepped from ~80 to ~83 MB when load started, then stayed flat
  for 9 minutes - no leak.
- **CPU: each pod ran at its full 500m limit.** That's the headroom finding:
  more load would hit CPU throttling first, so raise the limit or add a
  third replica before touching Postgres or Redis (barely loaded).

## Not tested

- **The AI endpoints under real load.** With one API key the rate limiter
  caps concurrency; a real GPU test needs multiple keys or a controlled
  bypass.
- Postgres or Redis under load or down - see [failure-testing.md](failure-testing.md).
- CPU and memory are the process's own numbers (`process_*`): cAdvisor's
  container metrics aren't scraped. Fine here - one process per pod.
