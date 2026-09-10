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

## Finding 3: enabling IPv6 hands the LAN a resolver that bypasses AdGuard

IPv6 was off entirely after the swap, and turning it on fixed a real
problem while creating a worse one in the same move.

What it fixed: AAAA records resolved but nothing could route to them, so
dual-stack clients tried IPv6 first and failed without falling back.
`curl -4 https://example.com` returned 200 while plain `curl` returned
nothing, and `gh` failed intermittently against api.github.com.

What it created: the router advertised Spectrum's own resolvers
(`2001:1998:f00:2::1`) via Router Advertisement, and Windows prefers IPv6
DNS. Every client silently stopped using CoreDNS. Measured, not assumed:

| Query | Via default resolver | Via the Pi |
|---|---|---|
| `mediavisor.doubleclick.net` | `142.251.210.110` | `0.0.0.0` |
| `dashboard.home` | NXDOMAIN | `192.168.1.253` |

This is Milestone 1's problem 2 arriving by a different road. Note the
failure shape: `.home` broke because Spectrum answered NXDOMAIN
*authoritatively*, so Windows accepted it and never consulted the IPv4
server that would have answered correctly.

**No ULA on this router.** Its IPv6 LAN page offers four assigned types,
all GUA-based. The old router self-generated `fd00:f405:95c7:c412::/64`,
which is exactly what made pointing IPv6 DNS at the Pi safe before. So the
Pi now holds static addresses inside the ISP-delegated prefix instead.

**Two addresses, and no public secondary.** The router's IPv6 DNS form
requires a secondary and rejects a blank; its IPv4 form accepts a blank
and then appends the gateway itself. Either default puts an unfiltered
resolver on the network - not a constant bypass, but one that wins
whenever the primary is slow. So the Pi carries `::253` and `::254`, and
all four DNS slots across both families point at it. That is not
redundancy - one host, one CoreDNS - it just denies the slot to something
worse.

**Ordering mattered, again.** `ufw` allowed port 53 only from the LAN IPv4
range and the dead ULA prefix, so DNS to the Pi over IPv6 timed out.
Advertising it before opening that would have taken DNS down network-wide,
the same shape as the rate-limit outage above. Done in this order instead:
pin the addresses, open `ufw`, confirm resolution over IPv6 from a client,
and only then change what the router advertises.

**Most of the debugging time went to a stale client, not the router.**
Windows caches RA-learned DNS for its advertised lifetime, and `ipconfig
/release6 /renew6` does not clear it - that refreshes DHCPv6 only. Several
rounds of "the router setting has not taken" were a client holding old
data. An elevated `Restart-NetAdapter` forces a fresh Router Solicitation
and is what finally showed the new values. Worth checking the client can
actually see a change before concluding the change was not made.

## The prefix rotation tripwire

The Pi's IPv6 addresses live in `2600:6c51:4500:20e2::/64`, delegated by
Spectrum. If that rotates, four things go stale at once:

- the Pi's `::253` and `::254` static addresses
- the `ufw` rule scoped to the prefix
- the `ipBlock` in `kubernetes/argocd/traefik-security.yaml`
- the router's advertised RDNSS pair

DNS then fails LAN-wide with nothing naming the cause. Three of the four
live in this repo and could be reconciled the way the `inference`
Endpoints are; the fourth is inside the router, which nothing in the
cluster can reach. So it is recorded as a tripwire rather than solved -
the one place in this project where a stale address cannot be repaired
from the cluster side.

The old router's self-generated ULA had none of this exposure. Losing it
is the real cost of the swap, and it was invisible until IPv6 was turned
back on.

## Configuration that tracks the LAN's IPv6

- `ansible/roles/firewall` - `lan_ipv6_prefix` carries the delegated
  prefix, and the IPv6 rules are skipped when it is empty. Renamed from
  `lan_ipv6_ula_prefix`, which would now describe the value incorrectly.
- `kubernetes/argocd/traefik-security.yaml` - the IPv6 `ipBlock` is back
  with the current prefix. This file is applied by hand, not by Argo CD,
  so a change here is not live until `kubectl apply`.

## Closing the address gap: the Pi's IPv4 is static now

`192.168.1.253` was a DHCP lease for this project's entire life. It never
moved, so eight places across this repo came to treat it as fixed - but
what actually held it was a reservation living inside the router, the one
component that had just been replaced.

It is now configured on the host: `ipv4.method manual`, address
`192.168.1.253/24`, gateway `192.168.1.1`, on the `Velas_Home_5G`
profile. IPv6 was deliberately left on `auto` so SLAAC keeps supplying the
delegated prefix and the default route, with the two static addresses
layered on top.

**Staged, not activated.** `nmcli connection up` reactivates the
interface, and a wrong address or gateway on a headless Wi-Fi host strands
it with no way back in. Writing to the profile and letting it take effect
at the next reboot puts the risk in the one moment physical access is
available anyway. Rollback, if it had been needed:

```bash
sudo nmcli connection modify Velas_Home_5G ipv4.method auto ipv4.addresses '' ipv4.gateway ''
```

**Confirmed by the route, not the address.** After the reboot:

```
default via 192.168.1.1 dev wlan0 proto static metric 600
```

`proto static` rather than `proto dhcp` is the whole proof. The address
alone shows nothing - the reservation would have handed back the identical
value and looked the same.

The router reservation stays in place. A reservation and a matching static
address agree rather than conflict, so it costs nothing and covers the
case where the host config is ever cleared. Narrowing the DHCP pool is
also safe now: this firmware deletes any reservation that falls outside
the pool, and that no longer determines the Pi's address.

**Cross-node networking survived this reboot without intervention**, which
is worth recording because previous reboots have not. The WSL2
kernel-socket quirk in [kubernetes.md](kubernetes.md) is asymmetric: it
affects the *desktop's* WireGuard socket registration, so it bites when
the desktop reboots, not when the Pi does. A Pi reboot only asks the
desktop's already-registered socket to re-handshake with a peer that came
back, which it does on its own.

Checking it needs care, though, because the obvious signal lies. All seven
Prometheus targets read `up` while nothing on the desktop was using pod
networking at all - only `svclb-traefik` and `node-exporter-desktop` were
scheduled there, and both are `hostNetwork`, reachable over the desktop's
real LAN address whether the tunnel works or not. That is the exact
blind spot `apps/dashboard/app/prometheus.py` documents.

The test that actually proves it is a throwaway pod on the desktop
reaching a Service backed by pods on the Pi:

```bash
kubectl run xnode --rm -i --restart=Never --image=busybox:1.36 --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"desktop-j1grrmu"}}}' -- wget -qO- http://api.backend.svc.cluster.local:8000/health
```

It returned `{"status":"ok","postgres":"ok","redis":"ok"}` from pod IP
`10.42.1.151`, in the desktop's own pod subnet. `wg show` on the desktop
agreed - bytes received as well as sent, which is the distinction that
matters, since the documented failure mode is a tunnel that happily sends
and receives nothing.

## A startup order worth recognising

For roughly a minute after the Pi boots, `.home` names resolve while
external ones do not. Not a fault. CoreDNS serves `.home` from its own
hosts file and forwards everything else to `127.0.0.1:5335`, and AdGuard
takes longer to start than CoreDNS does. It clears on its own once
AdGuard binds. Worth recognising rather than debugging - the split
signature, internal working and external failing, looks alarming and
means only that one container is still coming up.

## Known gaps

- **The Pi is on Wi-Fi**, so an SSID change alone is enough to strand the
  control plane. Finding 1 is the mild version of that.
- **AdGuard per-client stats are a single bucket**, as above.
- **The IPv6 prefix is still ISP-delegated and rotatable** - see the
  tripwire section. The IPv4 address is now static, but its IPv6
  counterparts are not, and cannot be until the LAN has a stable prefix.
