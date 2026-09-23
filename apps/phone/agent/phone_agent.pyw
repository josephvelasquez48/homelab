"""Desktop ring agent: take the whole call in a small always-on-top window.

Runs in the background on the Windows desktop (pythonw from its own venv,
started at login from the Startup folder - see docs/phone.md). A worker
thread asks the Pi once a second about calls. For every
call - incoming, answered on the iPhone, or dialed from it - it shows
its window (and rings through the PC speakers while the call rings,
unless a browser page with PC audio on is open to ring instead): the
phone page's compact /popup view, embedded with
pywebview on WebView2 (Windows' built-in web engine; no browser window
opens). Answer, Decline, Mute, Keypad and Hang up all happen in there,
and the page holds the mic and speakers, so the call gets the web
engine's echo cancellation. For a call on the iPhone it offers "Move
call audio to this PC". The window hides itself when the call is over,
and is blanked while hidden so it doesn't count as an open page.

It's also the whole app, so no browser is needed: the tray's "Open
phone" and the desktop shortcut (phone_agent.pyw --show) open the same
window in app mode - the full page, with the dial pad, recent calls, mic
picker and settings - which stays until closed. A second copy of the
agent started with --show just tells the running one to open, over a
localhost socket that also keeps the agent to a single instance.

It also keeps a tray icon (iPhone connected / on a call / not connected,
a menu to open the phone window or pause popups, and notifications for missed
calls) and system-wide hotkeys: Ctrl+Alt+A answers (or
moves a call's audio to the PC), Ctrl+Alt+H declines or hangs up,
Ctrl+Alt+M mutes. See tray.py and hotkeys.py.

There's no login step: when the embedded page comes up on the login
form, the agent signs it in with its token (POST /api/agent/session from
inside the page) and reloads. The session lives in the agent's own
WebView2 profile under %LOCALAPPDATA%.

Config: %APPDATA%\\phone-bridge\\agent.json

    {"url": "https://phone.home:8443", "token": "<PHONE_AGENT_TOKEN from the Pi>"}
"""
import faulthandler
import json
import logging
import math
import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
import winsound
from pathlib import Path

import webview

import hotkeys
from tray import AMBER, GREEN, GREY, Tray

CONFIG_DIR = Path(os.environ["APPDATA"]) / "phone-bridge"
PROFILE_DIR = Path(os.environ["LOCALAPPDATA"]) / "phone-bridge" / "webview"
CA_FILE = Path(__file__).resolve().parents[3] / "certificates" / "homelab-ca.crt"
POLL_SECONDS = 1.0
# 127.0.0.1 only: how a second copy (the desktop shortcut) tells the
# running agent to open its window, and how it knows one is running.
SHOW_PORT = 51871

# JS run in the popup by the hotkeys: click the first of these buttons that
# is actually visible (its row may be hidden), and say which one it was.
CLICK_FIRST_VISIBLE = """(() => {
  for (const id of %s) {
    const b = document.getElementById(id);
    if (b && !b.closest('[hidden]')) { b.click(); return id; }
  }
  return null;
})()"""
WIDTH, HEIGHT = 360, 400  # call popup: room for the call card plus the volume slider
APP_WIDTH, APP_HEIGHT = 420, 760  # app window: the full page, scrolls if needed

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=CONFIG_DIR / "agent.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("phone-agent")

# pythonw has no console: sys.stderr is None, so a crash message - an
# uncaught exception in a thread, a warning, a fatal error - went nowhere,
# and the agent once vanished (tray icon and all) without a line in its
# log. Send all of it to the log file instead.
_crash_log = open(CONFIG_DIR / "agent.log", "a", buffering=1, encoding="utf-8")
sys.stderr = _crash_log
faulthandler.enable(_crash_log)  # hard crashes (native code) dump a traceback too
threading.excepthook = lambda args: log.error(
    "uncaught in thread %s", args.thread.name if args.thread else "?",
    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
)


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


def corner(width: int = WIDTH, height: int = HEIGHT) -> tuple[int, int]:
    """Bottom-right, above the taskbar, like a notification."""
    try:
        screen = webview.screens[0]
        return max(0, screen.width - width - 24), max(0, screen.height - height - 72)
    except Exception:
        return 100, 100


class Agent:
    def __init__(self, pi: Pi, window: webview.Window):
        self.pi = pi
        self.window = window
        self.showing = False
        # "call": the compact popup, shown for a call and hidden after it.
        # "app": the full page, opened from the tray or the desktop
        # shortcut, which stays until closed.
        self.mode: str | None = None
        self.call_seen: str | None = None  # the call the app window last came forward for
        self.open_on_start = False
        self.dismissed: set[str] = set()
        self.ringing = False
        self._permissions_hooked = False
        self.paused = False
        self.quitting = False
        self.status_text = "Starting..."
        self.tray = Tray(self)
        # None until the first poll, so a call missed before the agent
        # started isn't announced.
        self._seen_missed: int | None = None
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
        if self.quitting:
            return True
        # The window is reused for every call, so closing it only hides it.
        # A ringing call is silenced (not declined); a call in progress keeps
        # the window, since hiding it would leave no way to hang up.
        threading.Thread(target=self._close_requested, daemon=True).start()
        return False

    def _close_requested(self) -> None:
        status = self._status()
        call = status.get("call")
        if self.mode == "app" and not self._holds_call_audio():
            if call:
                self.dismissed.add(call["path"])
            self.hide()
        elif not call:
            self.hide()
        elif not self._holds_call_audio():
            # Ringing, or a call whose audio is on the iPhone (or in another
            # page): hide for this call. It carries on regardless.
            self.dismissed.add(call["path"])
            self.hide()
        # Otherwise this window is the call's mic and speakers - closing it
        # would leave the call silent with no way to hang up, so it stays.

    def _holds_call_audio(self) -> bool:
        """Whether this window's page answered or took over the call's audio."""
        try:
            return bool(self.window.evaluate_js("typeof answeredHere !== 'undefined' && answeredHere && !!audio"))
        except Exception:
            return False

    # -- show / hide ------------------------------------------------------------

    def show(self) -> None:
        """The compact call popup, bottom-right and on top."""
        self.showing, self.mode = True, "call"
        self.window.on_top = True
        self.window.resize(WIDTH, HEIGHT)
        self.window.move(*corner())
        self.window.load_url(f"{self.pi.url}/popup?agent=1")
        self.window.show()

    def open_app(self) -> None:
        """The full app: from the tray, the desktop shortcut or a --show."""
        if self.showing and self._holds_call_audio():
            # Mid-call through this window: reloading would cut the call's
            # audio, so just bring it forward.
            self.window.restore()
            self.window.show()
            return
        self.showing, self.mode = True, "app"
        self.call_seen = None
        self.window.on_top = False
        self.window.resize(APP_WIDTH, APP_HEIGHT)
        self.window.move(*corner(APP_WIDTH, APP_HEIGHT))
        self.window.load_url(f"{self.pi.url}/?agent=1")
        self.window.show()
        self.window.restore()

    def hide(self) -> None:
        self.set_ringing(False)
        self.showing = False
        self.mode = None
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
        if self.open_on_start:  # started by the desktop shortcut with none running
            self.open_app()
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
        self.update_tray(status)
        self.notify_new(status)
        # Shown for every call - ringing, answered on the iPhone, or dialed
        # from it - so its audio can be moved to the PC at any point, until
        # the call ends or the window is closed for that call.
        call = status.get("call")
        if self.mode == "app":
            # The app window stays open with or without a call. A new call
            # brings it forward and on top (it shows the call card itself);
            # once the call is over it goes back to a normal window.
            if not call:
                self.dismissed.clear()
                if self.call_seen:
                    self.call_seen = None
                    self.window.on_top = False
                self.set_ringing(False)
                return
            if call["path"] != self.call_seen and call["path"] not in self.dismissed:
                self.call_seen = call["path"]
                log.info("%s call: %s", call.get("state"), call.get("name") or call.get("number") or "unknown")
                self.window.restore()
                self.window.on_top = True
                self.window.show()
            self.set_ringing(bool(status.get("ringing")) and status.get("audioPages", 0) == 0)
            return
        if not call:
            self.dismissed.clear()
            if self.showing:
                self.hide()
            return
        if call["path"] in self.dismissed:
            if self.showing:
                self.hide()
            return
        if self.paused and not self.showing:
            return
        if not self.showing:
            log.info("%s call: %s", call.get("state"), call.get("name") or call.get("number") or "unknown")
            self.show()
        # Ring while it rings - unless a page with PC audio on is open, which
        # rings by itself.
        self.set_ringing(bool(status.get("ringing")) and status.get("audioPages", 0) == 0)


    # -- tray, notifications, hotkeys ---------------------------------------------

    def update_tray(self, status: dict) -> None:
        call = status.get("call")
        if not status:
            color, text = GREY, "Can't reach the Pi"
        elif call:
            who = call.get("name") or call.get("number") or "unknown"
            color, text = AMBER, ("Ringing: " if status.get("ringing") else "On a call: ") + who
        elif status.get("connected"):
            color, text = GREEN, "iPhone connected"
        else:
            color, text = GREY, "iPhone not connected"
        self.status_text = text + (" (popups paused)" if self.paused else "")
        self.tray.update(color, "Phone - " + self.status_text)

    def notify_new(self, status: dict) -> None:
        if not status:
            return
        missed = status.get("missed")
        if self._seen_missed is None:  # first poll: remember, don't announce
            self._seen_missed = missed["id"] if missed else 0
            return
        if missed and missed["id"] != self._seen_missed:
            self._seen_missed = missed["id"]
            self.tray.notify("Missed call", missed.get("name") or missed.get("number") or "Unknown caller")

    def listen_for_show(self, server: socket.socket) -> None:
        """Serve "show" requests from a second copy (the desktop shortcut)."""

        def run() -> None:
            while True:
                conn, _ = server.accept()
                with conn:
                    if conn.recv(16).startswith(b"show"):
                        self.open_app()

        threading.Thread(target=run, name="show", daemon=True).start()

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        log.info("popups %s", "paused" if self.paused else "resumed")
        if self.paused and self.showing and not self._holds_call_audio():
            self.hide()

    def quit(self) -> None:
        log.info("quitting")
        self.quitting = True
        self.set_ringing(False)
        self.tray.stop()
        self.window.destroy()

    def _press(self, *button_ids: str) -> None:
        """Hotkey action: bring up the popup for the live call and click a button in it."""
        call = self._status().get("call")
        if not call:
            return
        self.dismissed.discard(call["path"])
        if not self.showing:
            self.show()
        script = CLICK_FIRST_VISIBLE % json.dumps(list(button_ids))
        for _ in range(30):  # the page may still be loading or signing in
            try:
                if self.window.evaluate_js(script):
                    return
            except Exception:
                pass
            time.sleep(0.3)
        log.warning("hotkey found none of %s to press", button_ids)

    def start_hotkeys(self) -> None:
        actions = {
            "answer (Ctrl+Alt+A)": lambda: self._press("answer", "to-pc"),
            "hang up (Ctrl+Alt+H)": lambda: self._press("decline", "hangup"),
            "mute (Ctrl+Alt+M)": lambda: self._press("mute"),
        }
        hotkeys.listen({name: (*keys, actions[name]) for name, keys in hotkeys.CALL_HOTKEYS.items()})


def allow_audio_without_a_click() -> None:
    """Let the popup start audio from a hotkey, not only from a mouse click.

    Chromium's autoplay policy keeps an AudioContext suspended until a
    user gesture, and a hotkey isn't one to the page. pywebview offers no
    way to add WebView2 browser arguments, so extend the ones it sets once
    it has built them. Only this agent's embedded window is affected.
    """
    from webview.platforms import edgechromium

    original = edgechromium.EdgeChrome.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.webview.CreationProperties.AdditionalBrowserArguments += " --autoplay-policy=no-user-gesture-required"

    edgechromium.EdgeChrome.__init__ = init


def ask_running_agent_to_show() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", SHOW_PORT), timeout=2) as s:
            s.sendall(b"show")
        return True
    except OSError:
        return False


def main() -> None:
    try:
        server = socket.create_server(("127.0.0.1", SHOW_PORT))
    except OSError:
        # One is running already: hand over (the desktop shortcut) or leave
        # (a duplicate start at login).
        if "--show" in sys.argv:
            ask_running_agent_to_show()
        return
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
    agent.listen_for_show(server)
    agent.open_on_start = "--show" in sys.argv
    agent.tray.start()
    agent.start_hotkeys()
    allow_audio_without_a_click()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    webview.start(agent.run, private_mode=False, storage_path=str(PROFILE_DIR))


def supervise() -> None:
    """Run the agent as a child process and restart it if it dies.

    The agent can die in ways Python can't catch - WebView2 and pythonnet
    are native code - and it's what makes calls reach the PC, so it runs
    under this small loop. A clean exit (Quit in the tray, or a duplicate
    that found one already running) ends the loop too; a crash restarts it
    after a short, growing pause, and five crashes within ten minutes stop
    it rather than loop forever.
    """
    crashes: list[float] = []
    while True:
        started = time.time()
        code = subprocess.call([sys.executable, __file__, "--child", *sys.argv[1:]])
        if code == 0:
            return
        now = time.time()
        crashes = [t for t in crashes if now - t < 600] + [now]
        log.error("agent exited with code %s after %.0f s", code, now - started)
        if len(crashes) >= 5:
            log.error("5 crashes in 10 minutes - not restarting; see the tracebacks above")
            return
        time.sleep(min(60, 5 * len(crashes)))
        # A restart isn't a new request to open the window.
        sys.argv = [a for a in sys.argv if a != "--show"]


if __name__ == "__main__":
    if "--child" not in sys.argv:
        supervise()
        sys.exit(0)
    try:
        main()
    except Exception:
        log.exception("agent stopped")
        raise
