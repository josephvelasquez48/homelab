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

## Not fixed, and worth knowing

The router hands out `192.168.1.253` as both primary and secondary DNS.
There is no fallback resolver. If the Pi stops answering, every device on
the network loses DNS with nothing to fail over to. That is a separate
decision, not a bug, but it is the reason a resolver fault here is felt
everywhere at once.

## Phone-side settings, unrelated to the above

Private Wi-Fi Address was set to Rotating, so the phone periodically
takes a new MAC and a new lease, which drops connections mid-flight.
Limit IP Address Tracking was on, which routes Mail and Safari through
Apple's relay even with iCloud Private Relay off, and is why the query
log showed thousands of `mask.icloud.com` lookups.
