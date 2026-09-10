# Second node migration: WSL2 to an M1 MacBook

Planned, not yet executed. This records the decision and the sequence
before any of it is done, because the failure modes are known in advance
and the ordering is what keeps them from biting.

## The decision

Retire `desktop-j1grrmu` - a K3s node running inside WSL2 with mirrored
networking - and replace it with a Linux VM on a spare M1 MacBook. The
Windows desktop stays on the LAN as the GPU host for Ollama, outside the
cluster entirely.

```
LAN
 |
 +-- Pi 5            K3s control-plane  (unchanged)
 +-- M1 MacBook      K3s worker, Linux VM, bridged
 +-- Windows PC      Ollama + RTX 3070 Ti, not a cluster member
```

## Why

Nearly every hard failure in [kubernetes.md](kubernetes.md) traces to one
root cause: the worker is not really a host on the network. Not a
Kubernetes problem, a WSL2 one.

- flannel VXLAN silently dropped by the Windows host stack
- `host-gw` refused by Windows' strong-host model (`Not locally destined`)
- the WireGuard kernel-socket registration quirk, recurring on every
  reboot, currently worked around by a logon-triggered Scheduled Task
- stale flannel routes winning by longest-prefix match after a backend
  switch
- the idle-timeout saga, where the keepalive silently forced a cold boot
  every 60 seconds while appearing to help
- `svclb-traefik` crash-looping and taking `k3s-agent` down with it
- Ollama unreachable from pods on its own node, because a Windows-side
  listener is invisible inside WSL

A bridged VM on macOS is an ordinary L2 host. UTM, multipass and Parallels
all support real bridged networking, unlike WSL2, which offers only NAT
or mirrored. So this removes the class, not an instance.

**It also removes WSL2 from the project entirely.** Ollama was moved into
WSL specifically because pods on the desktop could not reach a
Windows-side listener. With no pods there, that constraint disappears and
Ollama returns to native Windows, which has the better GPU story anyway.

## What this costs

- **Both nodes become arm64.** The `linux/amd64` half of every image build
  would have nowhere to run. Precedent already exists for handling this
  honestly: `ci-adguard-exporter.yml` builds arm64 only, with a comment
  saying a multi-arch build would be unused bytes.
- **No Ethernet port on an M1 MacBook**, so wired needs a USB-C adapter.
  Worth doing - the Pi is already on Wi-Fi, and two wireless nodes is a
  fragility you would feel.
- **A laptop must be kept awake.** `sudo pmset -c disablesleep 1` while on
  power. A sleeping node is `NotReady`, which is the same shape of problem
  being migrated away from.
- **Gaming mode largely dissolves.** Its scripts cordon and drain a node
  that will not exist. The real contention was never CPU - it is Ollama
  holding ~5.7GB of VRAM - so it becomes "stop Ollama", which is simpler
  and more honest, but the dashboard's SSH runner and endpoints need
  rewriting.
- **`node-exporter-desktop` has no node to run on.** Desktop metrics on
  the dashboard would need `windows_exporter` running natively, which
  [monitoring.md](monitoring.md) already lists as a known gap.

## Decisions taken deliberately

**The Pi stays control-plane.** Moving it means migrating the K3s
datastore, which is unrelated risk bolted onto an already substantial
change. The M1 is the more capable machine, but the Pi is a dedicated
always-on appliance and the laptop is not.

**Postgres and Redis stay pinned to the Pi, for now.** Their `local-path`
PVCs bind to a node's disk, so moving them is a data migration, not a
`nodeSelector` edit. Worth revisiting once the new node has proven itself
over weeks rather than in the same change.

**flannel returns to `vxlan`.** The default and best-tested backend.
`wireguard-native` is in use only because `vxlan` and `host-gw` both lost
fights with Windows networking - fights that will no longer exist. This
also retires the socket-registration workaround at its root.

## Prerequisites

- USB-C Ethernet adapter, and the MacBook wired to the LAN
- `sudo pmset -c disablesleep 1`, confirmed to survive a lid close
- A hypervisor with genuine bridged networking. `multipass launch
  --bridged` is the least ceremony; UTM with a bridged adapter gives more
  control over an always-on VM. Either must auto-start the VM at boot.
- A DHCP reservation for the VM's MAC, so its address is stable the way
  the Pi's and the desktop's are

## Sequence

Each step verifies before the next. The whole point of the ordering is
that the cluster stays serving throughout - the desktop node is carrying
almost nothing today, so there is no urgency to remove it first.

1. **Provision the VM.** Ubuntu 24.04 arm64, bridged, static or reserved
   address, wired. Confirm from the Pi that it answers on its own LAN IP
   before K3s is involved at all.
2. **Join it as a second worker**, leaving the WSL2 node in place. A
   three-node cluster is a valid intermediate state and makes the cutover
   reversible.
3. **Verify cross-node networking properly**, not by reading Prometheus.
   All targets can read `up` while nothing exercises the pod network -
   `node-exporter-desktop` and `svclb-traefik` are both `hostNetwork` and
   stay reachable over the node's real LAN address whether the tunnel
   works or not. The test that proves it is a pod on the new node reaching
   a Service backed by pods on the Pi:

   ```bash
   kubectl run xnode --rm -i --restart=Never --image=busybox:1.36 --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"<new-node>"}}}' -- wget -qO- http://api.backend.svc.cluster.local:8000/health
   ```

4. **Cordon and drain the WSL2 node**, and confirm workloads reschedule
   onto the new one and stay healthy.
5. **Delete the WSL2 node** from the cluster, and stop `k3s-agent` inside
   WSL.
6. **Switch flannel to `vxlan`** on the server, restart both nodes'
   agents, and - critically - `ip route del` the old `wireguard-native`
   routes on every node. A live backend switch leaves the previous
   backend's routes in place, and they win by longest-prefix match with
   nothing logged to say so. Check with `ip route | grep 10.42`, do not
   assume.
7. **Move Ollama back to native Windows**, re-point the `inference`
   Endpoints, and confirm `/v1/chat` and the RAG routes from a pod on each
   node.
8. **Retire the two Scheduled Tasks** - `WSL2-K3s-Keepalive` and
   `WSL2-K3s-WireGuard-Register` - and the scripts behind them.

## Repo changes this implies

- `.github/workflows/ci.yml` and `ci-dashboard.yml` - drop `linux/amd64`
  unless a decision is made to keep building it for a future x86 node
- `kubernetes/monitoring/node-exporter-desktop.yaml` - delete, or replace
  with a `windows_exporter` scrape target
- `apps/dashboard` - the gaming-mode endpoints, `ssh_runner.py`, and the
  desktop-metrics queries in `prometheus.py`
- `gaming-mode/*.ps1` - rewrite around stopping Ollama rather than
  draining a node
- `kubernetes/ai/inference.yaml` and the sync CronJob - still needed if
  the desktop keeps a DHCP address; unnecessary if it gets a reservation
- `argocd-repo-server`'s `nodeSelector` pin - it exists only because the
  desktop node was flaky, and can go
- `docs/gaming-mode.md`, `docs/dashboard.md`, `docs/kubernetes.md` - the
  WSL2 material becomes history rather than current architecture, and
  should be framed that way rather than deleted

## Rollback

Every step before 5 is reversible by uncordoning the WSL2 node - it stays
a cluster member until then, and the intermediate three-node state is
valid. After step 5, rolling back means rejoining it, which is the
original install path and is documented.

Steps 6 through 8 are independent of the node change and can be deferred
or reverted on their own.

## Open question

Whether to keep building `linux/amd64`. Dropping it is consistent with
`ci-adguard-exporter.yml` and makes CI faster and more honest. Keeping it
means images already exist if an x86 node is ever added, at the cost of
QEMU emulation time for a target nothing runs. This should be a recorded
decision either way, not a default.
