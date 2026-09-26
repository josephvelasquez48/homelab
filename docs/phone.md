# Phone bridge

Not part of the original roadmap. Take iPhone calls on the Windows
desktop: the Pi pairs with the phone as a Bluetooth hands-free unit (the
role a car stereo plays), and a browser page on the desktop is that
unit's microphone and speaker. No VoIP provider or phone number is
involved - the call stays on the carrier, the phone just hands its audio
to the Pi.

```
iPhone ──Bluetooth HFP──► Pi: PipeWire/WirePlumber (hands-free role)
                               │  org.pipewire.Telephony (session D-Bus): answer/dial/hang up
                               │  bluez_input/bluez_output streams: call audio
                               ▼
                           phone-bridge (FastAPI, systemd user unit, :8443)
                               │  WebSocket: call state as JSON, audio as 16 kHz s16 frames
                               ▼
                           desktop agent's window (the page, embedded in WebView2):
                           the full app from the tray / desktop shortcut, or the
                           always-on-top popup when a call comes in
                               ▲
                           desktop agent (pythonw) - polls the Pi about calls, tray, hotkeys
```

## Why a host service, not K3s

Everything else of this shape runs in the cluster. This can't, for the
same reason CoreDNS doesn't: it needs the Pi's Bluetooth radio and the
logged-in user's PipeWire session and session bus. A pod could only get
those by being handed the host, at which point it is a host service with
extra steps. So it's a systemd *user* unit, with linger enabled so the
user session exists without anyone logged in.

That also puts it outside Traefik, so it terminates its own TLS
(`phone.home` only, issued from the homelab CA by
`certificates/issue-phone.py`, separate from the five-name cluster
certificate so re-issuing one never touches the other) and has its own
ufw rule, which - unlike Traefik's ports - ufw does enforce, because no
kube-router chain sits in front of a host process
([kubernetes.md](kubernetes.md)).

HTTPS isn't optional here: browsers only allow `getUserMedia` (the mic) in
a secure context.

## No oFono

The usual Linux recipe for a hands-free unit is BlueZ + oFono.
WirePlumber 0.5.8 on this Pi already publishes `org.pipewire.Telephony`
on the session bus with an oFono-compatible `VoiceCallManager`
(`Dial`, `GetCalls`, `HangupAll`, `SendTones`) and per-call
`org.ofono.VoiceCall` (`Answer`, `Hangup`). Found by introspecting the
live bus once the phone was paired, not from documentation. oFono was
never installed.

## Bugs found on real calls

Each of these made a working call look broken, and each is now pinned in
`apps/phone/wireplumber/51-phone-bridge.conf` or the bridge code.

1. **A wrong diagnosis: wideband voice blamed for a silent call.** The
   first call negotiated mSBC (16 kHz) and measured as near-silence
   (peaks 4-28 of 32767). The journal had `spa.bluez5.source.sco: decode
   failed: -3` and the kernel `Unexpected continuation frame`, and that
   was read as "the Pi 5's UART Bluetooth mangles every mSBC frame". mSBC
   was forced off in favour of CVSD (8 kHz). But the decode error
   appeared twice in the whole call, not per frame; the silence was the
   stream volume (bug 3), found later. Calls on CVSD sounded muffled both
   ways next to an HD cell call. Re-tested with mSBC on after the volume
   fix: 400 of 400 SCO packets in a 3 s capture were intact mSBC frames
   (H2 header in sequence + 0xAD sync, exactly 60 bytes apart), zero
   decode failures in 25 minutes, and it sounded clearer. mSBC is back
   on, explicitly, in `51-phone-bridge.conf`.

   The lesson is the same one as bug 3: count before concluding. "Decode
   failed" in the log was true, and irrelevant.

2. **The caller heard themselves.** WirePlumber's default policy linked
   the call's incoming stream into the Pi's only sink (Dummy Output) and
   that sink's monitor back into the outgoing stream. The person on the
   other end reported a feedback loop. Fixed by `node.autoconnect = false`
   on the hands-free streams; the bridge links them itself with
   `pw-link`, port by port.

3. **A live call measured as silence.** With the codec fixed, the
   incoming stream still peaked at 5-16 out of 32767 while the other
   person was talking. Raw SCO packets captured with `btmon` on the same
   call peaked at 427 - the audio was arriving and being turned down. The
   stream's volume was 0.019 (about -34 dB), pushed by the phone's call
   volume over HFP and restored by WirePlumber. Fixed with
   `bluez5.enable-hw-volume = false`, and the bridge sets both streams to
   1.0 when a call starts. Volume is the page's slider now.
   The first version of that fix didn't work, and a later call showed
   why: the node has two volumes. `wpctl set-volume` sets
   `channelVolumes` (it read back 1.00), but the 0.019 was the node's
   master `volume` prop, untouched. The bridge now sets both
   (`pw-cli set-param <id> Props '{ volume: 1.0 }'`); PipeWire's output
   then matched raw `btmon` packet levels on the same call (~1000 peak
   both).

4. **A missing target records silence without error.** `pw-record
   --target X` with X absent falls back to the default sink's monitor and
   happily records nothing. The bridge starts both `pw-cat`s with
   `--target 0` and fails loudly (shown on the page) if the phone's
   streams or ports aren't there.

5. **A call's audio sat on the Pi and went nowhere.** The bridge only
   started once the transport read `active`, but with autoconnect off
   (bug 2) nothing consumes the phone's streams until the bridge does,
   so the transport stayed `pending` forever - while `btmon` showed ~260
   SCO packets/s arriving. Neither side of the call heard anything, and
   pressing "Move call audio to this PC" again returned
   `org.pipewire.Telephony.Error.InvalidState`. `pending` now counts as
   the audio being on the Pi.

6. **A call the Pi never heard about.** The hands-free link dropped at
   15:08 and came back while a call was up. The phone reopened the audio
   link to the Pi (transport `pending`, both streams present), but
   `GetCalls` stayed empty, so the window had nothing to show or answer
   and the caller's audio went to a Pi with no page taking it. PipeWire
   doesn't pick up a call already in progress when the link comes back.
   `Device1.DisconnectProfile` then `ConnectProfile` for just the
   hands-free UUID (`0000111f-…`) made the phone announce it: `call1`,
   `active`, within 2 s, and the call carried on. The hub now does that
   itself after 8 s of audio on the Pi with no call - once per stretch,
   because an app call (FaceTime, WhatsApp) can also put audio here
   without an HFP call and mustn't reset in a loop.

7. **"Move call audio to this PC" hidden for audio stuck on the Pi.** The button was
   offered only while the audio was on the iPhone. With the audio on the
   Pi and no page bridging it (bug 6), there was no way to take it. It's
   offered now whenever the call isn't bridged to a page, and
   `audio-to-pc` skips `Activate` when the audio is already on the Pi.
   The same call turned up a second case: a window closed mid-call left
   the bridge running into nobody, still reporting "bridged", so the
   reopened window hid the button again. The bridge now stops when its
   last audio page leaves.

Checked and ruled out on the way: the Broadcom controller's SCO routing
(`hcitool cmd 0x3f 0x1d` reads back routing `01`, over HCI, not the PCM
pins), and whether SCO data reached the host at all (~260 packets/s each
way, as expected for a call).

## Keeping calls on the phone when nobody's listening

iOS sends call audio to a connected hands-free unit by default, like a
car. With no page open, that would move a call answered on the handset
to a Pi with no speakers. The bridge sets the transport's `RejectSCO`
property whenever no page has turned PC audio on, so the audio stays on
the iPhone until a browser can actually play it. A page that's merely
open doesn't count; it has to have clicked "Enable PC mic & speakers"
(or answered).

### "Calls I answer on the iPhone stay on the iPhone"

A switch on the page, stored on the Pi (`~/.config/phone-bridge/settings.json`)
so it holds for every browser and across restarts. With it on,
`RejectSCO` stays set even while a page has audio on, except for a call
the PC claimed: answered, dialed, or moved with "Move call audio to this
PC". The claim is taken, and `RejectSCO` lifted, *before* the command
goes to the phone - otherwise the audio link the phone opens in response
would be refused. It ends when the calls it covered are over, and a dial
that fails outright drops it, so the next call answered on the handset
stays there.

## The ring agent

`apps/phone/agent/phone_agent.pyw`, on the desktop, in its own venv with
pywebview. Polls `/api/agent/ringing` once a second with a bearer token
(`PHONE_AGENT_TOKEN`). On a new ringing call, if no page has PC audio on,
it plays the ringtone and shows a small always-on-top window, bottom
right: the page's compact `/popup?agent=1` view embedded in WebView2
(Windows' built-in web engine - no browser window opens). The whole call
happens there: Answer/Decline, then Mute, Keypad and Hang up. The page
holds the mic and speakers, so it gets the web engine's echo
cancellation, which is why this embeds a web view rather than doing
audio natively.

- **No login step.** If the embedded page comes up on the login form,
  the agent signs it in: a same-origin `fetch` to `/api/agent/session`
  with the token, run inside the page via `evaluate_js`, then a reload.
  The token never goes in a URL, and the cookie lands in the agent's
  own WebView2 profile (`%LOCALAPPDATA%\phone-bridge\webview`). That
  makes the token equivalent to the password; it lives only in the
  desktop user's `%APPDATA%`.
- **Mic permission.** pywebview 6.2.1 doesn't handle WebView2's
  `PermissionRequested`, so the agent does: microphone for
  `phone.home`, deny everything else.
- **Every call, not just incoming.** The window shows for a ringing
  call, one answered on the iPhone, and one dialed from it, so "Move call
  audio to this PC" is always one click away. It hides when the call
  ends, and is blanked while hidden so it doesn't count as an open audio
  page. Closing it hides it for that call - unless this window is the
  call's mic and speakers, since hiding it then would leave the call
  silent with no way to hang up. Moving audio *back* to the phone is the
  iPhone's audio-route button or *Send call audio to iPhone* (see
  Extras).
- **Starts at login, restarts after a crash.** The Startup-folder entry
  runs a small supervisor that runs the real agent (`--child`) and
  restarts it when it dies, after a short, growing pause. WebView2 and
  pythonnet are native code and can take the process down in ways Python
  can't catch; the agent is what makes calls reach the PC. A clean exit
  (tray *Quit*) ends it; so do 5 crashes in 10 minutes, rather than
  looping. Both write to `%APPDATA%\phone-bridge\agent.log`, including
  native crashes (`faulthandler`) and uncaught thread errors - `pythonw`
  has no console, so these used to vanish.
- **Pins to the taskbar as Phone** (`agent/taskbar.py`). Windows pins
  by AppUserModelID; without one the window belonged to `pythonw.exe`,
  and pinning it pinned Python. The agent sets its own ID
  (`Homelab.PhoneBridge`) on the process, and on its window together
  with a relaunch command (`pythonw phone_agent.pyw --show`), name and
  icon, so the pinned button starts the agent and opens the app.
- **One copy at a time.** A named mutex (`Local\phone-bridge-agent`)
  says whether an agent is running; the desktop shortcut's copy then
  asks it to open its window, over 127.0.0.1 on a port the running agent
  publishes in `%APPDATA%\phone-bridge\show-port`, and exits. This
  used to be one fixed port (51871) for both jobs. It is in Windows'
  ephemeral range, and one afternoon NVIDIA's `nvcontainer` was handed
  it for an outgoing connection: every start took the busy port for a
  running agent and exited cleanly - which the supervisor treats as
  *Quit* - so the shortcut did nothing, with nothing in the log.
- **Quit ends the process.** Once, after *Quit*, the window closed but
  the process never exited - left waiting on its threads or pythonnet's
  shutdown - and a copy that's still alive holds the mutex: every later
  start, from the shortcut or the pin, handed over to it and exited, so
  the app wouldn't open. The agent now calls `os._exit` as soon as the
  window loop returns, and 5 s after *Quit* if it never does.

## Extras

Added together after the calls themselves were solid. Each is optional
in the hub (a test Hub has none of them) and none can hold up call
state: the slow ones run in their own loop.

- **Caller names** (`app/contacts.py`): the iPhone's phonebook over
  Bluetooth PBAP, via BlueZ's OBEX daemon (`bluez-obexd`). Pulled on
  connect and every 6 h into `~/.config/phone-bridge/contacts.json`,
  names and numbers only, matched on the last ten digits. iOS returns
  an empty phonebook until **Sync Contacts** is on for "joe" in its
  Bluetooth settings - found by pulling it: the session opened fine and
  reported size 0.
- **Call history** (`app/history.py`): SQLite at
  `~/.local/share/phone-bridge/calls.db`, not the cluster's Postgres -
  this runs on the host, and Postgres admits only the backend
  namespace. Missed = incoming and never active. The page lists recent
  calls with call-back buttons.
- **Send call audio to iPhone**: PipeWire's API can pull audio onto the
  Pi but not release it, so `release-sco.sh` sends an HCI Disconnect for
  the (e)SCO link. It needs root: installed root-owned as
  `/usr/local/sbin/phone-bridge-release-sco`, with a sudoers rule for
  exactly that path. `RejectSCO` stays on until the call ends or the PC
  asks for the audio back.
- **Auto-reconnect** (`app/reconnect.py`): while no phone is connected,
  `Device1.ConnectProfile` for the hands-free gateway on each paired,
  trusted device offering it, every 30 s. It used to be
  `Device1.Connect`, and the phone never came back on its own: BlueZ ran
  that over Bluetooth LE for the dual-mode iPhone, scanning 30 s for a
  public address the iPhone never advertises. `btmon` showed no classic
  page in 168 attempts with the phone in the room; `ConnectProfile`,
  classic-only, connected in 2 s. A connect that hangs is
  given up after 30 s and followed by `Disconnect`, or BlueZ answers
  every later attempt with `InProgress` without paging the phone. The
  same module resets the hands-free profile for bug 6.
- **Only while the PC is on**: with the desktop off, the Pi is a
  hands-free unit with no speaker or mic, and the iPhone still connected
  to it - it could send a call's audio there. The agent polls every
  second while it runs, so when nothing on the PC has checked in for
  2 minutes (`PC_GONE_SECONDS`; an open page counts too) the reconnector
  sets the phone's BlueZ `Blocked` property. That disconnects it and
  refuses its own connection attempts, but keeps the pairing - checked
  live: still paired, bonded and trusted, and no reconnect in 20 s. When
  the agent polls again the phone is unblocked and connected on the next
  5 s check: 12 s after a service restart, in the test. Two minutes is
  room for the agent's crash restart or a quick reboot without dropping
  the phone. `phone_pc_present` in the metrics shows which state it's in.
- **Mic noise filter**: RNNoise, a small neural-network noise
  suppressor, runs on the mic in an AudioWorklet, between the mic and
  the capture that resamples to 16 kHz. Vendored as three static files
  from `@sapphi-red/web-noise-suppressor` 0.4.1 (MIT,
  `static/rnnoise-LICENSE.txt`), the SIMD build where WebAssembly SIMD
  is available. It needs 48 kHz, so the page's AudioContext runs at
  48 kHz. The browser's own `noiseSuppression` stays on under it; alone
  it did little against a desk fan. In WebView2 it cut synthetic fan
  noise (brown noise plus a 120 Hz hum) by 52 dB, and the other end of
  a live call confirmed the fan was gone. Always on - there's no
  switch. If it can't load, the call goes ahead unfiltered.
- **Metrics and alerts**: `phone_bridge.prom` in node_exporter's
  textfile directory, the same route as the backup metrics, so no new
  scrape target. Rules in `kubernetes/monitoring/alertmanager.yaml`:
  bridge down or silent, and the two silent-call cases (caller silent,
  PC mic silent) that each took a live call to find. No alert for the
  phone being away - it leaves the house with its owner.
- **The whole app in the agent's window**: the tray's *Open phone* and
  the desktop shortcut open the full page (dial pad, recent calls, mic,
  settings) in the agent's WebView2 window; it stays until closed, and a
  call brings it forward. The compact popup is still what appears for a
  call when it isn't open. Opening it doesn't take calls' audio: the
  window only announces audio once Answer, a dial or "Move call audio"
  is used there, so a call answered on the iPhone stays on the iPhone.
- **Tray icon and hotkeys** (`agent/tray.py`, `agent/hotkeys.py`): the
  icon shows connected / on a call / not connected, opens the page,
  pauses popups, and raises a notification for missed calls. Ctrl+Alt+A answers (or moves audio to the PC), Ctrl+Alt+H
  declines or hangs up, Ctrl+Alt+M mutes - by clicking the popup's own
  buttons. A hotkey isn't a user gesture to the page, so the agent adds
  `--autoplay-policy=no-user-gesture-required` to its own WebView2 only.

## Setup

Pi (as joe):

```bash
python3 apps/phone/pair.py      # opens a 2-minute pairing window; pair from the phone
bash apps/phone/install.sh      # venv, WirePlumber config, user unit, linger
```

`install.sh` prints the login password and agent token the first time;
both live in `~/.config/phone-bridge/env` (mode 600). It restarts
WirePlumber only when its config changed - that drops a call's audio if
one is on the Pi.

Certificate (desktop, needs the CA key):

```bash
uv run --no-project --with cryptography python certificates/issue-phone.py
```

then copy the pair to `~/.config/phone-bridge/tls.{crt,key}` on the Pi
and delete the local key.

Desktop:

```powershell
powershell -ExecutionPolicy Bypass -File apps\phone\agent\install-agent.ps1 -Token <PHONE_AGENT_TOKEN>
```

It also puts a **Phone** shortcut on the desktop that opens the agent's
own window as the full app (`phone_agent.pyw --show`) - no browser.

No browser setup is needed: the agent's window signs itself in and is
granted the microphone by the agent. (The page still works in a browser
at `https://phone.home:8443` - that needs the homelab root trusted, a
login, and the site's mic and autoplay allowed.)

## Limits

- Wideband (mSBC, 16 kHz) is verified in the phone-to-Pi direction by
  packet capture and by ear. Pi-to-phone is judged by ear only: these
  Broadcom controllers have no SCO flow control over UART, which is
  where outgoing audio would break (choppy/robotic) if it's going to.
- One phone. If two ever pair, the first gateway on the bus wins.
- Echo: the page asks the browser for echo cancellation, but a headset
  is still the reliable way to keep speaker audio out of the mic.
- If the page closes mid-call, the call's audio stays on the Pi. The
  bridge stops, and a reopened window offers *Move call audio to this PC* for it;
  otherwise move it back from the iPhone's audio-route button.
- The reset for bug 6 cuts the call's audio for a few seconds; the call
  itself stays up on the phone.
- Only one phone can be the iPhone's hands-free unit at a time, so the
  car or earbuds compete with the Pi.
- With the PC off (or the agent quit) for 2 minutes, calls ring only on
  the iPhone: the Pi keeps it disconnected on purpose. The earbuds or the
  car get it to themselves.
- Coming back into range, the phone reconnects on the next 30 s check,
  not instantly. Measured: 26 s after a forced disconnect.
