# Phone bridge

Take iPhone calls on the Windows desktop, with its mic and speakers. The Pi
pairs with the phone as a Bluetooth hands-free unit (what a car stereo
does), and a window on the desktop is that unit's mic and speaker. The
call stays on the carrier - no VoIP service or extra number. Not part of
the original roadmap.

What it does:

- Incoming calls ring on the PC in a small pop-up: answer, decline, mute,
  keypad, hang up. Outgoing calls from a dial pad or recent calls.
- Caller names from the iPhone's contacts, and a call history.
- Music and videos from the phone can play on the PC too (a switch).
- Neural noise filtering on the mic, with a status readout.
- Tray icon, hotkeys, a taskbar pin, starts at login, restarts itself
  after a crash or freeze.
- Only while the PC is on: otherwise the phone is left alone.

## How it works

```
iPhone ──Bluetooth──► Pi: PipeWire / WirePlumber, hands-free role (+ A2DP speaker for media)
                          │  org.pipewire.Telephony (D-Bus): answer, dial, hang up
                          ▼
                      phone-bridge: FastAPI, systemd user unit, https://phone.home:8443
                          │  WebSocket: call state + call audio (16 kHz)
                          │  /api/agent/media: music and videos (48 kHz stereo)
                          ▼
                      desktop agent (Python): the Phone window (WebView2), tray,
                      hotkeys, and a separate media-player process
```

| Where | What | Code |
|---|---|---|
| Pi | Call control, call-audio bridge, media stream, contacts, history, reconnects, metrics | `apps/phone/app/` |
| Pi | WirePlumber settings | `apps/phone/wireplumber/51-phone-bridge.conf`, plus `52-phone-media.conf` written by the service |
| Pi | Realtime priority for PipeWire (rtkit) | `apps/phone/pipewire/60-realtime.conf`, `50-rtkit-joe.rules` |
| Desktop | The Phone window, tray, hotkeys, taskbar pin, watchdog | `apps/phone/agent/phone_agent.pyw` and friends |
| Desktop | Media player (its own process) | `apps/phone/agent/media_player.py` |

## Using it

**The Phone window** opens from the taskbar pin, the desktop shortcut or
the tray. From the top:

- **Audio** (folds to a one-line summary; click to open) - call volume
  in dB (-40 to +12, starting at -10), and while the mic is on, which mic. Two switches:
  - *Music and videos on this PC* - off plays them on the iPhone. Calls
    come to the PC either way. The iPhone's volume buttons set the level.
  - *Keep iPhone-answered calls on the iPhone* - calls you pick up on the
    phone stay there.
  - *Noise filter* - Off / Normal / Strong, with a status dot and "removing
    N dB" during a call. Strong also mutes the gaps between words.
- **Status** - whether the iPhone is connected and where call audio is.
- **Dial pad** - hidden until its switch is on.
- **Recent** - the latest 3 calls, *Show all* for up to 30, each with *Call*.

**The call pop-up:** while a call rings, it fills the whole pop-up - caller
and Answer / Decline, nothing else. Once answered, the volume slider
appears underneath.

**During a call:** Mute, Keypad, End. *Move call audio to this PC* and
*Send call audio to iPhone* move the audio either way.

**The Pi's touchscreen:** while a call rings or is up, the Pi's own screen
shows it full screen - Answer, Decline, then Keypad and End. It's a remote
control: the Pi has no speakers or mic, so Answer there sends the call to
the PC, exactly as if you'd answered on the PC. The rest of the time the
Pi's always-on display (docs/dashboard.md) shows underneath. **Home screen**
on the call screen sends it away for the rest of that call: the service
answers `screenHidden` to `phone-screen.service`, which closes it. A new
call brings it back by itself, and the display's **Call** button (bottom
bar, shown during a call) brings it back on demand - through a
`homelab-call://` link whose handler (`apps/pi-display/show-call.sh`) posts
to `/api/agent/screen-show` on loopback with the agent token.

**Tray and hotkeys:** the tray icon is green (connected), amber (on a
call) or grey. Ctrl+Alt+A answers, Ctrl+Alt+H declines or hangs up,
Ctrl+Alt+M mutes. The X hides the window; *Quit* in the tray stops the app.

## Design choices

- **A host service, not K3s.** It needs the Pi's Bluetooth radio and the
  user's PipeWire session, so it's a systemd *user* unit with linger. It
  terminates its own TLS (`phone.home`, from the homelab CA via
  `certificates/issue-phone.py`) - browsers only allow the mic over HTTPS -
  and has its own ufw rule on 8443.
- **No oFono.** WirePlumber 0.5.8 already publishes an oFono-compatible
  call API (`org.pipewire.Telephony`) - found by introspecting the bus.
- **A web view for audio.** The page holds the mic and speakers, so it gets
  the web engine's echo cancellation for free.
- **Call audio stays on the iPhone unless the PC can play it.** The bridge
  sets `RejectSCO` while no window has PC audio on, or while *Keep
  iPhone-answered calls* is on and the PC didn't answer, dial or claim the
  call. The claim is taken *before* the command goes to the phone, or the
  audio link it opens in reply would be refused.
- **Only while the PC is on.** The agent checks in every second. After 2
  minutes without it, the Pi blocks the phone in BlueZ - disconnected, but
  still paired - and unblocks it within 5 s of the PC coming back.
- **The media switch moves one link.** The Pi is always registered as a
  speaker; the switch connects or drops just the phone's A2DP profile. That
  takes a second or two and never touches the calls link.
- **No automatic gain control on the mic.** The Samson has a hardware gain
  knob; the browser's AGC fought it, lifting the room's echo between
  sentences ("sounds like a bathroom"). The knob alone sets the level.
- **Realtime audio thread.** PipeWire's audio thread runs at realtime
  priority 20 through rtkit, so a busy Pi (the display's animation, K3s)
  can't delay call audio. Two things stood in the way: polkit gives rtkit's
  actions only to logged-in desktop sessions, and PipeWire runs in the
  lingering user manager, outside one (`pipewire/50-rtkit-joe.rules` allows
  joe); and PipeWire asks xdg-desktop-portal first, which reports no
  allowance and never reaches rtkit (`pipewire/60-realtime.conf` skips it).
  Check: `ps -eLo cls,rtprio,comm | grep data-loop` has a `RR 20` line
  (PipeWire's; WirePlumber's and pipewire-pulse's stay `TS`).
- **Mic only during calls.** The window opens the mic while a call rings
  (that also wakes the Samson, which sleeps) and closes it 5 s after the
  call ends.
- **The Pi's screen, without a login.** `screen/phone_screen.py` (a user
  service) polls the phone service and opens Chromium kiosk-style on
  `/popup?touch=1` during calls. Chromium maps `phone.home` to 127.0.0.1,
  so it connects over loopback - which the service trusts, and nothing
  else on the network can use - while the certificate still matches.
  Chromium trusts that certificate's key directly (`--ignore-certificate-errors-spki-list`,
  read from the certificate at each launch). The Pi's own screen doesn't
  count as "the PC is on", and can't become an audio page. It needs the
  Pi's desktop logged in, on X11 or Wayland (the Pi runs X11 for RustDesk -
  see [rustdesk.md](rustdesk.md)).
- **Answer on the Pi, audio on the PC.** The Pi publishes a *handoff*; the
  PC's ringing window takes the audio (it announces itself as an audio
  page); only then does the Pi answer. If the PC doesn't take it within 4 s,
  the call is answered anyway, with the audio on the iPhone - at once if no
  PC window is connected at all.
- **Taps respond at once.** Answer, Decline and End dim and say
  "Answering…" etc. the moment they're pressed, until the call's next state
  arrives. The Pi's screen checks for calls every 0.25 s and closes 0.5 s
  after one ends.
- **Timeouts everywhere.** Every telephony D-Bus call has a limit (5 s,
  20 s for answer and dial), and reconnect attempts time out after 30 s.

## Bugs found on real calls

Each made a working call look broken. Found by measuring, not guessing.

| Symptom | Cause | Fix |
|---|---|---|
| Call silent | The phone's call volume (0.019, -34 dB) applied to the stream - in the node's *master* volume, which `wpctl` doesn't touch. `btmon` showed the audio arriving at ~30x the level PipeWire output | Ignore the phone's volume for calls; set both volumes to 1.0 |
| HD voice blamed for the silence | Two "decode failed" lines in a whole call, read as every frame failing | Counted: 400 of 400 packets intact. mSBC (HD) back on |
| Caller heard an echo | WirePlumber linked the call into the Pi's dummy speaker and back | Auto-linking off; the bridge links its own ports |
| Audio sat on the Pi, heard by nobody | The bridge waited for "active", which only comes once something reads the stream | "pending" counts as audio on the Pi |
| Silent recording, no error | `pw-record` with a missing target records the default sink | Link explicitly, fail loudly |
| A call the Pi never showed | The Bluetooth link dropped and came back mid-call; PipeWire doesn't pick up a call in progress | After 8 s of audio with no call, reset the hands-free profile once - the phone re-announces it |
| *Move call audio* missing | Offered only while audio was on the iPhone, not stuck on the Pi | Offered whenever the call isn't reaching a window |
| Phone never reconnected | BlueZ's `Connect` ran over Bluetooth LE, looking for an address an iPhone never shows - 168 failures with the phone in the room | `ConnectProfile` (classic Bluetooth): 2 s |
| Music far too loud | The phone's volume ignored (the call fix above) | Honour it for media only |
| Window froze on a mode switch | A D-Bus call caught by a WirePlumber restart waited ~25 s, stalling updates | D-Bus time limits |
| Phone icon did nothing | Its "already running" check used a fixed port that NVIDIA's service happened to hold | A named mutex |
| Wouldn't reopen after *Quit* | The quit process lingered, holding that mutex | Force the exit |
| Window froze with music playing | Not pinned down; only while media played in the agent's process | Media player moved to its own process; a watchdog restarts a frozen window and logs every thread's stack |
| Noise filter seemed not to work | RNNoise filters one channel; the Samson can be stereo | Mix the mic to mono first |

## Setup

Pi (as joe):

```bash
python3 apps/phone/pair.py      # 2-minute pairing window - pair from the phone
bash apps/phone/install.sh      # venv, WirePlumber config, user unit, linger
```

`install.sh` also installs `phone-screen.service`. It prints the login password and agent token the first time
(kept in `~/.config/phone-bridge/env`). On the iPhone, turn on **Sync
Contacts** for "joe" in Bluetooth settings, or caller names stay empty.

Certificate (desktop, needs the CA key), then copy the pair to
`~/.config/phone-bridge/tls.{crt,key}` on the Pi and delete the local key:

```bash
uv run --no-project --with cryptography python certificates/issue-phone.py
```

Desktop - run in your own PowerShell window (installers run from inside
sandboxed apps can have their AppData writes redirected):

```powershell
powershell -ExecutionPolicy Bypass -File apps\phone\agent\install-agent.ps1 -Token <PHONE_AGENT_TOKEN>
```

That adds the Startup entry and a **Phone** desktop shortcut. To pin it,
open the app and pin its taskbar button.

## Troubleshooting

| Problem | Look at |
|---|---|
| App won't open | `%APPDATA%\phone-bridge\agent.log`; a leftover `pythonw.exe` in Task Manager |
| No call audio | The Audio card's status; `phone_bridge_running` and the peak metrics |
| Phone not connecting | `journalctl --user -u phone-bridge` on the Pi (reconnect lines); is the PC's agent running? |
| Metrics | `/var/lib/node_exporter/textfile/phone_bridge.prom`; alerts in `kubernetes/monitoring/alertmanager.yaml` |

## Limits

- One phone, and only one hands-free unit per phone - the car or earbuds
  compete with the Pi.
- With the PC off (or the app quit) for 2 minutes, calls ring only on the
  iPhone, on purpose.
- Coming back into range takes up to ~30 s to reconnect.
- Media uses SBC (no AAC on Debian's PipeWire) and arrives ~0.2 s after the
  phone plays it; the phone delays video to match.
- HD voice is verified phone-to-Pi by packet capture; Pi-to-phone by ear only.
- Echo cancellation is the browser's; a headset is still the surest way to
  keep speaker audio out of the mic.
