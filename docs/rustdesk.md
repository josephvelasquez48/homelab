# Remote desktop (RustDesk)

Remote desktop between the Windows PC, the laptop and the Pi, through a
RustDesk server on the Pi instead of RustDesk's public ones.

## What runs

| Piece | Where | Ports (LAN only) |
|---|---|---|
| `hbbs` - ID/rendezvous server | `docker/rustdesk`, host network | 21115 tcp, 21116 tcp+udp |
| `hbbr` - relay | same | 21117 tcp |
| RustDesk client, as a service | the Pi (`rustdesk.service`, arm64 `.deb`) | - |
| Traffic counter, for the display's map | the Pi (`rustdesk-traffic.service`, `apps/rustdesk-traffic`) | - |

The map on the Pi's display ([dashboard.md](dashboard.md)) shows RustDesk's
traffic on the Home network - RustDesk line: the Pi's screen streaming out
to a viewer, input coming in. `apps/rustdesk-traffic` sums the kernel's
per-connection byte counters (`ss -ti`) for the client, `hbbs` and `hbbr`
every 10 s - connections to other machines only - and writes them for
Prometheus like the phone bridge. Install or update it with
`bash apps/rustdesk-traffic/install.sh` on the Pi.

Name: `rustdesk.home` (CoreDNS `home.hosts`). Start or update the server:

```bash
cd ~/apps/homelab/docker/rustdesk && docker compose up -d
```

## Why it's safe

- **LAN only.** ufw allows 21115-21117 from `192.168.1.0/24` only
  (`ansible/roles/firewall`), so a router port-forward couldn't expose it.
  The web-client ports 21118/21119 stay closed.
- **Key required.** Both halves run with `-k _`: a client without the
  server's public key is refused.
- **No third party.** IDs and relayed traffic stay on the Pi; sessions are
  end-to-end encrypted between clients.
- **The private key** is in `docker/rustdesk/data/` (mode 700, git-ignored)
  and in the Pi snapshot (`backup/pi-snapshot.py`). Losing it means a new
  key on every client.

## Client setup

On each device, **Settings > Network > ID/Relay server**:

| Field | Value |
|---|---|
| ID server | `rustdesk.home` |
| Relay server | `rustdesk.home` |
| Key | `docker/rustdesk/data/id_ed25519.pub` on the Pi |

The main window should then say **Ready**. In **Settings > Security**: a
strong permanent password, and **Enable direct IP access** off.

## The Mac

The MacBook (`192.168.1.180`, also the host of m1-node) runs the client too,
ID `359301729`. Set up over SSH on 2026-10-01: `brew install --cask
rustdesk`, and the three server options above written into
`~/Library/Preferences/com.carriez.RustDesk/RustDesk2.toml` before its first
launch. What SSH can't do, because macOS only allows it at the Mac: Screen &
System Audio Recording and Accessibility permissions (System Settings >
Privacy & Security), the permanent password, and starting at login.

## The Pi's desktop

The Pi's client has a permanent password (`~/.config/rustdesk-password` on
the Pi). Connect to its ID from the PC. Desktop autologin is off, so after
a reboot you land on the login screen and sign in there.

It needs the desktop on **X11** (openbox): on Wayland (labwc) RustDesk can't
capture the screen or send input without someone approving it on the Pi.

```bash
sudo raspi-config nonint do_wayland W1   # X11; W2 = back to labwc. Reboot after.
```

The touchscreen call screen works on either. X11 ignores labwc's rotation
(kanshi `transform 270`), so it's set separately:

- display: `xrandr --output DSI-2 --rotate right` in `/usr/share/dispsetup.sh`
- touch: `/etc/X11/xorg.conf.d/40-touch-rotate.conf`, TransformationMatrix
  `0 1 0 -1 0 1 0 0 1`
- no screen blanking: X11 blanks after 10 minutes by default, labwc here
  didn't. Off with `sudo raspi-config nonint do_blanking 1`
  (`/etc/X11/xorg.conf.d/10-blanking.conf`)

## Limits

Only on the home network. From outside, the way in is a VPN into the LAN
(e.g. WireGuard on the Pi), not open ports.
