# Failure testing

Roadmap step 17. Five scenarios, each chosen to answer a specific
question about how the system actually behaves when a dependency dies,
not just whether it comes back eventually. Methodology:
`failure-testing/watch.sh` runs a one-request-per-second health-check
loop through the entire failure window, so downtime is measured directly
from real request/response pairs rather than estimated from before/after
snapshots or Kubernetes' own reported pod status.

Scoped out deliberately: a full Pi/control-plane failure. Postgres,
Redis, Traefik, and Argo CD all run on the single Pi node with no HA -
losing it is closer to "the whole stack is down until it reboots" than a
graceful-degradation scenario worth measuring. Documented as a known gap
below rather than staged as a real outage.

## 1. Kill an `api` pod (2 replicas, `maxUnavailable: 0`)

```
kubectl delete pod <one api pod> -n backend
```

**Result: zero downtime.** 26 consecutive health checks, one per second,
spanning the delete and the ~26s it took the replacement pod to become
`Ready` - every single one returned 200. The Service kept routing to the
surviving replica the whole time; nothing about this was a coincidence
of timing, it's exactly what `maxUnavailable: 0` plus 2 replicas is
supposed to guarantee, confirmed rather than assumed.

## 2. Kill Redis (single instance, no replica)

```
kubectl delete pod -n backend -l app=redis
```

**Result: ~6.5 seconds of correctly-reported downtime, then automatic
recovery.** `/health` (which explicitly pings Redis) returned 6
consecutive `500`s, then went straight back to `200` once the replacement
pod's readiness probe passed. No hang, no silent "ok" while actually
broken - the failure surfaced immediately and loudly, exactly what you'd
want from a health check.

## 3. Kill Postgres (single instance, `local-path` PVC)

```
kubectl delete pod -n data -l app=postgres
```

**Result: ~6.6 seconds of downtime (near-identical profile to Redis),
plus confirmed data persistence.** Queried the RAG document created
during the k6 smoke test
(`docs/load-testing.md`, id `fa33edf8-3066-4155-be14-4e6cc45a9eac`)
immediately before killing the pod, and again after recovery - same id,
same content, same embedding-search result both times. This proves the
PVC genuinely persists data across a pod restart, not just that the new
pod came up healthy.

## 4. Stop Ollama (native process on the desktop, not containerized)

This is the scenario that actually found something. Baseline `/v1/chat`
request: 200 OK in 0.66s. Stopped both the `ollama` and `ollama app`
processes, then sent the same request.

**Result: the request hung for over 180 seconds without resolving.** Not
6 seconds like Redis/Postgres - a curl with a 180s timeout wrapper never
got a response at all; the API pod's own request logs show no
`request_completed` entry for the request during that entire window,
confirming it was genuinely still in flight, not lost or dropped
somewhere else.

**Root cause, found by reading the code, not guessing:**

```python
# apps/api/app/main.py
app.state.ollama = httpx.AsyncClient(base_url=OLLAMA_URL, timeout=120.0)

# apps/api/app/ollama.py
@tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    retry=tenacity.retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    ...
)
async def generate(client, model, prompt): ...
```

`stop_after_attempt(3)` bounds the *number* of tries, not the *total
time* - and each individual attempt can itself take up to the full 120s
client timeout before it even raises an exception for tenacity to retry
on. Worst case: 3 x 120s + up to ~20s of backoff between attempts,
theoretically approaching **6 minutes** before the request finally
resolves to the documented `502 "AI backend unavailable"`. This session
confirmed it exceeds 180s in practice; it wasn't worth burning more time
proving the exact multi-minute figure once the point was already made -
the finding doesn't need a precise number to be actionable.

**Why a single attempt takes so long instead of failing fast**: a closed
TCP port normally returns an immediate `RST` (`ConnectError` almost
instantly), which is what a 120s timeout is *sized* for - the connect
failing fast, then that generous window covering slow LLM generation on
a later, successful attempt. That assumption breaks if the SYN is
silently dropped instead of rejected. This project already found and
fixed exactly that failure mode once this session - Windows silently
dropping unsolicited traffic rather than refusing it, documented in
[docs/kubernetes.md](kubernetes.md)'s flannel VXLAN incident. Plausible
the same category of behavior is at play here too, though unconfirmed -
what's confirmed is that the code has no defense against it either way.

**The actual gap**: `timeout=120.0` is a single blanket value covering
both "how long to wait to connect" and "how long to wait for a slow LLM
response" - two very different things with very different reasonable
bounds. A tight connect timeout (a few seconds - LAN, not internet) separate
from a generous read timeout (real generation can legitimately take
tens of seconds) would make a dead backend fail in seconds instead of
minutes, without touching how long a slow-but-working generation is
allowed to take. Recorded here as a finding, not fixed in this pass -
worth a follow-up now that it's understood precisely.

Recovery: restarting Ollama also surfaced a smaller, separate issue -
the relaunched process bound to `127.0.0.1:11434` instead of
`0.0.0.0:11434` despite `OLLAMA_HOST` being set as a persistent Machine
environment variable, because the PowerShell session that launched it
had a stale cached environment. Fixed by setting `$env:OLLAMA_HOST`
explicitly in the same launch command rather than relying on inheritance.
Confirmed full recovery afterward: `/v1/chat` back to a normal ~17ms
cached response.

## 5. Argo CD self-heal drift correction

```
kubectl scale deployment worker -n backend --replicas=0
```

**Result: corrected in ~11 seconds**, not the ~3 minutes
[docs/argocd.md](argocd.md) documents as Argo CD's default polling
interval. K8s events confirm the exact timing:

```
36s   Scaled down replica set worker-66874f678c from 1 to 0   (the manual kubectl scale)
25s   Scaled up replica set worker-66874f678c from 0 to 1     (Argo CD's selfHeal)
```

**This is a real clarification of the earlier docs, not a contradiction**:
the ~3-minute figure is specifically about how often Argo CD polls *git*
for new commits (no webhook configured, by design - see argocd.md's
"Known gaps"). Correcting *live drift* against a manifest it has already
synced doesn't require re-polling git at all - Argo CD watches the
cluster's actual state via the Kubernetes watch API and reacts to a
resource diverging from its last-known-desired state immediately. Only a
*new* git commit is bottlenecked on the 3-minute poll; an out-of-band
`kubectl` change to something Argo CD already manages gets reverted
almost in real time.

## Known gaps

- No test of a full Pi/control-plane failure - see the note at the top.
  If pursued, the honest expectation going in is a total outage with no
  graceful degradation, since there's no control-plane or data-tier HA;
  the interesting number to measure would be recovery time on reboot,
  not behavior during the outage.
- No test of a genuine network partition between nodes (distinct from
  the flannel VXLAN bug already found and fixed this session, which was
  a standing misconfiguration, not a fault-injection test). Deliberately
  not re-broken here given how much effort went into fixing it.
- The Ollama timeout/retry gap found in scenario 4 is documented but not
  yet fixed - a legitimate next step, not deferred by oversight.
