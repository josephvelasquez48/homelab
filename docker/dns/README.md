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

## Startup order after a reboot

For about a minute after the Pi boots, `*.home` resolves while external
names do not. This is expected, not a fault: CoreDNS serves `.home` from
`home.hosts` directly, but forwards everything else to AdGuard on
`127.0.0.1:5335`, and AdGuard takes longer to start than CoreDNS does.
Queries that need forwarding fail until it binds.

It clears on its own. Confirm rather than debug:

```bash
docker ps --format '{{.Names}}	{{.Status}}'
sudo ss -lunp | grep :5335
```

The split signature - internal names working, external ones failing - is
the tell. It looks like a forwarding misconfiguration and is only one
container still coming up.

## Client setup

There is nothing to configure per device any more - DHCP hands every
client CoreDNS on both families:

| | Advertised |
|---|---|
| IPv4 (DHCP option 6) | `192.168.1.253`, `192.168.1.253` |
| IPv6 (RDNSS) | `2600:6c51:4500:20e2::253`, `2600:6c51:4500:20e2::254` |

Both slots hold the Pi on purpose, and neither pair is redundancy - it is
one host running one CoreDNS. The duplicates exist because neither
secondary can be left usefully empty: the router's IPv6 form rejects a
blank, and its IPv4 firmware silently appends the gateway when you leave
one. Either default puts an unfiltered resolver on the network - not a
constant bypass, but one that wins whenever the primary is slow or IPv6
drops. Filling the slot with the Pi denies it to something worse.

The Pi holds `::253` and `::254` as static addresses inside the delegated
prefix. That prefix is an ISP-delegated GUA rather than a ULA, because the
current router offers no ULA option - see
[docs/router-migration.md](../../docs/router-migration.md) for the
rotation tripwire that creates.

This replaces the per-device setup this project needed for its first
months. The previous router (a Spectrum SAX1V1S) had no DHCP DNS override
at all, so every client was pointed at the Pi by hand and phones and TVs
never got filtered at all. Retiring that was the reason for replacing it.

Two lessons from that era still apply to any client you configure
manually, and both cost real time to find:

- **Windows prefers IPv6 DNS.** If an IPv6 resolver is advertised, an
  IPv4-only override is silently bypassed for everyday resolution while
  `nslookup`/`Resolve-DnsName` with an explicit `-Server` keeps working -
  which is exactly what makes it confusing to debug.
- **RA-learned DNS is cached for its advertised lifetime.** `ipconfig
  /release6 /renew6` does not clear it; that refreshes DHCPv6 only. Only a
  full adapter restart forces a fresh Router Solicitation.

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

## Tracking AdGuard's config

CoreDNS is config-as-code: the `Corefile` in this directory *is* what runs.
AdGuard is not, and cannot be made so. It owns `AdGuardHome.yaml` at
runtime and rewrites it on every settings change in the web UI, and the
compose mount is read-write because the UI has to be able to save. So
`adguard-config/AdGuardHome.yaml` here is a **snapshot for rebuild and
review**,
not a source of truth.

That distinction is the whole reason this exists. Until 2026-09-10 the
file was untracked, which meant a rebuild from this repo would have come
back with default filtering behaviour and no record that anything had been
chosen. `blocking_mode`, the `user_rules` allowlist, the upstreams and the
filter list selection all lived only on the box.

| Script | Direction | When |
| --- | --- | --- |
| `adguard-capture.py` | Pi to repo | after changing anything in the web UI |
| `adguard-restore.py` | repo to Pi | rebuilding the box |

Run capture after UI changes and `git diff` shows exactly what moved. An
empty diff means the repo matches the Pi. A surprising diff means someone
changed something in the UI and did not say so, which is the drift this is
meant to catch.

**The admin password hash never enters the working tree.** Capture
replaces it with `SOPS_ADGUARD_ADMIN_PASSWORD_HASH`, and the real value
lives in `secrets/adguard-password-hash.enc.yaml`, encrypted to the same
age recipient as everything else here. Restore decrypts it and substitutes
it back on the way to the Pi. Capture refuses to write at all if it does
not find exactly one hash to redact, rather than guessing and risking a
commit of a live credential. The hash is bcrypt cost 5, which is weak
enough that treating it as public would be a real mistake.

Restore stages the complete configuration on the Pi, stops AdGuard, takes
a timestamped backup if a configuration already exists, and atomically
replaces the configuration before starting AdGuard and CoreDNS with Compose.
Stopping AdGuard first prevents it from overwriting the restored settings.
On a fresh rebuild, the script creates the configuration directory and
does not require an existing file or container. Docker with Compose and the
repo checkout at `/home/joe/apps/homelab` must already be present.
If a step fails after AdGuard stops, the script exits with an error; inspect
the configuration and backup before starting the stack again. For routine
changes use the UI and then capture.

### Host settings that live in Ansible, not here

Not everything DNS-related is in this directory. Two host settings matter
to DNS and are codified in the Ansible `common` role rather than captured
by the scripts above, because they are properties of the Pi rather than of
AdGuard (see `docs/dns-loop.md`):

- **`enable-wide-area=no`** in `/etc/avahi/avahi-daemon.conf`. With this
  on, avahi queries `lb._dns-sd._udp.<reverse-subnet>.in-addr.arpa` over
  unicast DNS at roughly 13 per second, all failing, all forwarded
  upstream.
- **The Pi's own resolver**, `ipv4.dns 127.0.0.1` / `ipv6.dns ::1` with
  `ignore-auto-dns` on both, documented above. After the move to Ethernet
  this lives on `Wired connection 1` rather than the Wi-Fi connection.

Both are in `ansible/roles/common`, so a rebuild applies them. The role
looks the connection up by device rather than by name, and fails loudly
if nothing is active on `pi_lan_interface` instead of silently
configuring nothing - that variable had to change from `wlan0` to `eth0`
when the Pi was wired.

Applying the resolver setting requires reactivating the connection, which
drops the Pi off the network for a few seconds. The Pi is the only
resolver on this LAN, so every client loses DNS while that happens. The
handler is deliberately the only thing that bounces it, and the task
guarding it compares against the live values first.

**The Pi is now the Ansible control node.** Ansible 12 is installed with
the required collections bundled. Run from `~/apps/homelab/ansible` with
`ansible-playbook playbooks/site.yml --check --diff -c local`; `joe` has
`NOPASSWD: ALL`, so become needs no password. The check run returned 27 ok,
0 failed, and the resolver task skipped. The two reported changes were an
IPv6 ufw rule comment and the repo task selecting main from a test branch.
See [the Ansible notes](../../docs/ansible.md) for the control-node setup
and the limitations of running recovery automation on the Pi itself.
