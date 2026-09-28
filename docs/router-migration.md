# Router migration

Not on the original roadmap. The Spectrum SAX1V1S was replaced by a router
that supports two things the old one couldn't:

- **Handing out the Pi as every device's DNS server (DHCP DNS).** Before,
  each device had to be pointed at CoreDNS by hand, so phones and TVs got
  no ad blocking ([milestone-1.md](milestone-1.md)).
- **DHCP reservations.** The desktop's address had moved twice and broken
  in-cluster inference ([kubernetes.md](kubernetes.md)).

The swap touched more of the project than any other hardware change.

## What happened, in order

1. **The Pi went unreachable.** The new router used `192.168.0.0/24`. The
   Pi answered ping and nothing else: ufw and the Traefik NetworkPolicy
   both allow only `192.168.1.0/24`, so every port was closed - correctly.
   It was found by its MAC vendor prefix in the desktop's ARP table (a host
   that refuses every connection still answers ARP), after a port scan
   turned up a decoy device.
2. **Changed the router back to `192.168.1.0/24`** rather than migrate the
   repo: one router setting instead of ~8 files, 5 firewall rules and the
   K3s certificates.
3. **The Pi lost its DNS settings.** The new Wi-Fi network name made
   NetworkManager create a fresh profile without the old one's
   customisation: the Pi's own resolver (`127.0.0.1`) and its static IPv6
   address. Re-applied, and checked with `nmcli` rather than trusting
   `resolv.conf` at that moment.
4. **Pointing DHCP DNS at the Pi took down DNS for the whole network** -
   finding 1 below.
5. **Turning IPv6 on bypassed the ad blocking** - finding 2.
6. **Made the Pi's IPv4 address static** on the Pi itself.

## Finding 1: AdGuard's rate limit collapses behind a forwarder

AdGuard's stock `ratelimit: 20` is per client IP. CoreDNS forwards every
query to AdGuard from `127.0.0.1`, so the whole LAN shared one 20
queries-per-second bucket. Harmless with two devices, a total outage the
moment DHCP sent every phone and TV there - and dropped queries look like
timeouts, not errors, so nothing named the cause.

**Fix:** `ratelimit: 0`. AdGuard only listens on `127.0.0.1:5335`, for
CoreDNS; it was rate-limiting its own forwarder. **Verified** with 120
concurrent unique queries: none dropped (~100 would have been lost before).

**Cost, kept on purpose:** AdGuard sees every query as `127.0.0.1`, so its
per-device statistics are one bucket. Fixing that means putting AdGuard in
front of CoreDNS, which gives up CoreDNS being the config-as-code front door.

## Finding 2: IPv6 handed the LAN an unfiltered resolver

IPv6 had to go on - without it, dual-stack sites half-worked (`curl -4`
succeeded, plain `curl` didn't). But the router advertised Spectrum's
resolvers over IPv6, Windows prefers IPv6 DNS, and every client quietly
stopped using the Pi:

| Query | Default resolver | The Pi |
|---|---|---|
| `mediavisor.doubleclick.net` (an ad domain) | real address | `0.0.0.0` (blocked) |
| `dashboard.home` | NXDOMAIN | `192.168.1.253` |

**Fix:** the Pi holds two static IPv6 addresses (`::253`, `::254`) and all
four DNS slots, IPv4 and IPv6, point at it. The router insists on a
secondary; any public one would win whenever the Pi is slow
([homelab DNS notes](../docker/dns/README.md)).

**Order mattered:** ufw only allowed DNS from the old IPv6 prefix, so the
steps were: pin the addresses, open ufw, test DNS over IPv6 from a client,
*then* change what the router advertises. The other order would have been
another network-wide outage.

**Most of the time went to a stale client.** Windows caches IPv6 DNS
settings, and `ipconfig /renew6` doesn't clear them; `Restart-NetAdapter`
does.

## The IPv6 prefix tripwire

The Pi's IPv6 addresses sit in Spectrum's delegated prefix
(`2600:6c51:4500:20e2::/64`); this router has no private (ULA) prefix like
the old one did. If Spectrum rotates it, four things go stale together and
DNS fails with nothing naming the cause:

- the Pi's two static IPv6 addresses
- the ufw rule for the prefix (`ansible/roles/firewall`, `lan_ipv6_prefix`)
- the IPv6 `ipBlock` in `kubernetes/argocd/traefik-security.yaml` (applied by hand)
- the DNS servers the router advertises

## The Pi's static IPv4

`192.168.1.253` was a DHCP lease all along, held by a reservation in the
router - the part that had just been replaced. It's now set on the Pi
itself, and the reservation stays as a backup. (It was first set on the
Wi-Fi profile; the Pi later moved to Ethernet, and the address with it -
[dns-loop.md](dns-loop.md).)

It was written to the profile and left to apply at the next reboot, not
activated live: a wrong address on a headless Wi-Fi host strands it. The
proof it took is `proto static` on the default route - the address alone
would have looked the same either way.

## What earlier work paid off

The desktop came back on a different address and needed nothing: a
CronJob updated the `inference` Endpoints within five minutes, and the
dashboard looked up the node's current address instead of a fixed one.

## Verification

Checked through real DNS names, not IPs with a `Host` header: both nodes
Ready, Argo CD all Synced, every Prometheus target up, `api.home` healthy,
the dashboard still refusing unauthenticated POSTs, no stale flannel
routes, and the 120-query burst with nothing dropped. Cross-node traffic
was proven with a pod on one node calling a Service on the other -
Prometheus targets alone can mislead, since host-network pods answer even
when the pod network is broken.

## Worth knowing

- For about a minute after the Pi boots, `.home` names resolve and
  external ones don't: AdGuard starts slower than CoreDNS. It clears on
  its own.
- The Pi was on Wi-Fi during this migration, which is why a new Wi-Fi
  name mattered. It's been on Ethernet since ([dns-loop.md](dns-loop.md)).
