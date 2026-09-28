# Failure testing

Roadmap step 17. Five failures injected on purpose, each to answer one
question about how the system behaves when something dies.
`failure-testing/watch.sh` sent one health check per second through each
test, so downtime was measured from real requests, not estimated.

## Results

| Test | What happened | Downtime |
|---|---|---|
| Kill one `api` pod (2 replicas, `maxUnavailable: 0`) | The other replica served everything; the replacement was Ready in ~26 s | **None** - 26 of 26 checks returned 200 |
| Kill Redis (single instance) | `/health` reported the failure straight away (500), then recovered on its own | ~6.5 s |
| Kill Postgres (single instance, persistent volume) | Same pattern, and the same document was queried before and after - same content, same search result | ~6.6 s, no data lost |
| Stop Ollama on the desktop | **A request hung for over 180 s** - found a real bug (below) | Fixed: a clean 502 in 18 s |
| Scale `worker` to 0 by hand | Argo CD put it back | ~11 s |

## The bug it found: a dead AI backend hung requests

With Ollama stopped, a `/v1/chat` request never came back - still in
flight after 180 s.

**Cause, from reading the code:** one blanket `timeout=120` covered both
connecting and waiting for a slow LLM answer, and the retry limited the
*number* of attempts (3), not the total time. If the connection attempt
is silently dropped rather than refused, each attempt waits the full
120 s: up to about 6 minutes in total.

**Fix:** separate limits - `httpx.Timeout(connect=5, read=120, write=10,
pool=5)`. Connecting on the LAN takes milliseconds; generating can take
tens of seconds. Shipped through CI and Argo CD, then the same test was
rerun against the deployed fix: **502 "AI backend unavailable" in 18 s**
(3 attempts × 5 s, plus backoff).

Side finding: Ollama restarted from a stale PowerShell session bound to
`127.0.0.1` despite the machine-wide `OLLAMA_HOST` - fixed by setting it
in the launch command.

## Why self-heal took 11 s, not 3 minutes

The ~3-minute figure in [argocd.md](argocd.md) is how often Argo CD polls
*git*. Drift in the cluster is different: Argo CD watches live resources
and reverts a change within seconds. Only a new commit waits for the poll.

## Not tested

- **Losing the Pi.** Postgres, Redis, Traefik and Argo CD all run on it
  with no failover, so the answer is a full outage until it's back; the
  number worth measuring would be recovery time after a reboot.
- **A network split between nodes**, on purpose. The real cross-node
  failures found earlier are in [kubernetes.md](kubernetes.md).
