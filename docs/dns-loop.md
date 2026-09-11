# The DNS resolver loop

Found 2026-09-10 while chasing "certain services on my iPhone are slow or
not working". Maps said "cannot connect to server", Mail did something
similar, an image would not send in Messages, and the Wells Fargo app
misbehaved. All four worked on cellular and failed on Wi-Fi.

## What was actually wrong

One in five DNS queries on the LAN was failing. Over a four hour window
CoreDNS answered 24,064 legitimate queries and returned SERVFAIL for
5,193 of them, 21.6%. The phone sat at 46%.

Two things were generating that load, neither of them a client.

**A resolver loop.** AdGuard resolves every client IP back to a name
(`clients.runtime_sources.rdns: true`). It is configured with
`use_private_ptr_resolvers: true` but `local_ptr_upstreams: []`, so those
private reverse lookups fall back to the system resolver.
`/etc/resolv.conf` on the Pi is `127.0.0.1`, which is CoreDNS, and
CoreDNS forwarded everything to AdGuard. So every private reverse lookup
went AdGuard to CoreDNS to AdGuard until it timed out. One address,
`192.168.1.180`, produced 2,430 SERVFAILs in ten minutes.

**A Bonjour flood.** avahi had `enable-wide-area=yes`, which makes it
query `lb._dns-sd._udp.<reverse-subnet>.in-addr.arpa` over unicast DNS.
123,341 of those in four hours, about 90% of all traffic through the
resolver, every one failing and every one forwarded out to Cloudflare and
Google.

A third setting made both worse. `cache 30` in the Corefile capped every
answer at 30 seconds regardless of what the origin published. apple.com
publishes 900. Every device on the network re-resolved everything roughly
thirty times more often than it needed to.

## The fix

- CoreDNS now answers the RFC1918 reverse zones itself with NXDOMAIN and
  never forwards them. This breaks the loop structurally, at the layer
  that was completing the circuit, rather than depending on AdGuard being
  configured correctly.
- `enable-wide-area=no` in `/etc/avahi/avahi-daemon.conf`.
- `cache 30` raised to `cache 3600`. It is a ceiling, not a floor:
  answers keep their own TTL when it is shorter.

The 172.16/12 zones are listed out individually rather than written as
`172.in-addr.arpa`, because the rest of 172/8 is public address space
whose reverse lookups should still resolve.

## Before and after

| | before | after |
| --- | --- | --- |
| SERVFAIL rate | 21.6% | 0% |
| Bonjour junk queries | ~13/sec | 0 |
| Private PTR lookups | thousands | 0 |
| Queries from the Pi itself | ~1,400 per 10 min | 2 per 45 sec |
| apple.com TTL served | 30 | 567 |

## Left alone deliberately

AdGuard still has `use_private_ptr_resolvers: true` with no
`local_ptr_upstreams`. It will still emit private reverse lookups, but
they now get an instant authoritative NXDOMAIN instead of looping, so
they cost nothing. Fixing it there as well would be belt and braces; the
structural fix is the one that matters, and fewer changes meant a
smaller blast radius on a service the whole house depends on.

## Why every DNS slot points at the Pi

All four slots, both address families, point at the Pi. That is
deliberate and predates this incident - see router-migration.md. The
router's IPv6 DNS form requires a secondary and rejects a blank, and its
IPv4 form appends the gateway when left blank, so any slot not given to
the Pi becomes an unfiltered resolver on the network.

That is worse than it sounds, because clients do not reliably prefer the
primary. milestone-1.md records Windows racing its configured servers and
the fast public resolver winning almost every time, silently defeating
the filtering. `::254` exists to satisfy a form field, not to provide
redundancy: one host, one CoreDNS.

So the single point of failure is real, but it is a chosen trade rather
than an oversight, and the alternative reintroduces a documented bypass.

## Phone-side settings, unrelated to the above

Private Wi-Fi Address was set to Rotating, so the phone periodically
takes a new MAC and a new lease, which drops connections mid-flight.
Limit IP Address Tracking was on, which routes Mail and Safari through
Apple's relay even with iCloud Private Relay off, and is why the query
log showed thousands of `mask.icloud.com` lookups.

## Second cause: blocked names answered as 0.0.0.0

The loop above was real and fixing it took the LAN failure rate to zero,
but the phone still could not use the Wells Fargo app. The reason was not
visible in any of the SERVFAIL counting, because AdGuard's `blocking_mode`
was `default`, which answers blocked names with `0.0.0.0` and `::` and a
NOERROR status. Every blocked lookup had been counted as a success.

Four of the app's dependencies were blocked that way:

| Domain | What it is |
| --- | --- |
| `gbxreport-prod.wf.com` | Wells Fargo's own domain |
| `pdx-col.eum-appdynamics.com` | AppDynamics end-user monitoring |
| `edge.adobedc.net` | Adobe analytics |
| `dpm.demdex.net` | Adobe audience manager |

The app was dialling `0.0.0.0` and sitting there until it gave up.

Two changes, both on the Pi since AdGuard's runtime config is not tracked
in this repository:

- **`user_rules`** now allowlists `wf.com` and `eum-appdynamics.com`. A
  bank's own domain must never be filtered, and the monitoring kit is one
  banking apps commonly refuse to finish starting without. The two Adobe
  tracker domains are deliberately still blocked; they are general
  trackers, not something the app needs to function.
- **`blocking_mode`** changed from `default` to `nxdomain`. Returning an
  unroutable address makes a client open a connection and wait for it to
  time out. Returning NXDOMAIN makes the lookup fail instantly, which is
  what lets an app skip an optional analytics call instead of hanging on
  it. This is the difference between a blocklist that costs nothing and
  one that breaks apps.

Verified after the change: blocked names return NXDOMAIN, allowlisted
names resolve, ordinary names resolve, and the `.home` zone still works.

### Worth remembering

Counting SERVFAIL is not the same as counting failures. With the default
blocking mode, a blocked name looks identical to a working one in the
CoreDNS log - NOERROR, fast, an answer section. The only way to see it is
to look at the address being returned.

## Resolution: the blocking mode was the main cause

The section above treats `blocking_mode` as a Wells Fargo detail. It was
not. It was the thing breaking nearly every app on every iPhone in the
house, and it had been doing so quietly for as long as the setting had
been at its default.

What proved it, after a long detour through things that turned out to be
healthy: pointing one iPhone at `1.1.1.1` fixed everything. That single
result cleared the uplink, the radio, MTU, QUIC, IPv6, the router and the
phones, because nothing changed except which resolver answered. Turning
AdGuard's filtering off entirely, with the Pi still resolving, fixed it
too. Turning filtering back on with `blocking_mode: nxdomain` left the
apps working.

So the fault was never that names were blocked. It was how they were
blocked.

`blocking_mode: default` answers a blocked name with `0.0.0.0` and `::`.
The client gets a valid-looking address, opens a connection to it, and
waits for a timeout that never usefully arrives. Most apps call some
analytics or telemetry endpoint during startup. On this network every one
of those calls became a stall. `nxdomain` makes the lookup fail
instantly, so the app skips the optional call and carries on.

That is the difference between a blocklist that costs nothing and one
that breaks the network it is protecting.

### Why it took so long to find

Every measurement said the resolver was healthy, and every measurement
was wrong in the same way. Blocked answers were returned as NOERROR with
an answer section and a sub-millisecond response time, so they were
indistinguishable from success in the CoreDNS log. The entire evening was
spent counting SERVFAILs while the actual failures were being recorded as
successes.

iPhones were hit hardest because they are the chattiest devices on the
network and run the most apps that phone home on launch. The Mac was
fine throughout, which repeatedly pointed the investigation away from
DNS.

### Final state

| Setting | Value |
| --- | --- |
| `blocking_mode` | `nxdomain` |
| `protection_enabled` | `true` |
| `user_rules` | allowlists `wf.com`, `eum-appdynamics.com` |

Measured after: 1573 queries, 2 SERVFAIL. Blocked names are all genuine
tracker traffic, and the iPhones are the busiest clients on the network.

## Also done: the Pi moved to Ethernet

The Pi had no wired connection, so every DNS query from every device
crossed the air twice. `192.168.1.253` and the two IPv6 service
addresses now live on `eth0` at gigabit, and `wlan0` is down with
autoconnect disabled so the dual-homed state cannot return on reboot.

Both interfaces were briefly on the same subnet with two default routes,
which is worth avoiding: replies leave by a different path than requests
arrive on. Moving the addresses and disabling Wi-Fi removed that.

IPv6 dropped up to 45% of queries for about a minute afterwards. That was
neighbour caches around the network still mapping those addresses to the
Wi-Fi MAC. It cleared on its own once the caches refreshed; nothing
needed fixing, but it is worth expecting when service addresses change
interface.

## A recommendation withdrawn

During this incident I twice suggested giving the second DNS slot to a
public resolver for redundancy. That was wrong, and the reasoning against
it was already written down in router-migration.md and milestone-1.md
before I started. See the section above.

It would have been actively harmful here. A public resolver in the second
slot does not filter, and clients race their configured servers rather
than preferring the primary. Tonight's fault was apps stalling on blocked
names; with a bypass in place the symptom would have come and gone
depending on which resolver won each race, which is far harder to
diagnose than a consistent failure - and the ad blocking would have been
quietly defeated the rest of the time.
