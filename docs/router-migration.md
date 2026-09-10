# Router migration

Not part of the original 18-step roadmap. The Spectrum SAX1V1S was
replaced, which turned out to touch more of this project than any other
single hardware change - the old router's limitations had shaped real
design decisions, and its successor invalidated a set of addresses six
files treated as fixed.

## Why it was worth doing

Two limitations of the old router are documented at length elsewhere in
these docs, and both were worked around rather than solved:

- **No DHCP DNS override.** Its "Manage DNS" setting only affected the
  router's own upstream queries, not what it handed LAN clients - so
  every device had to be pointed at CoreDNS by hand
  ([milestone-1.md](milestone-1.md)). Phones and TVs never got
  ad-blocking at all.
- **No static DHCP reservations.** The desktop's lease moved twice
  (`.131 -> .133 -> .132`), which broke in-cluster inference once and
  produced the `inference-endpoint-sync` CronJob, the SSH `HostKeyAlias`,
  and the InternalIP-at-call-time lookup in the dashboard
  ([kubernetes.md](kubernetes.md), [dashboard.md](dashboard.md)).

The replacement supports both.

## The decision: move the router, not the repo

The new router came up on `192.168.0.0/24`, and the Pi went unreachable
the moment it did. Two routes forward:

1. Reconfigure the router's LAN back to `192.168.1.0/24`.
2. Keep `192.168.0.0/24` and migrate the repo to match.

Option 2 meant physical console access to a headless Pi to open `ufw`
first, then coordinated edits across roughly eight repo files, five
Windows Firewall rules, the k3s agent's `K3S_URL`, the k3s server's cert
SANs, and the desktop's own resolver. Option 1 was one field on a device
with full admin access. Took option 1 - the addresses in this repo are
not the interesting part of it, and a subnet is not worth a migration.

## Why the Pi looked dead when it wasn't

It answered ping and nothing else. Every port was closed from the new
subnet:

| Port | Service | What was refusing |
|---|---|---|
| 22, 53, 3000, 6443, 10250 | SSH, DNS, AdGuard, K3s API, kubelet | `ufw`, which allows only `192.168.1.0/24` |
| 80, 443 | Traefik | `traefik-lan-only`, same CIDR |

Both layers failed closed, correctly, and left no way in. Worth noting
because the instinct on a headless host that stops answering is to
suspect the host - here every layer was doing exactly its job, and the
host was fine the whole time.

**Finding it needed a different tool than the last time.** The ping sweep
plus SSH banner approach from [milestone-1.md](milestone-1.md) found a
decoy: an unrelated device offering only `diffie-hellman-group1-sha1`,
which is not a current Raspberry Pi OS. The Pi itself had no open ports
to fingerprint. What identified it was the MAC OUI in the desktop's own
neighbour table - `2c:cf:67:59:a4:c6`, Raspberry Pi Ltd. A host that
refuses every connection still answers ARP.

## Finding 1: a new SSID silently discards the host's network config

The new router uses a different SSID, so NetworkManager on the Pi built a
**fresh connection profile** (`Velas_Home_5G`) instead of reusing the old
one (`Velas_wifi`). A new profile carries none of the old one's
customisation, and two things went with it:

- `ipv4.dns 127.0.0.1` plus `ignore-auto-dns yes` - the Pi's own
  resolver. `/etc/resolv.conf` reverted to the router, and the Pi could
  no longer resolve `.home` for itself. This is the exact failure
  [docker/dns/README.md](../docker/dns/README.md) already warns about,
  reintroduced by a completely different cause.
- The static `fd00:f405:95c7:c412::253` address, which is why the
  desktop's IPv6 DNS entry started timing out. Windows prefers IPv6 DNS,
  tried an address in a prefix that no longer existed on either end, and
  did not fall through to the working IPv4 entry.

Re-applied against the new profile name, and confirmed persistent via
`nmcli -g ipv4.dns,ipv4.ignore-auto-dns` rather than trusting that
`resolv.conf` looked right at that moment - too much of this session was
things that read correctly until the next reconnect.

## Finding 2: AdGuard's rate limit collapses behind a forwarder

The genuinely new one, and the one that cost the most.

With the subnet restored and the cluster healthy, setting the router's
DHCP Primary DNS to `192.168.1.253` - the whole point of the upgrade -
took the entire network's DNS down.

`AdGuardHome.yaml` had the stock `ratelimit: 20`, which is **per client
IP**. CoreDNS forwards every query to AdGuard from `127.0.0.1`, so
AdGuard sees the whole LAN as one client sharing one 20 qps bucket. With
two devices pointed at the Pi that was never close to a limit. With every
phone, TV and laptop funnelled through DHCP it was instant, and dropped
queries surface as timeouts rather than errors, so nothing named the
cause.

Set to `0`. The limit protects a LAN-facing resolver from an abusive
client; this AdGuard is bound to `127.0.0.1:5335` and reachable only by
CoreDNS. It was rate limiting its own forwarder.

**Verified with a burst, not by reading the setting back**: 120
concurrent unique queries through CoreDNS into AdGuard, zero dropped.
Under the old limit roughly a hundred of those would have vanished
silently.

**The same root cause costs per-client visibility**, which is worth
stating plainly because it is the reason AdGuard was added at all: every
query in its log shows as `127.0.0.1`. That was already true, but
harmless while two devices used it. Now that the whole LAN is behind
CoreDNS, the per-client view is a single bucket. Recovering it means
inverting the layering - clients point at AdGuard, AdGuard forwards
`.home` to CoreDNS - which trades away CoreDNS being the LAN-facing,
config-as-code front door it was chosen to be. Not done. Recorded so it
stays a decision rather than a surprise.

## What the earlier work paid for

The desktop came back on `.131` rather than `.132`, and nothing needed
touching:

- The `inference-endpoint-sync` CronJob reconciled the `inference`
  Endpoints to the new address on its own within five minutes.
- The dashboard read the SSH target from the node's InternalIP at call
  time, and verified the host key under the `homelab-desktop` alias
  rather than an address.

Both exist because of the lease move documented in
[kubernetes.md](kubernetes.md). This is the event they were written for,
and neither needed intervention.

The CronJob did log nine failed attempts during the window when the Pi's
own address was moving - `No route to host` reaching the API server,
because the ClusterIP mapping still pointed at the old one. It recovered
on its own once the address settled. The retained failures were cleaned
up afterwards; `failedJobsHistoryLimit: 3` keeps three Jobs at three
attempts each, and nothing evicts them once failures stop.

## Verification

Checked end-to-end through real DNS resolution rather than an IP with a
`Host` header, so the whole chain is proven:

| Check | Result |
|---|---|
| Nodes | `joe` `.253`, `desktop-j1grrmu` `.131`, both Ready |
| Argo CD | 7/7 Synced / Healthy |
| Prometheus targets | 7/7 up, including cross-node |
| `api.home` `/health`, `/ready` | ok, `ollama: "ok"` |
| `dashboard.home` | 200; unauthenticated POST to `/api/gaming/off` still 401 |
| flannel routes | `10.42.0.0/16 dev flannel-wg`, no stale `/24` |
| AdGuard burst | 120 concurrent queries, 0 dropped |

Cross-node scraping being up is the meaningful WireGuard test here.
`wg show` is unavailable on the Pi - `wireguard-tools` is not installed,
and flannel's `wireguard-native` backend uses the kernel module, which
does not need it. Worth installing so the `0 B received` check in
[kubernetes.md](kubernetes.md) actually runs next time.

## Now-dead configuration

The LAN has no IPv6 at all - no GUA, no ULA. Anything scoped to
`fd00:f405:95c7:c412::/64` matched nothing, so it was removed rather than
left in place looking like protection:

- `ansible/roles/firewall` - the prefix is now an empty variable, and the
  IPv6 rules are skipped when it is unset.
- `kubernetes/argocd/traefik-security.yaml` - the IPv6 `ipBlock` is gone,
  with a comment on how to re-add one. Note this file is applied by hand,
  not by Argo CD, so the git change is not live until `kubectl apply`.

## Known gaps

- **The Pi's IPv4 is still DHCP**, now held in place by a reservation
  rather than by static host configuration. The reservation lives in the
  router - the component that just got replaced. Making it genuinely
  static on the host would survive the next swap.
- **The Pi is on Wi-Fi**, so an SSID change alone is enough to strand the
  control plane. Finding 1 is the mild version of that.
- **AdGuard per-client stats are a single bucket**, as above.
