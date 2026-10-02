# Worker node migration: WSL2 to an M1 MacBook

Done 2026-09-10. The cluster's worker moved from a WSL2 VM on the Windows
desktop to a Linux VM on an M1 MacBook. The desktop left the cluster and
is now just the GPU host for Ollama.

```
LAN
 +-- Pi 5            K3s control plane (unchanged)
 +-- M1 MacBook      K3s worker: Ubuntu 24.04 VM (Multipass), bridged, 192.168.1.63
 +-- Windows PC      Ollama on the RTX 3070 Ti, not in the cluster
```

## Why

Nearly every hard failure in [kubernetes.md](kubernetes.md) came from one
thing: a WSL2 VM isn't really a host on the network. VXLAN was dropped by
Windows, `host-gw` was refused, WireGuard needed a reboot workaround, idle
detection cold-booted the node every minute, and pods couldn't reach
Ollama on their own machine. A bridged VM on a Mac is an ordinary network
host, so the move removes the whole class of problem, not one instance.

## What it cost

- **Both nodes are arm64**, so CI dropped the `linux/amd64` builds.
- **A laptop has to stay awake** (`pmset -c disablesleep 1` on power).
- **Gaming mode** became "unload the model from the GPU" instead of
  draining a node ([gaming-mode.md](gaming-mode.md)).
- **Desktop metrics are gone**: `node-exporter-desktop` had no node left
  to run on. A Windows exporter is an open item ([monitoring.md](monitoring.md)).
- **The second node needs a person after a power cut** - see Limits.

## How it went

Each step was verified before the next, and the old node stayed in the
cluster until the new one had proven itself.

1. **Created the VM.** Blocked at first: Multipass's QEMU crashed on
   macOS 14.3.1 - see below. Fixed by updating macOS to 26.6.2.
2. **Joined it as a second worker**, alongside the old one. A bridged
   Multipass VM has two default routes, and the NAT one wins, so K3s
   would register an address the Pi can't reach. The join pins it:
   `--node-ip 192.168.1.63 --flannel-iface enp0s2`. Ready in 19 s.
3. **Tested cross-node traffic properly:** a pod on `m1-node` calling a
   Service backed by pods on the Pi returned `postgres ok / redis ok`.
   (Prometheus targets alone can mislead; host-network pods answer even
   when the pod network is broken.)
4. **Drained the WSL2 node.** Nothing moved, because almost everything is
   pinned to the Pi - so this proved the cluster stayed healthy, not that
   the new node can carry load.
5. **Deleted the WSL2 node**, stopped its agent, deleted the CronJob that
   tracked the desktop's address, and gave all three machines DHCP
   reservations instead.
6. **Moved Ollama back to native Windows**, disabled the two WSL2
   Scheduled Tasks, and removed their scripts.

**Added later (2026-10-01):** m1-node's agent reads its pod DNS upstream
from `/etc/rancher/k3s/resolv.conf` (`nameserver 192.168.1.253`, set in
`/etc/rancher/k3s/config.yaml`), so the cluster's DNS goes through the Pi
wherever CoreDNS runs - see [kubernetes.md](kubernetes.md). m1-node is in
the Ansible inventory for that (`roles/k3s`, `k3s_role: agent`); a rebuilt
VM needs the control nodes' keys in `ubuntu`'s `authorized_keys`, then
`ansible-playbook playbooks/site.yml --tags k3s --limit m1`
([ansible.md](ansible.md)).

**Not done:** switching flannel back to `vxlan`. It still runs
`wireguard-native`, which works fine between the two nodes. If it's ever
switched, the old backend's routes have to be deleted by hand on every
node ([kubernetes.md](kubernetes.md)).

## The macOS bug worth remembering

`qemu-img` crashed with no error message, at address `0x0`, only when
given options. The binaries weak-import `strchrnul`, which doesn't exist
in macOS 14.3 - a weak import that resolves to nothing is a call to
address zero. Multipass always passes options when preparing an image, so
no VM could ever be created. The first two guesses (a bad download, a
damaged install) both had supporting evidence and were both wrong.
Homebrew's QEMU fixed the image step but has no hardware acceleration.
The macOS update fixed it properly (`strchrnul` resolves, `-accel` lists
`hvf`).

**Lesson:** a crash with an empty message and a frame at address zero is
a missing weak-linked symbol until proven otherwise.

## Addresses

| Device | MAC | Reserved IP |
|---|---|---|
| `m1-node` (VM) | `52:54:00:D1:C5:8E` | 192.168.1.63 |
| Windows desktop | `CC:28:AA:53:AA:A4` | 192.168.1.131 |
| `joe` (Pi) | `2C:CF:67:59:A4:C6` | 192.168.1.253 (also static on the Pi) |
| MacBook (m1-node's host; the webcam, [cam.md](cam.md)) | `42:03:36:38:18:63` (a macOS private Wi-Fi address - keep it Fixed) | 192.168.1.180 |

`kubernetes/ai/inference.yaml` holds the desktop's address. Argo CD
ignores Endpoints, so a change there needs `kubectl apply` by hand.

## Limits

- **After a power cut, the second node stays down until someone types the
  Mac's password.** FileVault is on and auto-login is off, so the Mac waits
  at the unlock screen and Multipass can't start. For planned reboots,
  `sudo fdesetup authrestart` unlocks once without anyone present. The
  backups on the Mac have the same dependency ([backups.md](backups.md)).
  Options if this matters: auto-login (weakens FileVault), or a mini PC
  that boots unattended.
- **The VM is bridged onto Wi-Fi**; a USB-C Ethernet adapter would make it
  wired.
- **Postgres and Redis stay on the Pi.** Their volumes are tied to its
  disk, so moving them would be a data migration.
