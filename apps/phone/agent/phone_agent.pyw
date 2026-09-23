"""Desktop ring agent: take the whole call in a small always-on-top window.

Runs in the background on the Windows desktop (pythonw from its own venv,
started at login from the Startup folder - see docs/phone.md). A worker
thread asks the Pi once a second whether a call is ringing. On a new
ringing call - unless a browser page already has PC audio on, in which
case that page rings by itself - it rings through the PC speakers and
shows its window: the phone page's compact /popup view, embedded with
pywebview on WebView2 (Windows' built-in web engine; no browser window
opens). Answer, Decline, Mute, Keypad and Hang up all happen in there,
and the page holds the mic and speakers, so the call gets the web
engine's echo cancellation. The window hides itself when the call is
over, and is blanked while hidden so it doesn't count as an open page.

There's no login step: when the embedded page comes up on the login
form, the agent signs it in with its token (POST /api/agent/session from
inside the page) and reloads. The session lives in the agent's own
WebView2 profile under %LOCALAPPDATA%.

Config: %APPDATA%\\phone-bridge\\agent.json

    {"url": "https://phone.home:8443", "token": "<PHONE_AGENT_TOKEN from the Pi>"}
"""
import json
import logging
import math
import os
import ssl
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
import winsound
from pathlib import Path

import webview

CONFIG_DIR = Path(os.environ["APPDATA"]) / "phone-bridge"
PROFILE_DIR = Path(os.environ["LOCALAPPDATA"]) / "phone-bridge" / "webview"
CA_FILE = Path(__file__).resolve().parents[3] / "certificates" / "homelab-ca.crt"
POLL_SECONDS = 1.0
WIDTH, HEIGHT = 360, 400  # room for the call card plus the volume slider

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=CONFIG_DIR / "agent.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("phone-agent")


class Pi:
    def __init__(self, config: dict):
        self.url = config["url"].rstrip("/")
        self.token = config["token"]
        self.context = ssl.create_default_context(cafile=str(Path(config.get("cafile", CA_FILE))))

    def status(self) -> dict:
        req = urllib.request.Request(
            f"{self.url}/api/agent/ringing", headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req, context=self.context, timeout=5) as r:
            return json.load(r)


# Ringtone: a marimba-style rising arpeggio (E5 G#5 B5 E6) played twice,
# then a pause - the same pattern the phone page plays (app.js RINGTONE).
# Not the 440+480 Hz US ringback tone this used to be, which is what a
# *caller* hears and made an incoming call sound like an outgoing one.
RINGTONE_NOTES = [659.25, 830.61, 987.77, 1318.51]
NOTE_SECONDS = 0.13
CYCLE_SECONDS = 2.6


def marimba(freq: float, t: float) -> float:
    """One struck note: fundamental plus the bar's ~4x overtone, fast attack, exponential decay."""
    attack = min(1.0, t / 0.004)
    return attack * (math.sin(2 * math.pi * freq * t) * math.exp(-t * 7)
                     + 0.35 * math.sin(2 * math.pi * freq * 3.9 * t) * math.exp(-t * 22))


def ring_wav() -> str:
    """One ringtone cycle as a WAV winsound can loop."""
    path = Path(tempfile.gettempdir()) / "phone-bridge-ringtone-v2.wav"
    if path.exists():
        return str(path)
    rate = 22050
    samples = [0.0] * int(rate * CYCLE_SECONDS)
    starts = [(i + rep * (len(RINGTONE_NOTES) + 1)) * NOTE_SECONDS for rep in range(2) for i in range(len(RINGTONE_NOTES))]
    for n, start in enumerate(starts):
        freq = RINGTONE_NOTES[n % len(RINGTONE_NOTES)]
        first = int(start * rate)
        for k in range(int(0.9 * rate)):
            if first + k < len(samples):
                samples[first + k] += marimba(freq, k / rate)
    peak = max(abs(v) for v in samples) or 1.0
    frames = b"".join(struct.pack("<h", int(v / peak * 0.5 * 32767)) for v in samples)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return str(path)


def corner() -> tuple[int, int]:
    """Bottom-right, above the taskbar, like a notification."""
    try:
        screen = webview.screens[0]
        return screen.width - WIDTH - 24, screen.height - HEIGHT - 72
    except Exception:
        return 100, 100


class Agent:
    def __init__(self, pi: Pi, window: webview.Window):
        self.pi = pi
        self.window = window
        self.showing = False
        self.dismissed: set[str] = set()
        self.ringing = False
        self._permissions_hooked = False
        window.events.loaded += self.hook_permissions
        window.events.loaded += self.sign_in
        window.events.closing += self.on_closing

    # -- WebView2 wiring ------------------------------------------------------

    def hook_permissions(self) -> None:
        """Grant the phone page the microphone - nothing else, no other site.

        pywebview 6.2.1 doesn't handle CoreWebView2.PermissionRequested, so
        without this the embedded page would prompt (or be refused) every
        time Answer asks for the mic.
        """
        if self._permissions_hooked:
            return
        # pywebview fires `loaded` on a worker thread, and WebView2 throws
        # "CoreWebView2 can only be accessed from the UI thread" - so hop
        # onto the form's thread first.
        from System import Action

        self.window.native.Invoke(Action(self._hook_permissions_on_ui_thread))

    def _hook_permissions_on_ui_thread(self) -> None:
        if self._permissions_hooked:
            return
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind, CoreWebView2PermissionState

        def on_request(sender, args):
            same_site = str(args.Uri).startswith(self.pi.url + "/")
            if same_site and args.PermissionKind == CoreWebView2PermissionKind.Microphone:
                args.State = CoreWebView2PermissionState.Allow
            else:
                args.State = CoreWebView2PermissionState.Deny

        self.window.native.browser.webview.CoreWebView2.PermissionRequested += on_request
        self._permissions_hooked = True

    def sign_in(self) -> None:
        """If the page came up on the login form, sign in with the token and reload.

        A same-origin fetch from the page itself, so the SameSite=Strict
        session cookie is set in this window's profile and sent on the
        reload. The token goes through evaluate_js, never a URL.
        """
        if not self.showing:
            return
        script = (
            "(() => { if (!document.querySelector('form[action=\"/login\"]')) return;"
            " fetch('/api/agent/session', {method: 'POST', headers: {Authorization: %s}})"
            ".then(r => { if (r.ok) location.reload(); }); })()"
        ) % json.dumps(f"Bearer {self.pi.token}")
        self.window.evaluate_js(script)

    def on_closing(self) -> bool:
        # The window is reused for every call, so closing it only hides it.
        # A ringing call is silenced (not declined); a call in progress keeps
        # the window, since hiding it would leave no way to hang up.
        threading.Thread(target=self._close_requested, daemon=True).start()
        return False

    def _close_requested(self) -> None:
        status = self._status()
        call = status.get("ringing")
        if call:
            self.dismissed.add(call["path"])
            self.hide()
        elif not status.get("inCall"):
            self.hide()

    # -- show / hide ------------------------------------------------------------

    def show(self) -> None:
        self.showing = True
        self.window.load_url(f"{self.pi.url}/popup?agent=1")
        self.window.show()

    def hide(self) -> None:
        self.set_ringing(False)
        self.showing = False
        self.window.hide()
        # Blank it: a hidden page still holding the mic would count as an
        # open audio page, and the Pi would keep taking calls' audio for it.
        self.window.load_url("about:blank")

    def set_ringing(self, on: bool) -> None:
        if on and not self.ringing:
            winsound.PlaySound(ring_wav(), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        elif not on and self.ringing:
            winsound.PlaySound(None, 0)
        self.ringing = on

    # -- polling ------------------------------------------------------------------

    def _status(self) -> dict:
        try:
            return self.pi.status()
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("can't reach the Pi: %s", e)
            return {}

    def run(self) -> None:
        log.info("watching %s", self.pi.url)
        failures = 0
        while True:
            status = self._status()
            failures = failures + 1 if not status else 0
            try:
                self.apply(status)
            except Exception:
                log.exception("update failed")
            time.sleep(min(30, POLL_SECONDS * max(1, failures)))

    def apply(self, status: dict) -> None:
        call = status.get("ringing")
        if not status.get("inCall"):
            self.dismissed.clear()
        wanted = bool(call) and call["path"] not in self.dismissed
        if wanted and not self.showing and status.get("audioPages", 0) == 0:
            log.info("ringing: %s", call.get("name") or call.get("number") or "unknown")
            self.show()
        # Once answered here, this window's page is the audio page, so the
        # call is "ours" while inCall and audioPages hold. Answered on the
        # phone instead, nobody turned audio on - nothing left to show.
        still_needed = wanted or (status.get("inCall") and status.get("audioPages", 0) > 0)
        if self.showing and not still_needed:
            self.hide()
        self.set_ringing(self.showing and wanted)


def main() -> None:
    config = json.loads((CONFIG_DIR / "agent.json").read_text())
    pi = Pi(config)
    x, y = corner()
    window = webview.create_window(
        "Phone",
        url="about:blank",
        width=WIDTH,
        height=HEIGHT,
        x=x,
        y=y,
        on_top=True,
        hidden=True,
        background_color="#111317",
    )
    agent = Agent(pi, window)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    webview.start(agent.run, private_mode=False, storage_path=str(PROFILE_DIR))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("agent stopped")
        raise
