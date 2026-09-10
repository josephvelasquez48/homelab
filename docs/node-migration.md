# Second node migration: WSL2 to an M1 MacBook

This records the decision and the sequence before any of it is done,
because the failure modes are known in advance and the ordering is what
keeps them from biting. Execution began on 2026-09-10 and is logged at
the end; step 1 is not yet complete, so everything below still reads as
a plan.

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
   kubectl run xnode --rm -i --restart=Never --image=busybox:1.36 --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"<new-node>"}}}' --command -- wget -qO- http://api.backend.svc.cluster.local:8000/health
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

## 2026-09-10: first attempt, blocked inside step 1

Ran on the M1 Pro MacBook Pro (16GB, macOS 14.3.1). No VM exists, the
cluster was never touched, and nothing is left to roll back except one
symlink noted below.

### Prerequisites, as found

- `SleepDisabled` was already `1`.
- Multipass 1.16.4 installed, the Pi reachable on 22 and 6443, and
  `joe` + `desktop-j1grrmu` both `Ready` on v1.36.4+k3s1.
- **No USB-C Ethernet adapter attached.** All three ethernet ports read
  `status: inactive`; the only address is Wi-Fi. Bridging onto `en0` was
  chosen deliberately, accepting that the DHCP reservation has to be
  redone when the VM moves to wired, since rebridging changes its MAC.
- The Pi accepted only password SSH from this Mac. Generated an ed25519
  key here and copied it, so the rest of the sequence can run
  non-interactively.

### Blocker: Multipass's bundled QEMU cannot run on macOS 14.3.1

Three consecutive launch failures turned out to be one bug. First
`Failed to amend image to QCOW2 v3: qemu-img ... Process crashed`, then,
once that was worked around, `failed getting vmstate` from
`qemu-system-aarch64`.

The crash report shows a call into address `0x0` from `has_help_option`.
Both binaries **weak-import `strchrnul` from libSystem**, and
`dlsym(libSystem, "strchrnul")` returns `0x0` on this OS - the 14.4 SDK
on this machine does not declare it either. A weak import that resolves
to null is a call to address zero, which is why only option-parsing
paths die:

```
qemu-img create -f qcow2 y.qcow2 10M              # ok
qemu-img create -f qcow2 -o compat=1.1 y.qcow2 10M # SIGSEGV
```

Multipass always runs `qemu-img amend -o compat=1.1` to prepare an
image, so no instance could ever be created. The binaries pass
`codesign -v --strict`, so this is not a corrupt install:
`LC_BUILD_VERSION` reads `minos 13.3` / `sdk 26.5`. Canonical builds
against an SDK far newer than the floor the cask advertises, and the
weak import is the seam where that shows.

Worth keeping as a shape, not just an incident: **a crash with an empty
error message and a frame at address zero is a missing weak-linked
symbol until proven otherwise**, not a corrupt file or a bad input. The
first two hypotheses here - a truncated image download, then a damaged
install - both had supporting evidence and both were wrong.

### Homebrew QEMU is a half-fix only

`brew install qemu` (11.1.1, built `minos 14.0` / `sdk 14.5`) handles
`-o` correctly, and symlinking its `qemu-img` over Multipass's got image
preparation to pass - which is how the failure moved from `qemu-img` to
`qemu-system-aarch64`. It **cannot** replace the system emulator: it
links `vmnet` but not `Hypervisor.framework`, so `-accel help` lists
`tcg` alone. A K3s node under software emulation is not worth having.

### Chosen fix: update macOS

`softwareupdate --list` offers Sonoma 14.8.9 and Tahoe 26.6.2. **Sonoma
is the wrong choice here** - `strchrnul` is absent from Sonoma
entirely, so a 13GB point update would leave Multipass exactly as
broken. Tahoe 26.6.2 is the family Canonical built against.

Two things this pulls in that are not really about K3s: VMware Fusion
and Docker Desktop may need their own updates for Tahoe, and this Mac
was 2.5 years behind on security updates regardless.

### Left in place, to undo after the update

`/Library/Application Support/com.canonical.multipass/bin/qemu-img` is a
symlink to `/opt/homebrew/bin/qemu-img`; the original is beside it as
`qemu-img.broken`. Once Multipass's own binary works, restore it.

### Not a blocker, but measured

The image pull ran at roughly 1.5MB/s. The Wi-Fi link is not at fault -
`-43dBm`, 46dB SNR, 433Mbps negotiated - the WAN is, at around 40Mbps
shared. Two nodes on Wi-Fi remains the fragility this plan already
names; the Ethernet adapter is still the right next purchase.

### Resolved by the update, and step 1 completed

macOS 26.6.2 (build 25G83). The diagnosis held exactly:

```
strchrnul = 0x18d530060           # was 0x0
qemu-img create -o compat=1.1     # rc=0, was SIGSEGV
qemu-system-aarch64 -accel help   # hvf, tcg
```

`hvf` means the VM is hardware-accelerated, which was the whole reason
for rejecting Homebrew's QEMU as a substitute.

The instance: `m1-node`, Ubuntu 24.04.4 arm64, 4 CPU / 6GB / 40GB,
bridged onto `en0` with MAC `52:54:00:d1:c5:8e` and LAN address
`192.168.1.63`. It also picked up a SLAAC address on the LAN's
`2600:6c51:4500:20e2::/64` prefix, so IPv6 reaches it too. Step 1's
actual test - **from the Pi**, before K3s exists - passes at 0% loss.

### The one thing to get right in step 2

A `--bridged` Multipass VM has *two* interfaces, and the NAT one wins:

```
default via 192.168.252.1 dev enp0s1 metric 100   # multipass NAT
default via 192.168.1.1   dev enp0s2 metric 200   # bridged LAN
```

K3s derives its node IP from the default route, so a plain agent install
registers `192.168.252.2` - an address the Pi cannot reach. The join
must be explicit:

```
--node-ip 192.168.1.63 --flannel-iface enp0s2
```

Worth noticing that this is the same sentence as the WSL2 problem - "the
worker is not really a host on the network" - arriving by a different
road. Bridging makes it *possible* for the node to be an ordinary L2
host; it does not make it *automatic*.

### Corrections to the entry above

The WAN is not the ~40Mbps that entry claims. Idle, it does 88Mbps to
Cloudflare; the 40 was measured while Multipass was saturating it. The
slow host was `cloud-images.ubuntu.com` specifically, which served
593 B/s at one point and 44Mbps an hour later. Blaming the local link
was wrong.

Also: the Mac's own DHCP lease moved from `.198` to `.180` across the
update reboot. Nothing depends on it, but it is a live argument for
reserving the VM's address rather than trusting a lease.

### Still open after step 1

- **DHCP reservation for `52:54:00:d1:c5:8e`** at the router, before the
  join, so the node never has to re-register on a new address.
- **The `qemu-img` symlink is still in place.** Multipass's own binary
  now works and can be restored.
- Ethernet adapter still unattached; the VM is bridged onto Wi-Fi.

### Steps 2 and 3, done

The join, with both pins that the two-default-route problem demands:

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.36.4+k3s1 \
  K3S_URL=https://192.168.1.253:6443 K3S_TOKEN=... sh -s - agent \
  --node-ip 192.168.1.63 --flannel-iface enp0s2
```

The version is pinned deliberately - the other two nodes are on
`v1.36.4+k3s1`, and letting the installer take latest would introduce a
version skew that has nothing to do with this migration.

`m1-node` went `Ready` in 19 seconds, and its `INTERNAL-IP` reads
`192.168.1.63`, not `192.168.252.2` - which is the whole point of the
pins. The Pi's `ufw` needed no changes: the VM is inside
`192.168.1.0/24`, which every K3s rule there is already scoped to.

Step 3's cross-node test passed on the first try, over the existing
`wireguard-native` backend:

```
{"status":"ok","postgres":"ok","redis":"ok"}
```

That is a pod on `m1-node` reaching a Service backed by pods on the Pi,
so it exercises the tunnel, CoreDNS and Service routing together.

**The command in step 3 above was wrong and has been corrected.**
Without `--command`, `kubectl run` passes the wget line as *args* to
busybox's default entrypoint, which is `sh` - so it runs `sh wget ...`
and fails on a missing script rather than testing anything. A test that
fails for its own reasons is worse than no test, since the obvious
reading is that the pod network is broken.

### Step 4, done - and it verified less than it looks like

`cordon` + `drain --ignore-daemonsets --delete-emptydir-data` completed
cleanly. What was actually on the node:

| Pod | What happened |
| --- | --- |
| `ollama-probe` | Completed leftover, evicted |
| `svclb-traefik-...` | DaemonSet, skipped |
| `node-exporter-desktop` | Evicted, now `Pending` forever |

**Nothing rescheduled onto `m1-node`, because nothing could.** Every
other workload is already pinned to `joe` by `nodeSelector` - postgres,
redis, grafana, prometheus, the dashboard, `argocd-repo-server`,
`adguard-exporter`. The one pod that was pinned to the desktop is pinned
by hostname, so eviction leaves it unschedulable rather than moving it.

So step 4's stated check - "confirm workloads reschedule onto the new
one" - had nothing to confirm. That is consistent with this plan's own
description of the desktop as "carrying almost nothing today", but it is
worth being explicit that the drain proved the *cluster* stayed healthy,
not that the new node can carry work. Post-drain, the cross-node test
still returns `postgres ok / redis ok`, `api.home` answers 200 through
Traefik and `grafana.home` 302s to its login.

`node-exporter-desktop` being `Pending` is the expected end state, not a
failure - the plan already calls for deleting it or replacing it with a
`windows_exporter` scrape target. It will sit `Pending` until that
decision lands.

### Step 5 needs the Windows machine

`kubectl delete node` runs from anywhere, but stopping `k3s-agent`
inside WSL does not. That half has to happen at the desktop, so step 5
is the first point where this migration cannot be driven from the Mac.

### `node-exporter-desktop` deleted

Removed `kubernetes/monitoring/node-exporter-desktop.yaml`. The
`monitoring` Application syncs `kubernetes/monitoring` from `main` with
`prune: true` and `selfHeal: true`, so a `kubectl delete` would have
been reverted within minutes - **the repo is the only place this can be
deleted from**, and it takes effect when this merges, not when it is
committed.

Deleting rather than replacing with a `windows_exporter` target, for
now. Desktop metrics were already listed as a known gap in
[monitoring.md](monitoring.md), and a scrape target for a machine that
is no longer a cluster member is a separate decision from retiring the
node.

Worth noting what this also removes: [security-testing.md](security-testing.md)
names this Deployment as the widest blast radius in the cluster -
`hostNetwork: true`, running as root. That finding is now moot rather
than fixed, which is a different thing and should be re-read as history
next time that doc is revised.

**Left dangling deliberately**, because they are the plan's separate
line items rather than this one:

- 8 panel queries in `grafana.yaml` selecting
  `pod=~"node-exporter-desktop.*"`, which will render empty
- `apps/dashboard`'s `DESKTOP_SELECTOR` and `get_desktop_metrics`, which
  degrade to the "no desktop pods to check right now" state the UI
  already knows how to show

Prometheus needs no change - it discovered the pod through the existing
`kubernetes-pods` job, not a dedicated scrape config.
