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
  It uses the pod's own ServiceAccount token, no kubeconfig. Nothing on it
  changes anything, so there's no login.

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
  in each frame, which keeps it at the 24 fps cap on the Pi.
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

Tap a box or a fish for details. The top bar has the time, weather (if
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

**Deploys reach the screen on their own:** the dashboard sends
`Cache-Control: no-cache` for its pages and `/static` files, so a browser
always rechecks them (an unchanged file is a cheap 304). Without it,
Chromium chose its own freshness from `Last-Modified` and kept running the
old page for hours after a deploy, even across restarts. `/api/display`
also carries `page_version`, a hash of the display's files; a display
still on an older page reloads itself on its next poll.

**Cost:** animation is capped at 24 fps. At 60 the Pi's Chromium used
about 1.5 cores; at 24 it's about a quarter of one.

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
