# Milestone 1

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
