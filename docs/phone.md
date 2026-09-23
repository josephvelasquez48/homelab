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
                           shows an always-on-top Answer/Decline popup
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

1. **Wideband voice decoded to silence.** The phone negotiated mSBC
   (16 kHz). PipeWire logged `spa.bluez5.source.sco: decode failed: -3`
   for every frame and the kernel logged `Unexpected continuation frame`:
   the Pi 5's Bluetooth sits behind a UART and mSBC frames arrive split
   across HCI packets. Fixed with `bluez5.enable-msbc = false` - CVSD,
   8 kHz, ordinary phone-line quality. After the change: codec 1 (CVSD)
   and zero decode failures during a call.

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

4. **A missing target records silence without error.** `pw-record
   --target X` with X absent falls back to the default sink's monitor and
   happily records nothing. The bridge starts both `pw-cat`s with
   `--target 0` and fails loudly (shown on the page) if the phone's
   streams or ports aren't there.

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

## The ring agent

`apps/phone/agent/phone_agent.pyw`, on the desktop. Polls
`/api/agent/ringing` once a second with a bearer token
(`PHONE_AGENT_TOKEN`); on a new ringing call, if no page has PC audio on,
it plays a ring and shows a small always-on-top popup. The token can read
ringing status and decline a *ringing* call - nothing else. It can't
answer, dial, or end a call in progress.

Answer opens `https://phone.home:8443/popup?answer=1` in Firefox rather
than answering from the agent: the browser holds the mic and speakers,
and the Pi only accepts the call's audio once such a page is connected.
Firefox only lets the page start audio without a click if the site has
a remembered mic permission and autoplay allowed; otherwise the page asks
for one click on Answer.

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

Firefox, once: trust the homelab root if it doesn't already (Firefox has
its own certificate store - [https.md](https.md)), log in at
`https://phone.home:8443`, and in the site's permissions allow the
microphone (remembered) and autoplay.

## Limits

- Narrowband (8 kHz) audio, because of bug 1. A USB Bluetooth dongle
  with SCO over USB would likely get wideband back.
- One phone. If two ever pair, the first gateway on the bus wins.
- Echo: the page asks the browser for echo cancellation, but a headset
  is still the reliable way to keep speaker audio out of the mic.
- If the page closes mid-call, the call's audio stays on the Pi; move it
  back from the iPhone's audio-route button.
- Only one phone can be the iPhone's hands-free unit at a time, so the
  car or earbuds compete with the Pi.
