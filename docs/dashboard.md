# Dashboard

A FastAPI app at `dashboard.home`, running in the cluster and pinned to the
Pi, that serves the Pi's always-on display (below). `dashboard.home` on its
own redirects there. Not on the original roadmap.

It used to also be a status page (`/api/status`) with a login and a **Free
the GPU** button. Both were removed on 2026-09-30: the display covers the
status, and Ollama unloads idle models by itself
([gaming-mode.md](gaming-mode.md)).

## Design

- **Pinned to the Pi**, so it stays up when the worker is off - exactly
  when you'd want to check status.
- **Read-only, narrow RBAC:** `get`/`list` on nodes and pods, pod metrics,
  and read access to Argo CD Applications in the `argocd` namespace only.
  For the debug pages (below), pod logs and events, bound namespace by
  namespace to the ones on the map. It uses the pod's own ServiceAccount
  token, no kubeconfig. Nothing it serves changes anything, so there's no
  login; the display's buttons act through link handlers on the Pi itself
  (below), not through the dashboard.

## The Pi's always-on display

`/display` is a full-screen page for the Pi's 1280x720 touchscreen. Tap
to switch between two views:

- **Architecture map:** each box is a real service with a live status
  light; dots move along a line at its real request rate. Red DNS dots
  are queries AdGuard blocked, turned back before the internet. Along the
  bottom: DNS rate, blocked share, pods, API load and the Pi's vitals.
- **Cluster aquarium:** a coral reef seen side-on (`static/tank.js`), one
  fish per pod. Colour and species show the namespace - clownfish for
  backend and kube-system, barracuda for argocd and chat, angelfish for
  monitoring and ai, tang for dashboard and kiwix, a ray along the bottom
  for data - and each fish wanders on its own. Size is memory in use (log
  scale, 8 MiB to 1 GiB) and speed is CPU, both live from metrics-server.
  A pod at 85% of its memory limit puffs up into an orange pufferfish (red
  at 95%) - the warning before an OOM kill; pods with no limit never puff.
  A pod that isn't ready floats belly-up; a pending one sinks; a pod that
  restarts while you watch says so. A fish that needs attention always
  carries a tag saying why; the rest are named a namespace at a time - every
  fish of it at once, 10 s each, highlighted in the legend.
  Each node is a sandcastle flying its own flag, its name written in the
  sand in front, its windows lit while the node is ready; a node that
  isn't goes dark, its flag hanging grey, and its name in the sand turns
  red and says "not ready".
  It's 2.5D: layers at different depths (far reef, back weed, the sand
  with its coral and castles, the fish, weed against the glass) slide at
  different speeds as the view drifts, so it reads as deep without 3D.
  The far reef and the sand are drawn once per weather change and copied
  in each frame, which keeps it at the frame-rate cap on the Pi.
  The tank shows the real weather: a strip of sky above the water with
  the sun or moon (placed by the day's real sunrise and sunset, with dawn
  and dusk colours) - the moon in its real phase, worked out from the
  date (within about a day), lit from the right while waxing and the left
  while waning; `&moon=0.25` (0 new, 0.5 full) previews one - stars on
  clear nights, clouds from the actual cloud
  cover drifting with the wind, rain rippling the surface (heavier with
  the real rainfall), snow, fog, and lightning in a storm. Add
  `?wx=rain&phase=night` (clear, partly, overcast, fog, drizzle, rain,
  snow, storm; dawn, day, dusk, night) to the address to preview any of
  them.

Tap a box or a fish for its debug page (below). The top bar has the time, weather (if
`WEATHER_LAT`/`WEATHER_LON` are set), overall health and the iPhone; the
bottom ticker cycles alerts, down nodes, Argo CD, backups and AdGuard.

**Data:** one `GET /api/display` every 5 s (`app/display.py`):

| Shown | From |
|---|---|
| In-cluster boxes, fish | Pod readiness from the Kubernetes API |
| Fish size, speed, puffing | Pod metrics (metrics-server) and each pod's memory limit; the dashboard's ClusterRole can read `metrics.k8s.io` pods |
| CoreDNS, RustDesk | A DNS query / TCP connect to the Pi's own address - works because this pod is pinned to the Pi (same-node traffic isn't filtered by ufw) |
| AdGuard, phone, Ollama, backups | Prometheus gauges; the phone counts as down when its gauges are 2 minutes stale |
| Dots | Prometheus rates: AdGuard queries, Traefik per service, the api's own routes. No traffic, no dots |

No data shows as grey, never green.

**On the Pi:** `apps/pi-display/install.sh` adds the homelab CA to
Chromium's certificate store (name-constrained to `.home`) and installs
`pi-display.service`, a user unit that keeps Chromium in kiosk mode on
the page. An autostart entry starts it at desktop login. A call's screen
(`apps/phone/screen`) opens on top and closes back to it.

**Getting to the desktop:** the **Desktop** button in the bottom bar asks
first, then closes the display so the Pi's desktop shows; **Homelab
display** on the desktop (or in the menu) brings it back. A page can't stop
its own service, so the button follows a `homelab-desktop://` link that
Chromium hands to xdg-open, and `install.sh` makes
`apps/pi-display/show-desktop.sh` its handler. `install.sh` also
pre-approves that link for `https://dashboard.home` in the display's
Chromium profile, so there is no "open this application?" prompt. Without a
keyboard or the button, `systemctl --user stop pi-display.service` over SSH
does the same.

**Back to a call:** during a call the bottom bar also has a **Call** button.
The Pi's call screen covers this page, but its **Home screen** button sends
it away for the rest of the call; **Call** brings it back. The same trick as
Desktop: a `homelab-call://` link, pre-approved by `install.sh`, whose
handler `apps/pi-display/show-call.sh` asks the phone service on loopback
(docs/phone.md).

**The Pi's Bluetooth:** the **Bluetooth on** button in the bottom bar turns
the Pi's Bluetooth off (it asks first: the iPhone disconnects, so no calls
or music through the Pi, and a Bluetooth mouse stops too); it then reads
**Bluetooth off**, and a tap turns it straight back on. Same trick again: a
`homelab-bluetooth://on` or `://off` link, pre-approved by `install.sh`,
whose handler `apps/pi-display/set-bluetooth.sh` runs `bluetoothctl power`
(BlueZ lets any local user power the adapter, so no sudo). Off lasts until
it's turned back on or the Pi restarts. The phone page has the same control
(*Pi's Bluetooth*, docs/phone.md), and the two follow each other: both show
the adapter itself, as the phone service reads it every 5 s. The phone page
gets it pushed within those 5 s; the display reads it from
`phone_bluetooth_powered`, written straight after the phone page's switch
moves, so it follows within about 20 s (scrape plus poll). After a tap on
the display, the button shows the new state at once, until the gauge agrees
or a minute passes. It's hidden while there's no data. While Bluetooth is
off, the phone service doesn't try to reconnect.

**Deploys reach the screen on their own:** the dashboard sends
`Cache-Control: no-cache` for its pages and `/static` files, so a browser
always rechecks them (an unchanged file is a cheap 304). Without it,
Chromium chose its own freshness from `Last-Modified` and kept running the
old page for hours after a deploy, even across restarts. `/api/display`
also carries `page_version`, a hash of the display's files; a display
still on an older page reloads itself on its next poll.

**Cost:** animation is capped at 16 fps, and the aquarium drops to 12 at
night (with no light rays). At 60 the Pi's Chromium used about 1.5 cores;
at 24 the map alone measured about half a core (49%, 2026-10-01). Dots and
fish move per second, not per frame, so a lower rate only makes their steps
bigger. The aquarium's bubbles were removed the same day: decoration that
meant nothing.

## Debug pages

Tapping a box on the map opens `/component/<box>`, and tapping a fish opens
`/component/pod/<namespace>/<pod>` (`app/component.py`,
`static/component.js`). They also work from any browser at
`https://dashboard.home/component/api`. Each page has:

- **Connections**, drawn: what sends to this box (left) and what it sends
  to (right). Each line is labelled with what flows along it and the port,
  and drawn the way the map has it right now: green while traffic flows,
  grey dashes while quiet, red when an end is down. Tap a neighbour to open
  its page.
- **Numbers** from Prometheus, coloured when there's a clear good or bad.
- **For anything in the cluster:**
  - each pod's state, restarts and why it last ended (OOMKilled, exit code);
  - CPU, and memory against the limit;
  - Kubernetes events (they're kept for an hour);
  - the last 150 log lines of each container, plus the log from before the
    last restart, which is usually where a crash says why.
- **For anything outside it** (the Pi's Docker and systemd services, the
  Mac, the desktop): the commands that show its logs. The dashboard can't
  read those itself. Giving it a way in (the Docker socket, the host's
  journal) would hand a page with no login far more than it needs.

**What it can read:** logs and events only in the namespaces on the map
(`dashboard-debug` RoleBindings in `kubernetes/dashboard/dashboard.yaml`).
That excludes `cam`, whose logs carry viewers' addresses and invite links.
Anything credential-shaped (tokens, passwords, `user:pass@` URLs, invite
paths) is masked before it leaves the dashboard, in case a library logs one.

**On the Pi:** the page refreshes every 15 s and costs the Pi nothing in
between (nothing on it animates). Opened from the display, it goes back to
the map after 3 minutes untouched, so the screen never stays on a log.

## Worth knowing from the SSH era

Still true of Windows OpenSSH, and each cost real time:

- **Admin accounts ignore `~/.ssh/authorized_keys`**; Windows reads
  `C:\ProgramData\ssh\administrators_authorized_keys` (SYSTEM and
  Administrators only). Symptom: `Permission denied (publickey)` with a
  correct key.
- **A firewall rule scoped to the Private profile does nothing** when the
  active network is categorised Public. Check `Get-NetConnectionProfile`.
- **A new ghcr.io package is private by default**, so K3s's anonymous
  pull fails with 401 until it's made public.

## Known gaps

- The display and `/api/display` (cluster layout, pod names, node metrics)
  are open to anything on the LAN, read-only.
