# RustDesk server

Remote desktop between the Windows desktop and the laptop, through a
RustDesk server on the Pi instead of RustDesk's public ones.

## What runs

| Piece | Where | Ports (LAN only) |
|---|---|---|
| `hbbs` - ID/rendezvous server | `docker/rustdesk`, host network | 21115 tcp, 21116 tcp+udp |
| `hbbr` - relay | same | 21117 tcp |

Name: `rustdesk.home` (CoreDNS `home.hosts`).

Start or update it on the Pi:

```bash
cd ~/apps/homelab/docker/rustdesk && docker compose up -d
```

## Why it's safe

- **LAN only.** ufw allows 21115-21117 from `192.168.1.0/24` only
  (`ansible/roles/firewall`), so it isn't reachable from the internet even
  if a router port were forwarded. The web-client ports 21118/21119 stay
  closed.
- **Key required.** Both halves run with `-k _`: a client without this
  server's public key is refused, so knowing the address isn't enough.
- **No third party.** IDs and relayed traffic stay on the Pi. Sessions are
  end-to-end encrypted between the two clients either way.
- **The private key** is in `docker/rustdesk/data/` (mode 700, git-ignored)
  and is included in the Pi snapshot (`backup/pi-snapshot.py`). Losing it
  means entering a new key on every client.

## Client setup

On each device: RustDesk, **Settings > Network > ID/Relay server**:

| Field | Value |
|---|---|
| ID server | `rustdesk.home` |
| Relay server | `rustdesk.home` |
| Key | contents of `docker/rustdesk/data/id_ed25519.pub` on the Pi |

The bottom of the main window should then say **Ready**.

Recommended in **Settings > Security**: a strong permanent password (or
"accept sessions via click" only), and **Enable direct IP access** off.

## Limits

Away from home the laptop can't reach `rustdesk.home`. Reaching it from
outside needs a VPN into the LAN (e.g. WireGuard on the Pi), not open ports.
