# Kubernetes / K3s

Roadmap step 10. Multi-node cluster: Pi as control plane, desktop as a worker
node via WSL2. Bigger commitment than the single-node-on-Pi alternative, but
gets closer to the full namespace layout (ai/backend/data/monitoring all in
one cluster) from the original plan.

## Log

- 2026-09-03: **K3s control plane on the Pi.** First install attempt failed
  outright - Raspberry Pi OS doesn't enable the memory cgroup controller by
  default, which every container runtime needs. Fixed by appending
  `cgroup_memory=1 cgroup_enable=memory` to `/boot/firmware/cmdline.txt`
  (backed up first) and rebooting; confirmed the memory cgroup controller
  was live afterward, not just assumed. Second install succeeded - node
  `joe` came up `Ready` as `control-plane` immediately.

  Set up passwordless `kubectl` for the `joe` user by copying
  `/etc/rancher/k3s/k3s.yaml` to `~/.kube/config`. Note: `KUBECONFIG` needs
  to go in `~/.zshenv`, not just `~/.zshrc` - `.zshrc` only loads for
  interactive shells, and SSH-executed one-off commands (`ssh host "cmd"`,
  which is how this session runs everything) are non-interactive. Discovered
  this by testing a fresh `ssh ... "kubectl get nodes"` and watching it fail
  even though the interactive session moments earlier had it working.

- 2026-09-03: **Desktop as a worker node via WSL2 - the hard part.** WSL2
  defaults to NAT networking, where the VM's IP is only reachable from the
  Windows host itself - the Pi's control plane can't reach back into a NAT'd
  node for scheduling, logs, or exec. Fixed by enabling WSL2's **mirrored**
  networking mode (`networkingMode=mirrored` in `.wslconfig`), which makes
  the WSL2 VM share the host's real LAN IP directly - confirmed by checking
  the new distro's own `ip addr` and seeing `192.168.1.131` (the desktop's
  actual IP), not a NAT range.

  **This caused three separate Docker Desktop outages** while getting there,
  since mirrored mode changes networking for every WSL2 distro on the
  machine, including Docker Desktop's own:
  1. A `wsl --shutdown` mid-restart left stale Docker Desktop processes
     holding old port bindings, so the fresh instance failed to rebind
     `5432`/`8000` ("address already in use"). Fixed by force-killing every
     Docker-related process and doing a clean `down`/`up` instead of relying
     on `restart: unless-stopped` to recover stale state on its own.
  2. A stuck WSL2 reparse-point socket file
     (`sailor-ingest.sock`) blocked Docker Desktop's ingest server from
     starting, twice - not a process holding a lock, but a Windows-side
     filesystem object even `fsutil` (admin-only) initially couldn't clear
     cleanly. Needed the user to force-remove it from an elevated
     PowerShell.
  3. General lesson: **interrupting a Docker Desktop restart mid-boot
     (e.g. another `wsl --shutdown`) compounds the corruption** rather than
     fixing it. The recovery that actually stuck was: kill everything,
     wait several seconds for full tear-down, restart once, then wait it
     out fully without touching anything.

  This instability was significant enough to explicitly flag and confirm
  with the user before continuing, rather than assuming the chosen
  architecture (multi-node via WSL2 mirrored networking) was worth the risk
  - they chose to continue.

  Installed Ubuntu 24.04 as the WSL2 distro itself hit a snag too: `wsl
  --install -d Ubuntu` hung indefinitely with near-zero CPU usage - it
  depends on the Microsoft Store app, which doesn't complete its interactive
  flow in a non-interactive/automated context. Killed the stuck process and
  installed via `winget install Canonical.Ubuntu.2404` instead (bypasses the
  Store), then `ubuntu2404.exe install --root` for non-interactive first-run
  setup (root-only, no interactive user/password prompt - fine for a
  dedicated infra node, not a general dev environment).

  Once the agent was installed (`K3S_URL`/`K3S_TOKEN` env vars pointing at
  the Pi), it still couldn't join - `journalctl -u k3s-agent` showed
  consistent timeouts reaching `192.168.1.253:6443`, not a certificate or
  auth error. Root cause: the Pi's `ufw` never had rules for K3s's own
  cluster networking ports - only SSH/DNS/Grafana had been opened
  previously. Added `6443/tcp` (API server), `8472/udp` (Flannel VXLAN,
  pod-to-pod networking), and `10250/tcp` (kubelet), all scoped to
  `192.168.1.0/24` like every other rule on this Pi. Agent joined within
  seconds of the firewall fix - confirming the firewall, not WSL2 or K3s
  itself, was the actual blocker.

  **Verified the full cluster, not just that install commands succeeded**:
  `kubectl get nodes` from the Pi shows both `joe` (control-plane,
  `192.168.1.253`, Debian 13/arm64) and `desktop-j1grrmu` (worker,
  `192.168.1.131`, Ubuntu 24.04/amd64 via WSL2) as `Ready`. Confirmed
  Docker Desktop's existing stack (FastAPI/Postgres/Redis/worker, DNS,
  metrics) still fully functional after all of the above.

## Next

Namespaces (`ai`, `backend`, `data`, `monitoring`) and migrating the
existing Docker Compose workloads onto the cluster: Deployments, Services,
ConfigMaps, Secrets, PersistentVolumes, RBAC, health probes, resource
limits.
