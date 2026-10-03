"""What each debug page explains beyond its numbers and logs: how the piece
works, what has gone wrong with it before and what fixed it, and where the
full story is written down.

Every entry comes from the docs it points at (docs/*.md) or from an
incident that was worked through; nothing here is a guess. When a doc
changes, change the entry with it. tests/test_component.py checks that every
box on the map has a guide and that the docs named exist.

how: a few short lines, in the order the data flows.
fixes: (symptom, what to do) pairs, most likely first.
docs: repo paths, shown as "Read more".
"""

GUIDES: dict[str, dict] = {
    "lan": {
        "how": [
            "The router hands every device the Pi (192.168.1.253) as its only DNS server, in all four slots, IPv4 and IPv6.",
            "Names under .home are answered by CoreDNS on the Pi; everything else goes on to AdGuard, which filters it.",
            "Pages under .home resolve to the Pi, where Traefik serves them over HTTPS with the homelab CA's certificate.",
        ],
        "fixes": [
            ("Apps slow or broken on home Wi-Fi but fine on cellular",
             "Suspect DNS first. Compare with one device pointed at 1.1.1.1. In 2026-09 it was blocked names answered with 0.0.0.0 (now NXDOMAIN) and a reverse-lookup loop."),
            ("Someone suggests a public resolver as the router's second DNS",
             "Don't: clients race their servers, the public one usually wins, and filtering silently stops. One host, one CoreDNS, by choice."),
            ("A .home page says 'not secure'",
             "That device doesn't trust the homelab CA yet: install certificates/homelab-ca.crt (name-constrained to .home)."),
            ("For a minute after the Pi boots, only .home names resolve",
             "Normal: AdGuard starts after CoreDNS. It clears by itself."),
        ],
        "docs": ["docs/dns-loop.md", "docs/router-migration.md", "docs/https.md"],
    },
    "internet": {
        "how": [
            "AdGuard forwards allowed queries to 1.1.1.1 and 8.8.8.8; blocked ones never leave the Pi.",
            "Argo CD pulls this repo from GitHub; a watcher on the Pi (apps/argocd-refresh) checks main every 15 s so syncs start at once.",
            "Nothing on the LAN is reachable from outside except the camera: its page through Tailscale Funnel, its video through a UDP 8189 forward to the Mac.",
        ],
        "fixes": [
            ("Every site fails, .home pages still work",
             "Check upstream from the Pi: dig @1.1.1.1 example.com. If that fails too it's the router or ISP; if not, AdGuard (its box)."),
            ("Deploys stopped reaching the cluster",
             "Argo CD can't fetch: look at the Apps box's Argo CD section, and at argocd-repo-server's logs for git or DNS errors."),
        ],
        "docs": ["docs/argocd.md", "docs/cam.md"],
    },
    "coredns": {
        "how": [
            "Docker on the Pi (docker/dns), port 53 for the whole LAN.",
            "Answers *.home from home.hosts in the repo checkout, and reloads it by itself after a git pull.",
            "Answers private reverse lookups (RFC1918) itself with NXDOMAIN, so they can't loop through AdGuard.",
            "Everything else goes to AdGuard on 127.0.0.1:5335. Answers are cached up to an hour.",
        ],
        "fixes": [
            ("Lots of SERVFAIL, or lookups timing out",
             "Look for a loop: a private reverse zone missing from the Corefile sends AdGuard's lookups back to it. That was 21.6% failures in 2026-09."),
            ("A new .home name doesn't resolve",
             "Add it to docker/dns/coredns/home.hosts, merge, git pull on the Pi; CoreDNS picks it up without a restart."),
            ("Nothing resolves at all",
             "docker ps on the Pi - is coredns running? docker restart coredns. Every device depends on it, by design."),
        ],
        "docs": ["docs/dns-loop.md", "docs/router-migration.md"],
    },
    "adguard": {
        "how": [
            "Docker on the Pi, behind CoreDNS: filters ads and trackers, then forwards to the upstreams.",
            "Blocked names get NXDOMAIN, so apps fail fast instead of waiting on 0.0.0.0.",
            "Its numbers reach Prometheus through adguard-exporter, a pod only Prometheus may talk to.",
        ],
        "fixes": [
            ("An app or site is broken and fine on cellular",
             "Find its blocked name in AdGuard's query log (http://192.168.1.253:3000) and allow it. Check blocking_mode is still nxdomain."),
            ("'Answering' is 0 but DNS works",
             "The exporter can't log in or reach AdGuard: its scrape errors are above, its logs below."),
            ("Protection off",
             "Someone paused filtering in AdGuard's UI; turn it back on there."),
        ],
        "docs": ["docs/dns-loop.md", "docs/monitoring.md"],
    },
    "rustdesk": {
        "how": [
            "hbbs (IDs) and hbbr (relay) in Docker on the Pi, LAN only, and only for clients with the server's key.",
            "The Pi's own desktop is a RustDesk client too; it needs X11, not Wayland, to be controlled.",
            "apps/rustdesk-traffic counts bytes to other machines every 10 s for this map.",
        ],
        "fixes": [
            ("Remoting into the Pi is laggy",
             "The Pi has no hardware encoder and the display animates: tap Desktop on the display first to close it."),
            ("Phone music clips during a RustDesk session",
             "The session's rustdesk --cm takes ~a core and starves the phone bridge. Disconnect; check top on the Pi."),
            ("Client says 'not ready' / can't connect",
             "ID and relay server must be rustdesk.home, with the key from docker/rustdesk/data/id_ed25519.pub; LAN only."),
            ("After a Pi reboot you see a login screen",
             "Desktop autologin is off on purpose: sign in there."),
        ],
        "docs": ["docs/rustdesk.md"],
    },
    "phone": {
        "how": [
            "A systemd user service on the Pi (apps/phone): the Pi acts as a Bluetooth hands-free unit for the iPhone.",
            "Calls (HFP) and music (A2DP) come in over Bluetooth; PipeWire hands the audio to the bridge, which streams it to the PC's app.",
            "While the PC is away for 2 minutes the iPhone is blocked, so calls stay on the phone. A dropped phone is reconnected every 30 s.",
            "The display's Bluetooth button and the phone page's switch both turn the Pi's Bluetooth off; while off, nothing reconnects.",
        ],
        "fixes": [
            ("Music or calls clip",
             "Pi load first: top on the Pi, a RustDesk session, the display animating. On the PC, agent.log 'dropped N late chunks'."),
            ("iPhone not connecting",
             "Is the Pi's Bluetooth on (Numbers, above)? Is the PC's app running? journalctl --user -u phone-bridge shows the reconnect lines. Car or earbuds compete for the phone."),
            ("A call rings but no audio on the PC",
             "Is PC audio enabled in the app? 'Call audio being bridged' should be 1 during the call; PhoneCallerSilent alerts when it's silent."),
            ("Bridge 'down' (no update for 2 min)",
             "systemctl --user restart phone-bridge on the Pi - check no call is on first, it would drop it."),
        ],
        "docs": ["docs/phone.md"],
    },
    "phone_pc": {
        "how": [
            "The desktop agent (apps/phone/agent): a tray app that checks in with the bridge every second.",
            "It plays call audio and music, sends the mic back, and pops up the call window when the phone rings.",
        ],
        "fixes": [
            ("App won't open",
             "%APPDATA%\\phone-bridge\\agent.log, and a leftover pythonw.exe in Task Manager."),
            ("'Present' is 0 with the PC on",
             "The agent isn't running or can't reach phone.home:8443 - start it from the Phone shortcut, then read agent.log."),
        ],
        "docs": ["docs/phone.md"],
    },
    "iphone": {
        "how": [
            "Paired and trusted with the Pi; it only reaches the Pi while the Pi's Bluetooth is on and the PC is around.",
            "Calls use HFP (classic Bluetooth), music A2DP. Contacts sync over PBAP if allowed on the phone.",
        ],
        "fixes": [
            ("Not connected, in range",
             "Check the Pi's Bluetooth is on (Phone bridge page). Coming back into range takes up to ~30 s to reconnect."),
            ("Caller names missing",
             "On the iPhone: Bluetooth > the Pi > Sync Contacts on."),
        ],
        "docs": ["docs/phone.md"],
    },
    "backup": {
        "how": [
            "Nightly at 03:00 the Mac pulls a snapshot from the Pi over SSH into an encrypted restic repository.",
            "Missed nights catch up within 15 min of the Mac being back; restic then checks every byte.",
            "Captured: the K3s datastore, Postgres, Redis, Grafana and host config. Not Prometheus history or models.",
        ],
        "fixes": [
            ("HomelabBackupStale (no snapshot for 26 h)",
             "Is the Mac on and awake? Read backup.log and backup-error.log on it; run mac-backup.py by hand."),
            ("Metrics missing altogether",
             "The Mac stopped reporting - its reporter LaunchAgent, or the Mac is off. Missing data is the failure that looks like health."),
            ("Repository unreadable",
             "Check the password file on the Mac and the repository path, then restic check."),
        ],
        "docs": ["docs/backups.md", "docs/monitoring.md"],
    },
    "mac": {
        "how": [
            "The MacBook on Ethernet (192.168.1.219): the backups, the camera (MediaMTX), and m1-node, the cluster's worker VM (.63).",
            "m1-node is a Multipass VM bridged onto the Mac's USB Ethernet; pod traffic to the Pi is plain routed (flannel host-gw).",
        ],
        "fixes": [
            ("m1-node NotReady, or Argo CD 'Unknown' and DNS timeouts from pods",
             "The Mac's network first: ping 192.168.1.63 from the Pi, multipass list on the Mac. Network changes on the Mac have caused this before."),
            ("Outside viewers get the camera page but no video",
             "The router's UDP 8189 forward must point at 192.168.1.219."),
            ("Camera won't start after a macOS prompt",
             "Camera permission can only be granted at the Mac, never over SSH."),
        ],
        "docs": ["docs/node-migration.md", "docs/cam.md", "docs/backups.md"],
    },
    "traefik": {
        "how": [
            "K3s's ingress, pinned to the Pi: every *.home page arrives on 443 and is routed by host name.",
            "externalTrafficPolicy: Local plus a NetworkPolicy keep it LAN-only (K3s's iptables run before ufw's).",
        ],
        "fixes": [
            ("Every .home page down",
             "Is the Traefik pod Running on the Pi? It must be there: DNS points at the Pi."),
            ("Certificate errors from Windows curl",
             "Use --ssl-revoke-best-effort: the homelab CA has no revocation service."),
            ("5xx responses",
             "Usually the service behind it: open that box's page for its pods and logs."),
        ],
        "docs": ["docs/https.md", "docs/argocd.md", "docs/kubernetes.md"],
    },
    "api": {
        "how": [
            "FastAPI at api.home and ai.home, with a Redis-backed job worker.",
            "Stores documents and embeddings in Postgres (pgvector) and asks Ollama on the desktop for chat and embeddings.",
            "Readiness fails if Postgres or Redis is down, but not if Ollama is - the desktop sleeps.",
        ],
        "fixes": [
            ("AI requests fail with 502 'AI backend unavailable'",
             "Ollama is unreachable (desktop asleep or off). Expected; it answers within ~18 s instead of hanging."),
            ("Pods not ready",
             "Check Postgres and Redis first - readiness depends on them. Then the logs below."),
            ("api-migrate stuck",
             "It's an Argo CD PreSync hook recreated on every sync; look at its pod's logs in the backend namespace."),
        ],
        "docs": ["docs/backend.md", "docs/failure-testing.md", "docs/rag.md"],
    },
    "postgres": {
        "how": [
            "Postgres with pgvector, pinned to the Pi on a 5Gi local-path volume.",
            "Dumped nightly with pg_dump into the backup; a restore into a throwaway container was verified.",
        ],
        "fixes": [
            ("Pod not ready / api failing readiness",
             "Logs below; check the Pi's disk space (Pi vitals on the map, or df -h on the Pi)."),
            ("Data lost",
             "The nightly pg_dump is in the restic repository on the Mac (docs/backups.md)."),
        ],
        "docs": ["docs/backups.md", "docs/kubernetes.md"],
    },
    "redis": {
        "how": [
            "The api's job queue and cache, pinned to the Pi.",
            "Its RDB snapshot is in the nightly backup, job queue included.",
        ],
        "fixes": [
            ("Jobs stuck queued",
             "Is the worker pod running (FastAPI page)? Then Redis's logs below."),
        ],
        "docs": ["docs/backups.md", "docs/backend.md"],
    },
    "prometheus": {
        "how": [
            "Scrapes the Pi's node_exporter (which also carries the phone, RustDesk and backup textfiles) and annotated pods.",
            "Alertmanager emails through Gmail; absent() rules catch metrics that vanish, which otherwise look like health.",
            "Everything on this map's numbers and dots comes from here; no data shows grey, never green.",
        ],
        "fixes": [
            ("Boxes grey on the map",
             "A scrape target is down - see 'Scrape targets that are down' below. Host-network pods can answer while the pod network is broken."),
            ("No alert emails",
             "The Alertmanager config is a SOPS Secret: apply it (kubernetes/secrets/apply.sh) before Argo CD creates the pod."),
        ],
        "docs": ["docs/monitoring.md"],
    },
    "apps": {
        "how": [
            "Grafana, Argo CD, the chat assistant and offline Wikipedia (kiwix), each behind Traefik.",
            "Argo CD syncs everything from main automatically, with prune and self-heal; CI only writes image tags to git.",
        ],
        "fixes": [
            ("A change merged but didn't roll out",
             "Argo CD section below: OutOfSync or an error? CI must have committed the new tag first (a 'Deploy ...' commit on main)."),
            ("Every Application DeadlineExceeded",
             "A wedged git fetch in argocd-repo-server: restart it. It's pinned to the Pi."),
            ("'Synced' but a change didn't apply",
             "Argo CD ignores some kinds (Endpoints) by default: apply those by hand."),
            ("Grafana OOMKilled",
             "Happened at 512Mi during restart churn; it has 1Gi now. If it recurs, look at its memory below against the limit."),
        ],
        "docs": ["docs/argocd.md", "docs/cicd.md", "docs/kubernetes.md"],
    },
    "ollama": {
        "how": [
            "GPU inference on the Windows desktop's RTX 3070 Ti, reached from the cluster as the ai/inference Service.",
            "A model holds ~4.7 GB of VRAM and unloads by itself after 5 min idle; the next request loads it again.",
            "Not alerted on purpose: the desktop sleeps every night.",
        ],
        "fixes": [
            ("Need the GPU for a game now",
             "On the desktop: ollama ps, then ollama stop <model>."),
            ("Unreachable with the desktop on",
             "Ollama must listen on 0.0.0.0, not 127.0.0.1 - a stale PowerShell session once started it without OLLAMA_HOST."),
        ],
        "docs": ["docs/gaming-mode.md", "docs/failure-testing.md"],
    },
}
