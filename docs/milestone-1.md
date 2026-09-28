# Milestone 1: the starting point

**Complete** (2026-09-02). The first working version: a FastAPI gateway,
Postgres, Redis and Ollama on the desktop, and private DNS with ad blocking
on the Pi. Everything since has built on it; most of it now runs in K3s
([kubernetes.md](kubernetes.md)).

```
Raspberry Pi 5              Windows desktop
├── Linux                   ├── Docker: FastAPI, PostgreSQL (pgvector), Redis
├── Docker                  └── Ollama on the RTX 3070 Ti
└── Private DNS (CoreDNS)
```

## Decisions that stuck

- **Ollama runs natively on Windows, not in a container.** GPU
  passthrough into Docker/WSL2 adds a layer for no gain on a single GPU
  host. FastAPI fronts it, so nothing else cares how inference is hosted.
- **The Pi deploys with `git pull`** using a read-only deploy key, not
  `scp`. Git was the source of truth from day one - the on-ramp to Argo CD.
- **CoreDNS rather than Pi-hole:** config-as-code (a Corefile), and it's the
  DNS server Kubernetes itself uses. AdGuard Home was later added *behind*
  it for a query log and filter management - CoreDNS stays the only thing
  clients talk to ([router-migration.md](router-migration.md)).
- **pgvector from the start**, so RAG later needed no data migration.
- **`/health` does real checks** (`SELECT 1`, Redis `PING`), which later
  became the Kubernetes readiness signal.

## Problems found on the way

| Problem | Cause | Fix |
|---|---|---|
| Ad blocking silently did nothing | `mktemp` makes files mode 600, and the CoreDNS image runs as a non-root user, so it loaded zero blocklist entries | `chmod 644`; checked both a blocked and a normal domain |
| Windows ignored the Pi for DNS | Windows races its DNS servers, so a public fallback won almost every time | The Pi as the only DNS server |
| Still ignored, over IPv6 | Windows prefers IPv6 DNS, learned from the router and not cleared by `netsh` | A static IPv6 address on the Pi, pointed at directly |
| No network-wide DNS | The old router couldn't hand out a DNS server | Per-device settings, until the router was replaced ([router-migration.md](router-migration.md)) |
| The Pi was hard to find | A port scan's first candidate was a different device with a decade-old SSH | Found by its current OpenSSH banner (later, by its MAC vendor) |

## Baseline benchmark

| Metric | Value |
|---|---|
| Model | qwen2.5-coder:7b (Q4, 4.7 GB) |
| GPU | RTX 3070 Ti, 8 GB VRAM |
| Throughput (warm) | ~105 tokens/s (~101 through the FastAPI gateway) |
| VRAM used | ~5.7 GB |
| GPU utilization | 94%, 100% on GPU |

## Hardware found

- **Desktop:** Windows 11 Pro, Docker Desktop on WSL2, RTX 3070 Ti,
  192.168.1.131.
- **Pi 5:** Debian 13 on NVMe, arm64, 8 GB RAM, 192.168.1.253 - running the
  Desktop image rather than Lite, left as-is.
