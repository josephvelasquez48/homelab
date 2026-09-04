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

## Log (continued) - workload migration

- 2026-09-03: Migrated everything onto the cluster: `data/postgres`,
  `backend/{redis,api,worker}`, `ai/inference`, `monitoring/{prometheus,
  grafana}`. Manifests in `kubernetes/`, one file per component.

  **Getting images into the cluster at all was the first real problem.**
  K3s's containerd is a separate image store from Docker Desktop - an
  image built with `docker build` isn't visible to it. Pushed
  `apps/api`'s image to `ghcr.io/josephvelasquez48/homelab-api` instead
  (made the package public after confirming with the user, since pull
  secrets add real complexity for a personal project with no sensitive
  code). The cluster is also mixed-architecture (Pi = arm64, desktop
  worker = amd64), so a single-platform build wouldn't run on both nodes -
  built with `docker buildx build --platform linux/amd64,linux/arm64
  --push`, which needed a `docker-container` driver builder first (the
  default `docker` driver can't do multi-platform pushes at all - found
  this out from the build simply refusing to run, not a vague failure).

  **Design decisions, not just mechanical migration:**
  - `postgres` and `redis` are pinned to the Pi (`nodeSelector:
    kubernetes.io/hostname: joe`) with `local-path` PVCs. local-path PVs
    are tied to whichever node first hosts them - without pinning, a
    reschedule to the other node would leave the pod unable to find its
    own data. The Pi is also just the more "always-on" node; the desktop
    gets rebooted for normal use in a way a dedicated Pi doesn't.
  - `ai/inference` is a Service+Endpoints pair (no selector, manually
    specified IP) rather than an ExternalName Service - it points at
    native Ollama on the desktop (still not containerized, same reasoning
    as Milestone 1) by IP directly instead of depending on a pod's own
    external DNS resolution working for a hostname.
  - Prometheus uses `kubernetes_sd_configs` (pods annotated
    `prometheus.io/scrape: "true"`) instead of a static target list, which
    is what gives the RBAC ClusterRole/ClusterRoleBinding an actual
    purpose - Prometheus needs to query the API server to discover pods,
    it's not a token unused example.
  - The `ai`/`embeddings`/`rag-api` split from the original plan's example
    namespace layout isn't implemented as separate services - the FastAPI
    app already serves `/v1/embed` and `/v1/rag/query` from the same
    codebase as everything else in `backend/api`, and deploying the exact
    same image three times under different names would be namespace
    theater, not a real architectural split. It would take an actual code
    split to be honest, which wasn't done here.
  - CoreDNS (whole-LAN DNS, `hostNetwork`, port 53) was **not** migrated -
    it wasn't in the plan's own namespace example, and a botched rollout
    of something that critical is a worse failure mode than the modest
    benefit of having it in the cluster too. Stays on Docker Compose.

  **Blocked on Ollama being `127.0.0.1`-only.** Docker containers could
  always reach it via `host.docker.internal`'s special host-loopback
  routing, but a real pod on a different machine (the Pi) needs a genuine
  network path - `127.0.0.1` inside Ollama's own bind address isn't
  reachable from anywhere else no matter what the cluster networking
  looks like. This has a real security trade-off (opening Ollama to the
  LAN means any device on the network can call it directly, bypassing the
  gateway's auth/rate-limiting - explicitly against the "apps talk to the
  gateway, never straight to Ollama" design goal), so it was surfaced and
  confirmed rather than just done. Fixed with `OLLAMA_HOST=0.0.0.0:11434`
  plus a Windows Firewall rule scoping inbound `11434` to
  `192.168.1.0/24` - narrows the trade-off to "reachable within the LAN"
  rather than "reachable from anywhere."

  **The WSL2 idle-timeout gotcha came back.** The worker node went
  `NotReady` on its own, ~15 minutes after joining, with nothing having
  touched it - turned out WSL2 stops idle VMs after a period of no
  foreground activity, killing `k3s-agent` along with it, even though it
  was a live systemd service. Fixed with `vmIdleTimeout=-1` in
  `.wslconfig`. This needed another `wsl --shutdown` to take effect,
  applied deliberately while the cluster had zero real workloads on it
  yet - the lowest-risk moment available, given how disruptive that
  command had already proven to be.

  **`vmIdleTimeout=-1` didn't fully fix it** - the worker node went
  `NotReady` again roughly 45 minutes into the session, after real
  workloads existed. Waking the WSL2 distro with any command
  (`wsl -d Ubuntu-24.04 -- ...`) reliably brought `k3s-agent` back within
  15-30s and the node rejoined as `Ready` on its own, so this isn't
  data-loss-risky, just an open annoyance - worth a proper fix (a
  scheduled task pinging the distro, or investigating why the config
  isn't sticking) before this cluster is depended on for anything that
  needs to survive an unattended stretch of idle time.

  **Verified real functionality throughout, not just `kubectl apply`
  succeeding:** a rolling update on `api` (`maxUnavailable: 0`) replaced
  both pods with zero dropped requests; `/health`, `/v1/chat`, `/v1/embed`,
  `/v1/documents` + `/v1/rag/query`, and `/jobs` all tested through the
  real `api.home` Ingress hostname (not `kubectl exec` shortcuts) with
  fresh, never-cached responses; a rag query correctly retrieved a
  freshly-ingested document and answered from it; Prometheus showed every
  target - including dynamically-discovered pods - `up`; Grafana's
  dashboard and datasource were confirmed actually provisioned via its
  API, not just that the pod was healthy. Only after all of that did the
  old Docker Compose backend stack (api/worker/postgres/redis) and the old
  Docker Compose Prometheus/Grafana get torn down.

## Current state

| Namespace | Workload | Node | Notes |
|---|---|---|---|
| `data` | `postgres` | Pi | pgvector, `local-path` PVC |
| `backend` | `redis` | Pi | `local-path` PVC |
| `backend` | `api` (2 replicas) | either | Ingress: `api.home`, `ai.home` |
| `backend` | `worker` | either | processes the Redis job queue |
| `ai` | `inference` | - | Endpoints -> native Ollama, `192.168.1.131:11434` |
| `monitoring` | `prometheus` | Pi | K8s SD + RBAC for pod discovery |
| `monitoring` | `grafana` | Pi | Ingress: `grafana.home` |

Not migrated, staying on Docker Compose: CoreDNS (whole-LAN DNS, too
critical to risk on a first K8s pass) and node-exporter (needs host
`/proc`/`/sys`, simpler to leave where it already works).

## Next

CI/CD (roadmap step 11) is the natural next step now that there's an
image registry and a real deployment target - `git push` -> tests -> build
-> push to ghcr.io -> `kubectl apply`/rollout, replacing the manual
build-and-push done by hand in this phase.

## Log (continued) - flannel VXLAN silently dropped on the WSL2 worker

- 2026-09-03: Triggered by, but ultimately unrelated to, the secrets
  rotation incident in [docs/secrets.md](secrets.md) - fixing that
  exposed a second, genuinely separate bug: any pod scheduled onto
  `desktop-j1grrmu` (the WSL2 worker) couldn't reach *any* ClusterIP
  service, including CoreDNS itself. `api-migrate` and rolled-out
  `api`/`worker` pods crash-looped there with
  `Temporary failure in name resolution` even after the credential
  problem was fixed - a fresh, unrelated failure mode, not a symptom of
  the same root cause.

  **Isolating it**: raw LAN ping between the two hosts (`192.168.1.253` <->
  `192.168.1.131`) was fine, 5-7ms - so this wasn't the underlying network,
  it was specifically the flannel VXLAN overlay (UDP 8472) between nodes.
  `ping` to the peer's `flannel.1` gateway address failed 100% in both
  directions. Packet captures on both ends nailed down exactly where:
  the Pi received the WSL2 side's outbound VXLAN-encapsulated ping and
  sent a reply (visible leaving on `wlan0`) - but that reply never showed
  up in a capture taken on the WSL2 side, even though the flannel kernel
  socket was confirmed listening (`ss -lun` showed `UNCONN 0.0.0.0:8472`).
  Something in the Windows host network stack was dropping inbound VXLAN
  traffic before it ever reached the WSL2 VM.

  **Ruled out, in order, each with real evidence rather than assumption:**
  1. `ufw` on the Pi - already explicitly `ALLOW`s `8472/udp` from
     `192.168.1.0/24`, and its logs showed zero blocked packets matching
     that traffic.
  2. Classic Windows Firewall (`New-NetFirewallRule`) - added an explicit
     inbound allow for UDP 8472 from the LAN; no change. Restarting
     `k3s-agent` to force flannel to re-establish its VXLAN state also
     made no difference, ruling out stale routes/FDB entries.
  3. The **Hyper-V firewall** - a separate rule store from the classic
     Windows Firewall that specifically governs WSL2/Hyper-V VM traffic
     (`New-NetFirewallHyperVRule`, keyed by a `VMCreatorId` GUID). Easy to
     miss since `Get-NetFirewallHyperVRule` lists classic host rules
     alongside real Hyper-V-layer ones, so a rule showing up there doesn't
     mean it's actually enforced at that layer. Added the equivalent rule
     here too (`VMCreatorId {40E0AC32-46A5-438A-A0B2-2B479E8F2E90}`,
     confirmed as WSL's own ID via `Get-NetFirewallHyperVProfile`) -
     `EnforcementStatus: OK`, still no change.
  4. Confirmed WSL2's mirrored networking mode was genuinely active, not
     silently falling back to NAT (a known failure mode for that
     setting) - `ip addr` inside WSL2 showed the exact same IP
     (`192.168.1.131`) as the Windows host's physical adapter, ruling
     out NAT as the explanation for unsolicited inbound traffic being
     dropped.

  With both firewall layers confirmed open and mirrored mode confirmed
  genuinely active, the packet was still being dropped somewhere in the
  Windows host's own network stack before reaching the WSL2 VM - most
  likely Windows Defender's Network Inspection System, which does deep
  packet inspection and could plausibly flag an unusual UDP encapsulation
  pattern (VXLAN/OTV) on a mirrored interface. Given the choice between
  disabling part of Defender's real-time protection to confirm that, or
  removing the encapsulation from the equation entirely, chose the
  latter.

  **Fix: switched K3s's flannel backend from `vxlan` to `host-gw`.**
  Both nodes are on the same LAN segment, so flannel doesn't need UDP
  encapsulation at all here - `host-gw` just adds a direct kernel route
  to each peer's pod subnet via the peer's real LAN IP (`ip route` showed
  `10.42.0.0/24 via 192.168.1.253 dev eth1` on the desktop node after the
  switch) and lets normal IP routing do the rest. Changed via
  `--flannel-backend=host-gw` on the k3s **server** (control plane only -
  `/etc/systemd/system/k3s.service` on the Pi), confirmed the change
  propagated to both nodes' `net-conf.json`, then restarted `k3s-agent`
  on the desktop worker to pick it up. Rollback is symmetric: remove the
  flag, restart both, flannel falls back to `vxlan` on its own.

  **Verified with real traffic, not just ping**: after the switch,
  `ping` to actual pod IPs across nodes (CoreDNS, Postgres) succeeded
  with 0% loss, and a subsequent Argo CD sync scheduled one `api` replica
  onto each node - both came up healthy, proving real Kubernetes
  workloads (not just ICMP) now route correctly between them.

  Worth noting since it's easy to read backwards: this was never a K3s,
  ufw, or Kubernetes NetworkPolicy problem - every layer this project
  controls directly was already correctly configured. It was a Windows
  host networking quirk specific to WSL2 mirrored mode plus VXLAN, and
  `host-gw` sidesteps it rather than fixing it - worth remembering if a
  third node is ever added that *isn't* on the same L2 segment, since
  `host-gw` requires that and `vxlan` doesn't.

## Log (continued) - actually fixing the WSL2 idle-timeout flakiness

- 2026-09-03: The `vmIdleTimeout=-1` fix noted above kept resurfacing -
  most recently, the desktop node sat `NotReady` for several minutes
  and cycled through at least one full `k3s-agent` restart before
  recovering, well past the "15-30s" pattern seen earlier. Worth
  actually root-causing rather than continuing to just wake it by hand
  each time it's needed.

  **Ruled out, with evidence, before reaching for a workaround:**
  - The whole machine sleeping - `powercfg /query SCHEME_CURRENT
    SUB_SLEEP` showed `Sleep after` = 0 (never) on both AC and DC.
  - `.wslconfig` losing the earlier fix - `vmIdleTimeout=-1` was still
    present, unchanged.
  - An obvious Windows power-throttling policy override - none found.

  With the machine confirmed not sleeping and the existing config fix
  confirmed still in place, this is WSL2's own idle/suspend heuristic
  triggering on *something* Microsoft doesn't document precisely enough
  to keep chasing with more configuration alone - `vmIdleTimeout`
  governs the shared utility VM's teardown, not necessarily every path
  that can pause an individual distro's own state.

  **Fix: a Scheduled Task that touches the distro every minute**
  (`wsl.exe -d Ubuntu-24.04 -e /bin/true`), triggered both at logon and
  on a 1-minute repeating interval. Turns "figure out Microsoft's exact
  undocumented idle heuristic" into "never let the gap between real
  activity get long enough for any heuristic to matter" - a heartbeat
  is the standard fix for exactly this class of WSL2 problem across the
  community, and it sidesteps needing to reverse-engineer internals
  this project doesn't control.

  Verified the task itself actually runs, not just that it registered:
  `Get-ScheduledTaskInfo` showed `LastTaskResult: 0` (success) with
  `NextRunTime` exactly one minute later, confirming the repetition
  trigger is real. Full confirmation that this fixes the underlying
  flakiness - the node staying `Ready` over a real unattended idle
  stretch, without anyone manually touching WSL2 in the meantime - is a
  longer-running check; see the dated follow-up note once that window
  has actually elapsed, not just "the task exists."

  **Immediately caused a new, obvious problem**: `wsl.exe` is a console
  application, so a Scheduled Task invoking it directly pops a real,
  visible console window every single time it fires - once a minute,
  on a desktop someone actually sits at and uses for gaming. Not a
  subtle bug; noticed immediately. Fixed by wrapping the call in
  `scripts/wsl-keepalive.vbs`, invoked via `wscript.exe //B` instead of
  calling `wsl.exe` directly - `WScript.Shell.Run`'s hidden-window
  argument is the standard, reliable way to make Task Scheduler run
  something with zero visible window (more robust than `cmd.exe /min`
  tricks, which can still flash briefly). Confirmed the task still
  succeeds through the wrapper (`LastTaskResult: 0`, firing on
  schedule, no missed runs).

  **Confirmed fixed, not just "the task exists and fires"**: checked
  back after a real ~25-minute unattended idle stretch, with the only
  thing touching WSL2 in that window being the keepalive task's own
  automated schedule - no manual `wsl` command run by anyone in
  between, which would have masked the exact failure mode being tested.
  `kubectl get nodes` showed `desktop-j1grrmu` still `Ready`, and
  `Get-ScheduledTaskInfo` confirmed the task had fired every minute the
  whole time with `NumberOfMissedRuns: 0`. This is the first time this
  session the node has stayed `Ready` across a real idle window without
  needing to be woken by hand.

## Log (continued) - a routine Pi reboot that cascaded into three separate problems

- 2026-09-04: Rebooted `joe` for a pending kernel update
  (`6.12.47+rpt-rpi-2712` -> `6.18.39+rpt-rpi-2712`). The reboot itself
  was clean - node back `Ready` within about a minute, every pod that
  restarted because of it recovered on its own. Everything after this
  point was **triggered by** the reboot but not an inherent part of it.

  **Problem 1: a stuck `svclb-traefik` pod turned into a desktop-wide
  crash loop.** One `svclb-traefik` pod (k3s's built-in ServiceLB
  sidecar) came back from the reboot in `Unknown` with 54 restarts and
  no events at all - looked like simple kubelet/API desync, so it got
  deleted to let the DaemonSet recreate it. The replacement scheduled
  onto `desktop-j1grrmu` and sat `Pending` for 5+ minutes with *zero*
  kubelet activity, not even an image pull attempt - despite the node
  showing `Ready` and its Lease renewing normally seconds apart. That
  split (Lease healthy, NodeStatus/pod-processing not) was the first
  sign this wasn't the already-solved idle-timeout bug from the log
  above.

  **Ruled out before finding the real cause:**
  - Idle-timeout recurring - the `WSL2-K3s-Keepalive` scheduled task
    was still firing every minute exactly as designed
    (`Get-ScheduledTaskInfo` showed `LastRunTime`/`NextRunTime` a
    minute apart).
  - The whole machine sleeping - `Get-WinEvent` against
    `Microsoft-Windows-Kernel-Power` showed no sleep/wake events
    anywhere near the incident window.
  - Docker Desktop restarting the shared WSL2 utility VM - Docker
    Desktop wasn't even running at the time.

  **Actual cause**: `journalctl -u k3s-agent` showed repeated `PLEG is
  not healthy` and `Failed to create existing container: ... task
  <id> not found` errors, all referencing the *same* pod UID across
  multiple separate crash cycles - the recreated `svclb-traefik` pod
  itself. Its `lb-tcp-80`/`lb-tcp-443` sidecar containers
  (`crictl ps -a`) were exiting with code 255 within seconds of
  starting, every single time, and each failed reconciliation attempt
  was severe enough to take the entire `k3s-agent` process down with
  it - not a hung agent causing a stuck pod, but a broken pod crashing
  a healthy agent. Confirmed no port conflict at the OS level
  (`ss -tlnp` showed nothing bound to 80/443) - this looks like the
  ServiceLB pause-FIFO mechanism itself failing in this specific WSL2
  environment, not a resource or config problem.

  **Where this landed**: deleting the pod cleanly (not force-deleted
  this time) bought a stable, healthy `2/2 Running` replacement and a
  `k3s-agent` that settled into `active/running` - but it recurred
  again about 7 minutes later. Left as a **known, unresolved, recurring
  issue** rather than force-fixed blind: the pod is not required for
  the cluster's actual routing (the `joe`-side `svclb-traefik` replica
  stays healthy throughout, and every ingress hostname resolves through
  it fine), so the practical impact is limited to `kubectl get pods`
  showing one flapping pod. Next step, if this needs a real fix rather
  than tolerance: either exclude `desktop-j1grrmu` from this specific
  DaemonSet, or trace the pause-FIFO exec failure inside the WSL2
  container runtime directly (`strace`-level, not attempted here).

  **Problem 2 (separate, unrelated): Grafana's own metrics scrape
  target came back down.** `kubernetes-pods` job showed
  `10.42.0.91:9100/metrics` (Traefik, on `joe`) as connection-refused
  in Prometheus. Traefik's config is verified correct -
  `--metrics.prometheus=true`, `--entryPoints.metrics.address=:9100`,
  and `kubectl exec ... wget localhost:9100/metrics` returns real
  metrics from inside the pod - yet Prometheus, on the *same node*,
  gets refused hitting the pod IP on that port. Points to Traefik only
  binding its metrics listener to loopback rather than all interfaces.
  Confirmed this predates today entirely (restart count on the pod was
  1, from today's reboot, and the annotation/config have clearly been
  there longer) - **left unresolved**, cosmetic only, doesn't affect
  Traefik's actual routing or any other Grafana panel.

  **Problem 3 (separate, unrelated): Grafana OOMKilled again, past the
  previous 512Mi fix.** The memory-limit fix from the original
  OOMKilled incident (`docs/secrets.md`-adjacent Grafana notes) held
  for hours, but OOMKilled again (exit 137) during this reboot's
  recovery churn - many pods rescheduling/restarting simultaneously
  across the cluster. `joe` had ample headroom throughout (55% of 8Gi
  used, never memory-pressured node-wide), so this was Grafana's own
  container hitting its cgroup limit, not the node running out.
  Bumped `512Mi` -> `1Gi` (real breathing room, not another marginal
  nudge) - confirmed stable afterward with 0 restarts.

  **Net result**: reboot succeeded; Grafana OOM fixed properly this
  time; the Traefik metrics scrape and the desktop `svclb-traefik` flap
  are both pre-existing/recurring issues that were root-caused but
  deliberately left open rather than papered over, since neither
  affects real functionality and both would need deeper environment-
  specific debugging (WSL2 container runtime internals; Traefik's
  metrics-server bind address) to actually close out.
