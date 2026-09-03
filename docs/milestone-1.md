# Milestone 1

**Status: complete.** Further backend work continues in
[docs/backend.md](backend.md).

Target:

```
Raspberry Pi 5              Desktop
├── Linux                   ├── Docker
├── Docker                  ├── FastAPI
└── Private DNS             ├── PostgreSQL
                             ├── Redis
                             └── Ollama + RTX 3070 Ti
```

Reachable internally as `api.joseph`, `ai.joseph`, `grafana.joseph`.

## Environment assessment (2026-09-02)

**Desktop (Windows 11 Pro)**

- Docker Desktop 28.3.3 installed, WSL2 backend (v2.5.10.0) — engine was not
  running at assessment time, started manually.
- GPU: RTX 3070 Ti, driver 616.56, 8GB VRAM.
- Ollama 0.32.5 installed natively (not containerized) — already has
  `qwen2.5-coder:7b` pulled.
- LAN IP: `192.168.1.131/24` (Ethernet).
- SSH client available (OpenSSH 9.5p2).

**Raspberry Pi 5**

- OS already flashed to NVMe from a prior setup. Not currently powered on.
  Connection details (IP/hostname, SSH credentials) to be confirmed once
  powered on.
- 2026-09-02, once powered on: found via LAN ping sweep + SSH banner
  fingerprinting (`192.168.1.253`, `OpenSSH_10.0p2 Debian-7` matched current
  Raspberry Pi OS; an initial candidate at `.57` had a decade-old OpenSSH
  banner and turned out to be an unrelated device). Debian 13 (trixie),
  kernel `6.12.47+rpt-rpi-2712`, arm64, 8GB RAM, 4 cores, booting from the
  469GB NVMe as intended (SD card present but unused, just mounted as
  removable media). Running the **Desktop** image (GUI stack: `wayvnc`,
  `cupsd`) rather than Lite — left as-is for now, revisit if it needs
  trimming down later.

## Decisions

- **Ollama runs natively on Windows, not in a container.** Docker Desktop GPU
  passthrough to WSL2 works but adds a layer of complexity (NVIDIA container
  toolkit inside the WSL2 VM) for no real benefit here — the desktop is a
  single-purpose GPU host, not a fleet of inference containers. FastAPI still
  fronts it as a gateway, so the rest of the platform is agnostic to how
  inference is hosted. This can be revisited when moving to K3s if pod-level
  GPU scheduling becomes valuable to demonstrate.
- **New standalone repo** (`homelab`), separate from the personal portfolio
  site repo, so history and CI stay scoped to this project.
- **Pi deploys via a read-only GitHub deploy key + `git pull`**, not scp.
  Config lives in git as the source of truth from day one, which is also
  the natural on-ramp to Argo CD later (same principle, more automation).
- **CoreDNS over Pi-hole** for internal DNS — same ad-blocking result via
  the `hosts` plugin loaded with a StevenBlack hosts-format blocklist, but
  config-as-code (Corefile) instead of a web UI, and it's literally the DNS
  server Kubernetes uses internally, which is more directly relevant to the
  target roles than a consumer ad-blocker.

## Log

- 2026-09-02: Repo scaffolded, environment assessed. Docker Desktop started.
  Pi setup deferred until it's powered on.
- 2026-09-02: GitHub CLI installed, repo pushed to
  [josephvelasquez48/homelab](https://github.com/josephvelasquez48/homelab)
  (private). Docker Desktop engine verified working (`docker run hello-world`)
  — 28 CPUs / ~16GB RAM allocated to the WSL2 VM.
- 2026-09-02: Discovered a pre-existing local `docker system prune`-style
  cleanup (not run by this session) had removed all images/containers from an
  unrelated prior project (`ChatBot`, on the Desktop, separate repo). Verified
  it was fully rebuildable from its own git repo/Dockerfile before moving on
  — no data lost, since only its Ollama model volume mattered and that
  survived.
- 2026-09-02: Minimal FastAPI service (`apps/api`, dependency-managed with
  `uv`) running in Docker via `docker/docker-compose.yml`, `GET /health`
  verified returning 200. Pi setup deferred by request — desktop track
  continues first; Postgres + Redis are next.
- 2026-09-02: Added PostgreSQL (`pgvector/pgvector:pg17` — pgvector chosen
  now instead of plain `postgres` to avoid a data migration when RAG work
  starts, extension not enabled by default) and Redis 7 to compose, both
  with healthchecks. `pgvector` extension creation verified manually.
  `/health` now does real connectivity checks (`SELECT 1` via asyncpg,
  `PING` via redis-py) instead of a static response — this becomes the
  container's readiness signal for Kubernetes later. Credentials live in
  `docker/.env` (gitignored); `docker/.env.example` documents the shape.
- 2026-09-02: Confirmed Ollama (native Windows process) uses the GPU:
  `qwen2.5-coder:7b`, 100% GPU per `ollama ps`, ~5.7GB/8GB VRAM, 94% GPU
  utilization, **~105 tokens/sec** on a warm model (cold load ~0.1-12s
  depending on idle timeout). Added `POST /v1/chat` to the FastAPI service,
  proxying to Ollama over `http://host.docker.internal:11434` (the
  container-to-host bridge Docker Desktop provides automatically on
  Windows/Mac) — verified end-to-end through the container at ~101 tok/s,
  confirming apps talk to the gateway, never straight to Ollama, per the
  design goal.

- 2026-09-02: Pi provisioned: Docker CE (official repo) installed, `joe`
  added to the `docker` group; `ufw` installed with default-deny incoming,
  SSH and DNS (port 53) both scoped to `192.168.1.0/24` only, no external
  exposure. SSH key auth set up from the desktop (ed25519, no passphrase —
  it's a LAN-only automation key). Deploy key (read-only) added to the
  `homelab` GitHub repo so the Pi can `git pull` its own config.
- 2026-09-02: Deployed CoreDNS on the Pi (`docker/dns`, `network_mode: host`
  for port 53). Two zones: `joseph:53` serves `api.joseph`/`ai.joseph` (both
  → the desktop, same FastAPI process for now, no reverse proxy yet — URLs
  still need `:8000`); `.:53` blocks ads via a StevenBlack blocklist
  (85,427 entries) before forwarding everything else to `1.1.1.1`/`8.8.8.8`.
  A weekly systemd timer refreshes the blocklist automatically.
  **Debugging note:** ad-blocking silently didn't work on first deploy —
  real answers came back for domains confirmed present in the blocklist.
  Root cause: `mktemp` creates files mode `600`; the update script's `mv`
  preserved that, and the CoreDNS image runs as a non-root distroless user
  (`65532`) with no permission to read the file, so it silently loaded zero
  entries and every query fell through to `forward`. The `.joseph` zone's
  hosts file happened to be group/other-readable already, which is exactly
  why *that* one worked and masked the bug for a while. Fixed by adding
  `chmod 644` to the script; verified with a domain confirmed present in
  the blocklist (`4436230.fls.doubleclick.net` → `0.0.0.0`) plus a real
  domain still resolving normally, to rule out both directions of failure.
  DNS is deployed and verified but nothing on the network is configured to
  actually *use* it yet (router DHCP / per-device DNS) — that's a separate,
  more impactful step to confirm before making it live network-wide.
- 2026-09-02: Made the desktop actually use the Pi for DNS. **The router
  (Spectrum SAX1V1S) doesn't support DHCP DNS override** — its app-level
  "Manage DNS" setting only affects the router's own upstream queries, not
  what it hands LAN clients. Fell back to setting DNS per-device instead of
  network-wide. Three layered issues along the way, each confirmed by
  testing rather than assumed fixed:
  1. **Windows races configured DNS servers** rather than strictly
     preferring primary over secondary — pointing at the Pi + a public
     fallback (`1.1.1.1`) meant the fast public resolver almost always won,
     silently defeating the whole point. Fixed by using the Pi as the sole
     configured server (it already does its own upstream forwarding, so a
     second OS-level fallback wasn't actually adding safety, just breaking
     things).
  2. Windows prefers IPv6 DNS servers over IPv4 when both are configured.
     The Ethernet adapter had an **IPv6 DNS server learned dynamically from
     the router's IPv6 Router Advertisements**, which we'd never touched —
     so every "default resolver" query silently went there regardless of
     the IPv4 override, even though explicit `-Server 192.168.1.253`
     queries always worked (which is exactly what made this confusing:
     partial evidence pointed different directions until isolating explicit
     vs. default resolution paths cleanly).
  3. `netsh interface ipv6 set dnsservers ... static none` didn't stick —
     it only clears manually-set entries, not ones learned dynamically via
     RA, so the IPv6 DNS server kept reappearing. Resolved by giving the Pi
     a **static IPv6 address** instead, so it could be pointed at directly:
     used the router's self-generated ULA prefix (`fd00:.../64`, stable
     regardless of ISP prefix changes) rather than the public GUA
     (Spectrum-delegated `2600:.../64`, dynamic and could rotate on a
     modem reconnect) — `fd00:f405:95c7:c412::253`. Needed its own `ufw`
     IPv6 rule too, since ufw tracks IPv4/IPv6 rules separately.

  Verified clean end-to-end afterward: `api.joseph`/`ai.joseph` resolve via
  the plain default resolver (no explicit `-Server`), a known-blocked
  domain returns `0.0.0.0`, a real domain still resolves normally, and
  `Invoke-RestMethod http://api.joseph:8000/health` returns a live
  `{"status":"ok","postgres":"ok","redis":"ok"}` through the hostname.

## Baseline benchmark

| Metric | Value |
|---|---|
| Model | qwen2.5-coder:7b (Q4, 4.7GB) |
| GPU | RTX 3070 Ti, 8GB VRAM |
| Throughput (warm) | ~105 tokens/sec |
| VRAM used | ~5.7GB |
| GPU utilization | 94% |
| Processor split | 100% GPU (`ollama ps`) |
