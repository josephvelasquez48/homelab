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
                           Firefox on the desktop: https://phone.home:8443
                               ▲
                           ring agent (desktop, pythonw) - polls "is it ringing?",
                           shows an always-on-top call window (the page, embedded in WebView2)
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
  iPhone's own audio-route button; PipeWire's telephony API has no call
  to release the audio link.

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

It also puts a **Phone** shortcut on the desktop that opens the page in
Firefox.

Firefox, once: trust the homelab root if it doesn't already (Firefox has
its own certificate store - [https.md](https.md)), log in at
`https://phone.home:8443`, and in the site's permissions allow the
microphone (remembered) and autoplay.

## Limits

- Wideband (mSBC, 16 kHz) is verified in the phone-to-Pi direction by
  packet capture and by ear. Pi-to-phone is judged by ear only: these
  Broadcom controllers have no SCO flow control over UART, which is
  where outgoing audio would break (choppy/robotic) if it's going to.
- One phone. If two ever pair, the first gateway on the bus wins.
- Echo: the page asks the browser for echo cancellation, but a headset
  is still the reliable way to keep speaker audio out of the mic.
- If the page closes mid-call, the call's audio stays on the Pi; move it
  back from the iPhone's audio-route button.
- Only one phone can be the iPhone's hands-free unit at a time, so the
  car or earbuds compete with the Pi.
