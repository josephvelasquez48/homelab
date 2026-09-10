# DNS (CoreDNS + AdGuard Home)

Runs on the Raspberry Pi, both `network_mode: host` so CoreDNS can bind
port 53 directly and reach AdGuard Home over loopback.

Two layers, not one tool doing both jobs:

- **CoreDNS** is still the LAN-facing server every client points at,
  unchanged from Milestone 1 - config-as-code (`Corefile`), and it's
  literally the DNS server Kubernetes uses internally. `home:53` serves
  internal hostnames from `home.hosts`; `.:53` forwards everything else
  to AdGuard Home instead of a public resolver directly.
- **AdGuard Home** sits behind it, not in front - its DNS listener is
  bound to `127.0.0.1:5335` (loopback-only, set during its own setup
  wizard), reachable only from CoreDNS on the same host, never directly
  from the LAN. It owns ad/tracker filtering and forwards the survivors
  upstream to `1.1.1.1`/`8.8.8.8`. Only its web UI (`:3000`) is
  LAN-reachable, for the query log, per-client stats, and blocklist
  management the old hosts-file approach didn't have.
- **adguard-exporter** polls AdGuard's `/control/status` + `/control/stats`
  and re-exposes exactly the metrics `kubernetes/monitoring/grafana.yaml`'s
  dashboard queries as Prometheus metrics - AdGuard has no native
  `/metrics` endpoint. Code lives in `apps/adguard-exporter`, but it runs
  as a real pod (`kubernetes/monitoring/adguard-exporter.yaml`), not a
  Compose service here - moved off Compose specifically so a NetworkPolicy
  could restrict ingress to it to Prometheus only, which isn't possible
  for a host-network Compose container (it's not a pod, so there's no
  ingress boundary a NetworkPolicy can attach to). It's still
  `nodeSelector`-pinned to `joe` and still reaches AdGuard over the Pi's
  real LAN IP rather than a public resolver - same underlying
  same-node-bypasses-ufw mechanism this file used to document as an
  accepted gap when the exporter ran here; moving it into the cluster is
  what actually closed that gap instead of just accepting it.

This replaced a single-layer CoreDNS setup that did its own ad-blocking
via a `hosts`-plugin blocklist (StevenBlack's list, refreshed by a
systemd timer) - see `docs/milestone-1.md` for why CoreDNS was originally
chosen over a tool like this one, and the log entry in `docs/kubernetes.md`
(or `docs/milestone-1.md`, whichever this session's log landed in) for why
it's layered in now rather than replacing CoreDNS outright: keeps the
config-as-code/K8s-relevant DNS story CoreDNS was chosen for, while
getting AdGuard's per-client visibility for actual day-to-day use.

## Deploy (Pi, manual for now)

```bash
cd ~/apps/homelab/docker/dns
docker compose up -d
```

(adguard-exporter deploys separately now - CI on `apps/adguard-exporter/**`,
same as `apps/api`/`apps/dashboard`, not part of this Compose project.)

Then, one-time setup for AdGuard Home:

1. Browse to `http://192.168.1.253:3000` (LAN only).
2. Work through the setup wizard:
   - Admin Web Interface: `0.0.0.0`, port `3000` (already exposed this way).
   - **DNS server: set the listen interface/port to `127.0.0.1:5335`
     explicitly** - do not accept a default of `0.0.0.0:53`, that's
     CoreDNS's port and AdGuard will fail to bind it anyway. If the
     wizard doesn't offer a custom port field, finish the wizard with
     whatever port it picked, then `docker compose stop adguardhome`,
     edit `adguard/conf/AdGuardHome.yaml` (`dns.bind_hosts: ["127.0.0.1"]`,
     `dns.port: 5335`), and `docker compose start adguardhome`.
   - Upstream DNS servers: `1.1.1.1`, `8.8.8.8` (matches what CoreDNS used
     to forward to directly).
   - Create the admin account.
3. In the UI, **Filters -> DNS blocklists**, confirm AdGuard's default
   filters are enabled (or add `https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts`
   as a custom blocklist for parity with the old setup).

`adguard/conf` and `adguard/work` are gitignored (live config + query
log/stats, not source) - back them up manually if you care about
history/stats surviving a Pi rebuild.

The admin login itself is backed up separately, SOPS-encrypted, at
`docker/dns/secrets/adguard-admin.enc.yaml` (`sops --decrypt` to read it -
see [docs/secrets.md](../../docs/secrets.md)). This is a plain reference
copy only, not wired into `kubernetes/secrets/apply.sh` or anything
automated - AdGuard's real, live credential is the bcrypt hash already
sitting in `adguard/conf/AdGuardHome.yaml`. If that file is ever lost
(Pi rebuild without an `adguard/` backup), this encrypted copy tells you
what the login *was*, not a source of truth AdGuard reads from.

## Verify

```bash
dig @192.168.1.253 api.home +short                         # -> 192.168.1.253
dig @192.168.1.253 example.com +short                      # -> real answer, forwarded via AdGuard
dig @192.168.1.253 mediavisor.doubleclick.net +short        # -> 0.0.0.0 (or NXDOMAIN), blocked by AdGuard
```

(the bare apex `doubleclick.net` is *not* blocked by most lists, only
specific ad-serving subdomains are - a fine detail worth remembering
before assuming a test failed)

Also check the AdGuard Home UI's query log (`http://192.168.1.253:3000`)
shows real queries flowing through - that's the whole point of adding
this layer over the old blocklist file.

## AdGuard's rate limit has to be 0 behind CoreDNS

`ratelimit` in `AdGuardHome.yaml` is **per client IP** and ships at 20
queries per second. CoreDNS sits in front and forwards everything from
`127.0.0.1`, so AdGuard sees the entire LAN as one client sharing a single
20 qps bucket.

That is invisible while a device or two points at the Pi. It becomes a
total DNS outage the moment the router hands `192.168.1.253` to every
client over DHCP - phones, TVs and laptops all funnel into that one bucket,
AdGuard silently drops everything past the limit, and dropped queries look
like timeouts rather than errors, so nothing names the cause. Found exactly
that way; see [docs/router-migration.md](../../docs/router-migration.md).

Set it to `0` under **Settings -> DNS settings -> Rate limit** in the UI,
which applies live with no restart. The limit exists to protect a
LAN-facing resolver from an abusive client, and this AdGuard is not
LAN-facing at all - it is bound to `127.0.0.1:5335` and only CoreDNS can
reach it. It was rate limiting its own forwarder.

Verify with a burst rather than by reading the setting back - 120
concurrent unique names, none of which should be dropped:

```bash
for i in $(seq 1 120); do ( dig +tries=1 +time=3 @127.0.0.1 p$i-$RANDOM.example.com >/dev/null 2>&1 || echo DROP ) & done; wait
```

**The same root cause costs per-client visibility.** Every query in
AdGuard's log shows as `127.0.0.1`, because that is genuinely who sent it -
which undercuts the per-client stats this layer was added for once the
whole LAN is behind CoreDNS. See the doc above for the trade-off.

## Client setup

**The router (Spectrum SAX1V1S) doesn't support DHCP DNS override** - its
app's "Manage DNS" setting only affects the router's own upstream queries,
not what it hands out to LAN clients via DHCP. So this has to be configured
per-device rather than network-wide. On Windows, in an elevated PowerShell:

```powershell
Set-DnsClientServerAddress -InterfaceAlias "Ethernet" -ServerAddresses ("192.168.1.253","fd00:f405:95c7:c412::253")
```

Both an IPv4 and IPv6 address are needed - Windows prefers IPv6 for DNS
when an IPv6 server is configured, and the router advertises one via IPv6
Router Advertisements regardless of what's set here, so an IPv4-only
override gets silently bypassed for anything using the OS's default
resolver (`nslookup`/`Resolve-DnsName` with an explicit `-Server` flag
isn't affected either way, which is what makes this confusing to debug -
explicit-server tests pass while everyday resolution doesn't). The IPv6
address used is the Pi's own static address inside the router's
self-generated ULA prefix (`fd00:.../64`) rather than its public
ISP-delegated one, since ULA doesn't change if Spectrum ever rotates the
delegated prefix.

**The Pi itself needs this too, and it's easy to forget** - running
CoreDNS doesn't make the Pi's own OS use it for its own resolution.
Found this the hard way: `*.home` worked from every other LAN client
but failed to resolve from a browser running directly on the Pi's own
desktop session, because `/etc/resolv.conf` (NetworkManager-managed)
still pointed at the router. Fixed by pointing the Pi at itself instead
of another host's IP - `127.0.0.1`/`::1` rather than `192.168.1.253`,
since there's no reason to route through the LAN for a service running
locally:

```bash
sudo nmcli connection modify <connection-name> ipv4.dns '127.0.0.1' ipv4.ignore-auto-dns yes
sudo nmcli connection modify <connection-name> ipv6.dns '::1' ipv6.ignore-auto-dns yes
sudo nmcli connection up <connection-name>
```

Verified both directions after: `*.home` resolves (`getent hosts
dashboard.home`) and external resolution still forwards correctly
(`getent hosts google.com`) - `ignore-auto-dns` only stops DHCP from
overwriting the DNS *server* used, it doesn't break CoreDNS's own
upstream forwarding for non-`.home` queries.
