# Mustard Cam

The webcam is for **Mustard**, a tortoise. The site is branded for him
(2026-10-02): "Mustard Cam" everywhere a person sees it, including the
passkey prompt (`RP_NAME`), a terrarium palette (mustard on moss green,
sand in light mode), a tortoise-shell hexagon background
(`static/scutes-*.svg`, generated tiles), and Mustard himself as an SVG
drawing in `static/common.js` - nodding on the sign-in pages, walking
while the stream connects, napping (with z's) when the camera is offline.
CSS-animated, off under reduced motion, presentation attributes only so
the strict CSP needs no exceptions. The address stayed
`cam.taile847cc.ts.net`: passkeys are bound to it.


Watch the Logitech C922 plugged into the MacBook from a phone or computer,
at home or away. Not on the original roadmap.

Built in three phases, each working on its own before the next:

| Phase | What | Status |
|---|---|---|
| 1 | The camera as a WebRTC stream on the Mac | **Done** 2026-10-02 |
| 2 | `cam.home`: a web app in the cluster with per-person accounts and the viewer page | **Done** 2026-10-02 |
| 3 | Away from home: a public link (Tailscale Funnel) - see "Sharing by link" | **Done** 2026-10-02 |

## The web app (phase 2)

`https://cam.home` - FastAPI in the `cam` namespace, pinned to the Pi.
Pages: the live view, sign-in, invite, account (your passkeys), and
People for admins.

```
browser ──HTTPS──► Traefik ──► cam (Pi): session check, then relays the WebRTC
   │                              offer to MediaMTX with the cam-app login
   │                                         │
   └────────── video, UDP 8189 ◄─────────────┘  Mac (direct, not via the cluster)
```

- **The app is the gate, not the pipe.** It handles accounts and the WebRTC
  handshake (WHEP); the picture goes straight from the Mac to the browser.
  So it costs nothing while people watch, and the stream login never
  reaches a browser.
- **Accounts by invite, signed in with passkeys** (since 2026-10-02; there
  are no passwords). An admin names someone on the People page and gets a
  one-time link (24 hours) to send them; they open it and make a passkey -
  Face ID, fingerprint or device PIN, user verification required. Reset
  access makes the same kind of link for an existing account: using it
  replaces all their passkeys and signs them out everywhere, so a lost
  phone stops working. Turn off and Delete sign them out at once.
- **Passkeys belong to one address**, the public one (`RP_ID` from
  `PUBLIC_URL`), so `cam.home` redirects there (308). A passkey made on
  another site - a phishing copy - is refused by the browser and by the
  server's origin check. The database stores only public keys.
- **Sessions** are 30-day cookies (HttpOnly, Secure, SameSite=Strict),
  extended while in use; tokens are stored hashed. Sign-in failures are
  throttled per address (20 in 15 minutes). There is deliberately no
  per-name limit: the password version had one, which let anyone lock an
  account out by failing in its name.
- **Your passkeys** (Account page): see them, add one on a device that
  doesn't sync with the first (an Android phone alongside an iPhone), or
  remove one - never the last.
- **Viewing log:** every stream start is recorded (who, when, from where)
  and shown on the People page.
- **The live view** shows resolution, frame rate and buffer delay, saves
  snapshots, goes full screen, retries on its own if the Mac drops, and
  stops after 30 s in a background tab so the camera can turn off.
- **Locked down:** a strict Content-Security-Policy (no inline script),
  every state change needs a same-origin header, no-referrer so invite
  tokens never leak.

| What | Where |
|---|---|
| App | `apps/cam/app/` (`main.py` routes, `store.py` Postgres, `static/` pages) |
| Manifests | `kubernetes/cam/cam.yaml`, Argo CD app `kubernetes/argocd/apps/cam.yaml` |
| Database | `cam` database, owned by the `cam` role, in the shared Postgres |
| Secrets | `kubernetes/secrets/cam-secrets.enc.yaml`: `DATABASE_URL`, `MEDIAMTX_PASSWORD` |
| CI | `.github/workflows/ci-cam.yml` (not triggered by `apps/cam/mac/`) |
| Name | `cam.home` in `docker/dns/coredns/home.hosts` and the certificate |

### Running it

```sh
# The first admin - every other invite is made on the People page:
kubectl -n cam exec deploy/cam -- python -m app.cli invite <name> --admin

# Look at the pages locally, no database or camera; /dev/signin/demo signs
# in without a passkey (that route exists only in the dev server):
cd apps/cam && uv run python -m tests.devserver
```

### How it was set up

The `cam` role and database were created by hand in the running Postgres
(`CREATE ROLE cam LOGIN PASSWORD ...; CREATE DATABASE cam OWNER cam;
REVOKE ALL ON DATABASE cam FROM PUBLIC;`), the password going straight into
`cam-secrets.enc.yaml` without being printed. The app creates its tables
on start. Postgres's NetworkPolicy admits `app=cam` pods from the `cam`
namespace.

`MEDIAMTX_PASSWORD` is the Mac's `viewer-password`. If that file is ever
recreated, update the Secret to match, apply it, and restart the
Deployment.

## How it works (phase 1)

```
C922 ──USB──► cam-capture (Swift): picks the 1080p30 mode, raw NV12 frames
                  │ pipe
                  ▼
              ffmpeg: h264_videotoolbox (hardware), 1080p30 baseline, ~4 Mb/s
                  │ RTSP, localhost only
                  ▼
              MediaMTX: WebRTC (WHEP) on :8889, media over UDP :8189
```

All three run on macOS itself, not in m1-node's VM: the VM can't reach USB
devices, and macOS grants camera access only to a process in a user's
session.

- **On demand.** MediaMTX starts the capture when the first viewer
  connects and stops it 15 s after the last one leaves, so the camera (and
  its light) is on only while someone is watching. It costs about a quarter
  of a core for cam-capture and a sixth for ffmpeg while it runs, nothing
  otherwise.
- **Only the cam app can watch.** Reading the stream needs the `cam-app`
  login, accepted only from the two cluster nodes' addresses (pod traffic
  leaves from them) and from the Mac itself. Browsers reach it through the
  web app, which does the WebRTC signaling for them; only the video then
  flows directly between browser and Mac.
- **Publishing is localhost only**, and RTSP listens only on 127.0.0.1.

| Where | What | Code |
|---|---|---|
| Mac | MediaMTX config (a template) | `apps/cam/mac/mediamtx.yml` |
| Mac | The camera helper | `apps/cam/mac/capture.swift` |
| Mac | Installer: builds the helper, renders the config, loads the LaunchAgent | `apps/cam/mac/install.py` |

Installed to `~/.config/homelab-cam/` (config, helper binary, log, and
`viewer-password` - created once, 0600, never committed) and run by the
`local.homelab.cam` LaunchAgent, which restarts it if it exits.

## Installing (on the Mac)

```sh
brew install ffmpeg mediamtx && brew pin mediamtx
cd ~/Desktop/homelab && git pull
/opt/homebrew/bin/python3 apps/cam/mac/install.py
```

Rerun the installer after any change to `apps/cam/mac/`; it replaces
everything except the viewer password. Then, **at the Mac**, the first time:
the first viewer triggers macOS's camera prompt for **mediamtx** - click
Allow (or System Settings > Privacy & Security > Camera). SSH can't answer
it.

**Why `mediamtx` is pinned:** macOS ties the camera permission to the
binary's path, and Homebrew's includes the version
(`/opt/homebrew/Cellar/mediamtx/1.21.1/...`). An upgrade would silently cut
the camera off until someone re-allows it at the Mac. Upgrade on purpose:
`brew unpin mediamtx && brew upgrade mediamtx && brew pin mediamtx`, rerun
the installer, re-allow at the Mac. Rebuilding cam-capture doesn't need
this: the permission goes to the process launchd started, mediamtx.

## Focus

**On the page:** admins get a Far-Near slider and an Auto button under
the video. It changes the picture for everyone watching, so viewers don't
see it. The path is slider -> `POST /api/focus` (admin, same-origin) ->
focusd on the Mac (`apps/cam/mac/focusd.py`, LaunchAgent
`local.homelab.camfocus`, port 8890, MediaMTX's `cam-app` login and only
from the nodes' addresses) -> `camctl set`. A slider setting is remembered
like any other `set`.

`camctl` (`apps/cam/mac/camctl.c`, built by install.py) sets the C922's
focus with standard UVC requests through IOKit - macOS has no API for a
webcam's focus, but the camera takes these; no root needed, and it works
while streaming.

```sh
~/.config/homelab-cam/camctl status     # autofocus on/off, focus, range (0-250, step 5; 0 is far)
~/.config/homelab-cam/camctl set auto   # autofocus on
~/.config/homelab-cam/camctl set 5      # autofocus off, fixed at 5
```

A `set` is remembered (`~/.config/homelab-cam/focus`) and cam-capture
re-applies it 2 s and 6 s after it starts the camera: the C922 keeps a
setting only while it has power, and sent at the very start it was undone
by the camera's own start-up.

**Set to 5 on 2026-10-02**, from a sweep scoring each focus step by the
mean edge strength of its frames (numbers only): 0-10 scored about 1.37,
falling to 0.007 at 250. Autofocus had already been resting near 0, so
the softness people saw was mostly the dark enclosure and objects too
close to the lens, not focus; fixing it stops the autofocus hunting.
Re-run a sweep if the camera moves.

## Checking it

```sh
tail -f ~/.config/homelab-cam/mediamtx.log           # one line per viewer, and the helper's mode line
curl -s http://127.0.0.1:9997/v3/paths/list           # is the stream up, who's reading
launchctl kickstart -k gui/$(id -u)/local.homelab.cam # restart
launchctl kickstart -k gui/$(id -u)/local.homelab.camfocus # restart focusd (log: focusd.log)
```

A healthy start logs
`cam-capture: C922 Pro Stream Webcam: 1920x1080 at 30.0 fps`.

## Problems found and fixed

| Problem | Cause | Fix |
|---|---|---|
| The first capture hung, and a later one published with nobody watching | macOS held ffmpeg at the camera prompt; MediaMTX gave up and asked it to stop, but it was frozen and didn't exit. After Allow it carried on publishing, outside MediaMTX's on-demand control | The prompt answered at the Mac. cam-capture now SIGKILLs ffmpeg when stopped, and ffmpeg no longer opens the camera |
| 5 real frames a second at "1080p30" (324 of 384 frames were duplicates) | ffmpeg's avfoundation input applies the *last* format matching the size, with a frame rate from whichever format matched *first*. On the C922 the last is uncompressed and tops out at 5-10 fps over USB 2; setting 30 fps on it throws, logged as "Configuration of video device failed, falling back to default" | `cam-capture` (Swift) picks a format that really runs at the rate and pipes raw frames to ffmpeg. 30.0 fps measured |
| Every later start also "fell back to default" | The frozen ffmpeg above still held the camera, so the device couldn't be configured | Same fix - nothing is left holding the camera |
| Thousands of "Non-monotonic DTS" warnings; 14 s of video labelled as 0.01 s | The encoder's timestamps came out in the wrong units | ffmpeg stamps frames on arrival and evens them to `-fps_mode cfr -r 30` before encoding |
| A config edit to the login list didn't take effect | MediaMTX's live reload doesn't apply auth changes | Restart (`launchctl kickstart -k`); the installer always does a full restart |

## Sharing by link (Tailscale Funnel)

Phase 3 was first a WireGuard VPN on the Pi. It worked - a phone on
cellular watched through it - but every viewer then needed the WireGuard
app, a device config made over SSH, and the homelab CA installed, which is
too much to ask of someone you just want to show the camera. It was
replaced the same day and removed (the firewall role deletes its old
rules). Now an invite link is all
anyone needs: **People → Invite someone → Copy → send it.** It opens on any
phone, anywhere: they make a passkey and watch.

```
anyone's browser ──HTTPS──► Tailscale's Funnel servers ──► Pi: tailscaled (userspace) ──► cam Service 10.43.187.92:8000
anyone's browser ◄──── video, UDP ──── router (forward UDP 8189) ◄──── Mac
```

- **The page**: `https://cam.taile847cc.ts.net`, published by Tailscale
  Funnel from the Pi, with a Let's Encrypt certificate Tailscale renews.
  Nothing new is open on the router for it. `PUBLIC_URL` points there, so
  invite links do too; `cam.home` keeps working at home.
- **The video** still flows straight from the Mac. MediaMTX asks a STUN
  server (Cloudflare's) for the home connection's public address and offers
  it to viewers; the router forwards UDP 8189 to the Mac.
- **tailscaled runs in userspace networking mode** (`ansible/roles/tailscale`):
  no tun interface, routes, iptables rules or DNS changes on the Pi, which
  is the LAN's only resolver and the cluster's control plane. Funnel needs
  none of that - it proxies from inside tailscaled.
- **The cam Service's ClusterIP is pinned** (10.43.187.92) because Funnel on
  the host proxies to it directly; the host can't resolve cluster DNS.
- **Real visitor addresses reach the app** (Funnel sets X-Forwarded-For,
  which uvicorn trusts), so login throttling is per visitor.
- **It is on the internet.** A scanner requested the page within minutes of
  the certificate being issued - new certificates are public, and bots watch
  for them. What stands in the way: invite-only accounts, passkeys (nothing
  to guess or phish), 24-hour invite links, per-address throttling, a strict
  CSP. The stream itself
  still needs the cam app's server-side login, accepted only from the nodes.

### One-time setup (done 2026-10-02)

1. `ansible-playbook playbooks/site.yml --tags tailscale --limit pi -c local`
   (check mode first) - installs tailscaled in userspace mode.
2. `sudo tailscale up --hostname=cam --accept-dns=false` on the Pi, approved
   in the browser on the Tailscale account.
3. The tailnet policy file got a `nodeAttrs` entry allowing `funnel` for
   `autogroup:member`, and HTTPS certificates were enabled (admin console).
4. `sudo tailscale funnel --bg http://10.43.187.92:8000` - tailscaled keeps
   it across restarts.
5. Router: forward UDP 8189 → the Mac (192.168.1.219 since it moved to Ethernet), and reserve that
   address for the Mac.

```sh
tailscale funnel status                 # on the Pi: is it published?
sudo tailscale funnel --https=443 off   # take the public page down at once
```

## Hardening (2026-10-02)

After the public link, from a review of what an attacker on the internet
could reach:

| Change | Why |
|---|---|
| Passkeys replaced passwords | Viewers' weak or reused passwords were the easiest way in; a passkey can't be guessed, reused or phished |
| Invite links last 24 hours, not 7 days | An unused link is an account for whoever opens it first |
| Throttling per address only | The per-name lockout let anyone lock an account out |
| Tailscale policy: no grants | The tailnet held only the Pi, which needed no tailnet access - Funnel isn't governed by grants. Edited by the account owner (below). Since 2026-10-05 one grant: the owner's devices may reach the phone app on the Pi, port 8443 only |
| UPnP off on the router | Nothing was using it (its table was empty); left on, any device could open ports to the internet |

The tailnet policy (login.tailscale.com/admin/acls/file) keeps the Funnel
permission and grants one thing: the account's own devices (the iPhone) may
reach the phone app on the Pi, tailnet only, on port 8443 (docs/phone.md,
Drop). Nothing else on the Pi is open to the tailnet. A device's traffic
the policy doesn't allow shows in the Pi's tailscaled log as
`Drop: TCP{...} no rules matched`.

```json
{
	"hosts": {
		"pi": "100.77.161.83",
	},
	"grants": [
		// Your own devices (the iPhone) may reach the phone app on the Pi - port 8443 only.
		{"src": ["autogroup:member"], "dst": ["pi"], "ip": ["tcp:8443"]},
	],
	"nodeAttrs": [
		{"target": ["autogroup:member"], "attr": ["funnel"]},
	],
	"ssh": [],
}
```

Still worth doing: two-factor sign-in on the identity provider behind the
Tailscale account (it controls the public page), and keeping MediaMTX - pinned
in Homebrew - upgraded on purpose now and then (see Installing).

## Limits

- **Only while the Mac is on and logged in**, like the backups and m1-node.
- **The camera is on the Mac's Ethernet** (since 2026-10-02): a j5create
  USB adapter (`en9`, `00:05:1b:69:01:62`, 1 Gbit/s), reserved on the
  router at **192.168.1.219**, which `MEDIAMTX_URL` and the UDP 8189
  forward point at. MediaMTX offers `en9` first and the Wi-Fi as a
  fallback. The m1-node VM moved to the same adapter afterwards (see
  [node-migration.md](node-migration.md)), so the Wi-Fi is optional.
- **`cam.home` needs the homelab CA trusted** on a phone to open without a
  warning ([https.md](https.md)). The public link doesn't - its certificate
  is Let's Encrypt's.
- **Video only.** The C922's own microphone doesn't show up as an audio
  device on this Mac; audio would need looking into first.
- **Not yet tried in the dark.** If the camera slows down in low light,
  ffmpeg repeats frames to hold 30 fps, so the stream stays steady but
  smoother motion isn't guaranteed.
