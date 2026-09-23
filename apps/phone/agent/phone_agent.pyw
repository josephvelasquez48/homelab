"""Desktop ring agent: a small always-on-top popup when the iPhone rings.

Runs in the background on the Windows desktop (pythonw, started at login
from the Startup folder - see docs/phone.md). A worker thread asks the Pi
once a second whether a call is ringing. On a new ringing call - unless a
browser page already has PC audio on, in which case that page rings by
itself - it rings through the PC speakers and shows a popup with the
caller and Answer / Decline.

Decline hangs up straight from here. Answer opens the phone page in
Firefox with ?answer=1: the browser has to be the one to answer, because
it's what holds the mic and speakers, and the Pi only takes the call's
audio once a page with audio on is connected.

Standard library only (tkinter ships with python.org Windows builds).
Config: %APPDATA%\\phone-bridge\\agent.json

    {"url": "https://phone.home:8443", "token": "<PHONE_AGENT_TOKEN from the Pi>"}
"""
import json
import logging
import math
import os
import queue
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import wave
import winsound
from pathlib import Path

CONFIG_DIR = Path(os.environ["APPDATA"]) / "phone-bridge"
CA_FILE = Path(__file__).resolve().parents[3] / "certificates" / "homelab-ca.crt"
FIREFOX_PATHS = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Mozilla Firefox/firefox.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Mozilla Firefox/firefox.exe",
]
POLL_SECONDS = 1.0

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

    def _request(self, path: str, method: str = "GET"):
        req = urllib.request.Request(
            f"{self.url}{path}", method=method, headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req, context=self.context, timeout=5) as r:
            return json.load(r)

    def ringing(self) -> dict:
        return self._request("/api/agent/ringing")

    def decline(self) -> None:
        self._request("/api/agent/decline", method="POST")


def poll(pi: Pi, updates: queue.Queue) -> None:
    """Worker thread: push the Pi's ringing status to the UI thread."""
    failures = 0
    while True:
        try:
            updates.put(pi.ringing())
            failures = 0
            time.sleep(POLL_SECONDS)
        except (urllib.error.URLError, OSError, ValueError) as e:
            failures += 1
            if failures in (1, 10) or failures % 300 == 0:
                log.warning("can't reach the Pi (%s failures): %s", failures, e)
            updates.put({"ringing": None, "audioPages": 0})
            time.sleep(min(30, POLL_SECONDS * failures))


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


def open_in_firefox(url: str) -> None:
    firefox = next((p for p in FIREFOX_PATHS if p.exists()), None)
    if firefox is None:
        os.startfile(url)
        return
    subprocess.Popen([str(firefox), "--new-window", url])


class Agent:
    BG, FG, MUTED = "#1b1e24", "#e8eaee", "#9aa1ad"

    def __init__(self, pi: Pi):
        self.pi = pi
        self.root = tk.Tk()
        self.root.withdraw()
        self.updates: queue.Queue = queue.Queue()
        self.popup: tk.Toplevel | None = None
        self.popup_call: str | None = None
        self.dismissed: set[str] = set()
        self.ringing = False

    def run(self) -> None:
        threading.Thread(target=poll, args=(self.pi, self.updates), daemon=True).start()
        self.root.after(200, self.drain)
        self.root.mainloop()

    def drain(self) -> None:
        status = None
        while not self.updates.empty():
            status = self.updates.get_nowait()
        if status is not None:
            self.apply(status)
        self.root.after(200, self.drain)

    def apply(self, status: dict) -> None:
        call = status.get("ringing")
        show = bool(call) and status.get("audioPages", 0) == 0 and call["path"] not in self.dismissed
        if show and self.popup_call != call["path"]:
            log.info("ringing: %s", call.get("name") or call.get("number") or "unknown")
            self.show(call)
        elif not show and self.popup is not None:
            # Answered elsewhere, declined, missed, or a page turned audio on.
            self.close()
        if not call:
            self.dismissed.clear()
        self.set_ringing(show)

    def set_ringing(self, on: bool) -> None:
        if on and not self.ringing:
            winsound.PlaySound(ring_wav(), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
        elif not on and self.ringing:
            winsound.PlaySound(None, 0)
        self.ringing = on

    def show(self, call: dict) -> None:
        self.close()
        self.popup_call = call["path"]
        w = self.popup = tk.Toplevel(self.root, bg=self.BG, padx=16, pady=14)
        w.title("Incoming call")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", lambda: self.dismiss(call["path"]))

        tk.Label(w, text="Incoming call", bg=self.BG, fg=self.MUTED, font=("Segoe UI", 10)).pack(anchor="w")
        who = call.get("name") or call.get("number") or "Unknown caller"
        tk.Label(w, text=who, bg=self.BG, fg=self.FG, font=("Segoe UI Semibold", 16)).pack(anchor="w", pady=(2, 12))
        row = tk.Frame(w, bg=self.BG)
        row.pack(fill="x")
        style = {"fg": "white", "bd": 0, "font": ("Segoe UI Semibold", 11), "padx": 18, "pady": 7, "cursor": "hand2"}
        tk.Button(row, text="Answer", bg="#15803d", activebackground="#166534", command=self.answer, **style).pack(side="left", expand=True, fill="x", padx=(0, 6))
        tk.Button(row, text="Decline", bg="#c2362b", activebackground="#991b1b", command=self.decline, **style).pack(side="left", expand=True, fill="x")

        # Bottom-right, above the taskbar, like a notification.
        w.update_idletasks()
        x = w.winfo_screenwidth() - w.winfo_reqwidth() - 24
        y = w.winfo_screenheight() - w.winfo_reqheight() - 90
        w.geometry(f"+{x}+{y}")
        w.lift()
        w.focus_force()

    def close(self) -> None:
        if self.popup is not None:
            self.popup.destroy()
        self.popup = None
        self.popup_call = None

    def dismiss(self, path: str) -> None:
        # Closing the popup silences it without declining the call.
        self.dismissed.add(path)
        self.close()
        self.set_ringing(False)

    def answer(self) -> None:
        path = self.popup_call
        self.dismiss(path)
        open_in_firefox(f"{self.pi.url}/popup?answer=1")

    def decline(self) -> None:
        self.dismiss(self.popup_call)
        threading.Thread(target=self._decline, daemon=True).start()

    def _decline(self) -> None:
        try:
            self.pi.decline()
        except (urllib.error.URLError, OSError) as e:
            log.warning("decline failed: %s", e)


def main() -> None:
    config = json.loads((CONFIG_DIR / "agent.json").read_text())
    pi = Pi(config)
    log.info("watching %s", pi.url)
    Agent(pi).run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("agent stopped")
        raise
