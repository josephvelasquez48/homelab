# Webcam

Watch the Logitech C922 plugged into the MacBook from a phone or computer,
at home or away. Not on the original roadmap.

Built in three phases, each working on its own before the next:

| Phase | What | Status |
|---|---|---|
| 1 | The camera as a WebRTC stream on the Mac | **Done** 2026-10-02 |
| 2 | `cam.home`: a web app in the cluster with per-person accounts and the viewer page | **Done** 2026-10-02 |
| 3 | WireGuard on the Pi, for watching away from home | Next |

## The web app (phase 2)

`https://cam.home` - FastAPI in the `cam` namespace, pinned to the Pi.
Pages: the live view, sign-in, invite, account (change password), and
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
- **Accounts by invite.** An admin names someone on the People page and
  gets a one-time link (7 days) to send them; they choose their own
  password. Reset password makes the same kind of link for an existing
  account and signs them out everywhere. Turn off and Delete sign them out
  at once.
- **Sessions** are 30-day cookies (HttpOnly, Secure, SameSite=Strict),
  extended while in use; tokens are stored hashed. Passwords are argon2.
  Five failed sign-ins in 15 minutes locks that name or address out.
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

# Look at the pages locally, no database or camera (test logins in the file):
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

## Checking it

```sh
tail -f ~/.config/homelab-cam/mediamtx.log           # one line per viewer, and the helper's mode line
curl -s http://127.0.0.1:9997/v3/paths/list           # is the stream up, who's reading
launchctl kickstart -k gui/$(id -u)/local.homelab.cam # restart
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

## Limits

- **Only while the Mac is on and logged in**, like the backups and m1-node.
- **The MacBook's address isn't reserved on the router** (only its VM's
  is). If its lease changes, the app shows "camera offline" until
  `MEDIAMTX_URL` in `kubernetes/cam/cam.yaml` is updated. Add a DHCP
  reservation for it.
- **Phones must trust the homelab CA** to open `https://cam.home` without a
  warning ([https.md](https.md)) - not done on any phone yet.
- **Video only.** The C922's own microphone doesn't show up as an audio
  device on this Mac; audio would need looking into first.
- **Not yet tried in the dark.** If the camera slows down in low light,
  ffmpeg repeats frames to hold 30 fps, so the stream stays steady but
  smoother motion isn't guaranteed.
