# The DNS incident: a resolver loop, and blocked names that broke apps

2026-09-10. The symptom: on home Wi-Fi, iPhone apps were slow or broken -
Maps couldn't connect, Mail stalled, images wouldn't send in Messages, the
Wells Fargo app misbehaved. All of them worked on cellular.

There turned out to be two separate problems. The first was real and easy
to measure. The second was the one actually breaking the apps, and every
measurement hid it.

## Problem 1: one in five DNS queries failing

Over four hours CoreDNS answered 24,064 real queries and returned SERVFAIL
for 5,193 (21.6%; 46% for the phone). The load came from two things that
weren't clients:

- **A resolver loop.** AdGuard looks up a name for every client IP, sends
  private reverse lookups to the system resolver - the Pi's `127.0.0.1`,
  which is CoreDNS - and CoreDNS forwarded everything back to AdGuard. Each
  lookup went round in a circle until it timed out: 2,430 failures in ten
  minutes from one address.
- **A Bonjour flood.** avahi's `enable-wide-area=yes` sent 123,341 useless
  service-discovery queries in four hours - about 90% of all DNS traffic -
  each one failing and forwarded out to public resolvers.

A third setting made both worse: `cache 30` capped every answer at 30
seconds, so devices re-resolved everything ~30 times more often than needed.

**Fixes:**

- CoreDNS answers private reverse zones (RFC1918) itself with NXDOMAIN, so
  they never leave. That breaks the loop at the layer that closed it. The
  172.16/12 zones are listed individually, since the rest of 172/8 is public.
- `enable-wide-area=no` in avahi.
- `cache 3600` - a ceiling, not a floor; shorter TTLs still apply.

| | Before | After |
|---|---|---|
| SERVFAIL rate | 21.6% | 0% |
| Bonjour junk queries | ~13/s | 0 |
| Queries from the Pi itself | ~1,400 per 10 min | 2 per 45 s |
| apple.com TTL served | 30 s | 567 s |

## Problem 2: blocked names answered with 0.0.0.0

With failures at 0%, the apps still broke. The clue: pointing one iPhone at
`1.1.1.1` fixed everything, and so did switching AdGuard's filtering off.
The problem wasn't *that* names were blocked, but *how*.

AdGuard's `blocking_mode: default` answers a blocked name with `0.0.0.0`
and a normal "success" status. The app gets an address, tries to connect,
and waits for a timeout. Most apps call some analytics endpoint on launch,
so every one of those calls became a stall. `blocking_mode: nxdomain`
makes the lookup fail instantly, and the app skips the optional call.

**Why it hid so long:** a blocked answer looked exactly like success in the
CoreDNS log - success status, an answer, sub-millisecond. The evening went
on counting failures while the real failures were logged as successes. The
Mac was fine throughout, which kept pointing away from DNS.

**Final AdGuard settings** (runtime config on the Pi, not in this repo):

| Setting | Value |
|---|---|
| `blocking_mode` | `nxdomain` |
| `user_rules` | allowlist `wf.com` (the bank itself) and `eum-appdynamics.com` (monitoring banking apps won't start without) |

Measured after: 1,573 queries, 2 SERVFAIL.

**Lesson:** counting SERVFAILs isn't counting failures. Look at the
answers, not just the status codes.

## Also changed

- **The Pi moved to Ethernet.** Every DNS query used to cross the air
  twice. Its addresses now live on `eth0`, and Wi-Fi is down with
  autoconnect off. IPv6 dropped up to 45% of queries for about a minute
  afterwards while the network's neighbour caches caught up.
- **Phone settings:** a rotating private Wi-Fi address kept dropping
  connections, and *Limit IP Address Tracking* sent Mail and Safari through
  Apple's relay (thousands of `mask.icloud.com` lookups).

## Left alone on purpose

- AdGuard still sends private reverse lookups; they now get an instant
  NXDOMAIN, so they cost nothing. The structural fix is in CoreDNS.
- **Every DNS slot points at the Pi, with no public backup.** Twice during
  this incident a public second resolver was suggested for redundancy, and
  it would have been harmful: clients race their DNS servers rather than
  preferring the first, so an unfiltered public one would win most races,
  defeat the ad blocking, and make this fault come and go at random. The
  single point of failure is a chosen trade ([router-migration.md](router-migration.md)).
