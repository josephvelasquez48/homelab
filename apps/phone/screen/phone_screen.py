"""Show calls on the Pi's own touchscreen, as a remote control for the PC.

Runs in the logged-in desktop session (phone-screen.service, a systemd user
unit). It polls the phone service on loopback; while a call rings or is up
it wakes the display and opens Chromium full screen on the call page
(/popup?touch=1), and closes it when the call is over. The desktop is left
alone otherwise. Home on the call screen closes it early: the service
reports screenHidden for the calls up at the time, and a new call brings
the screen back.

The page has no login: Chromium maps phone.home to 127.0.0.1, so it arrives
on loopback, which the service trusts (main.py, is_local). The certificate
still matches phone.home; Chromium is told to trust that certificate's key
(--ignore-certificate-errors-spki-list), computed from the service's own
certificate at each launch - the Pi's Chromium doesn't use the system CA
store and certutil isn't installed.

Works on either desktop session: Wayland (labwc) or X11, which the Pi
runs so RustDesk can control it (docs/rustdesk.md). Whichever display is
up when a call comes in is used.

Stdlib only, so it runs on the system python3.
"""
import base64
import hashlib
import json
import logging
import os
import signal
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config/phone-bridge"
CERT = CONFIG / "tls.crt"
CA = Path(__file__).resolve().parents[3] / "certificates/homelab-ca.crt"
URL = "https://phone.home:8443/popup?touch=1"
STATUS = "https://127.0.0.1:8443/api/agent/ringing?screen=1"
PROFILE = Path.home() / ".local/share/phone-screen/chromium"
# Loopback and cheap, so poll often: the check interval is most of the delay
# between a call arriving and the screen showing it (Chromium itself starts
# and connects in ~0.8 s here). Closing waits only a moment - after the call
# ends the page is blank, and a 3 s wait there read as the screen hanging.
POLL_SECONDS = 0.25
CLOSE_AFTER_SECONDS = 0.5

log = logging.getLogger("phone-screen")


def wayland() -> bool:
    """Is the desktop a Wayland session (its socket exists) rather than X11?"""
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return (Path(runtime) / os.environ.get("WAYLAND_DISPLAY", "wayland-0")).exists()


def wake_x11() -> None:
    """Wake a blanked X11 display without turning power saving on.

    `xset dpms force on` also enables DPMS, with its default 10-minute
    timeout - on a Pi set never to blank, one call brought the sleep timer
    back. Turn it off again if it was off before.
    """
    was_off = "DPMS is Disabled" in subprocess.run(["xset", "q"], capture_output=True, text=True).stdout
    subprocess.run(["xset", "dpms", "force", "on"], capture_output=True)
    if was_off:
        subprocess.run(["xset", "-dpms"], capture_output=True)


def agent_token() -> str:
    for line in (CONFIG / "env").read_text().splitlines():
        if line.startswith("PHONE_AGENT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("PHONE_AGENT_TOKEN not in ~/.config/phone-bridge/env")


def spki_hash(cert_pem: str) -> str:
    """base64 SHA-256 of the certificate's public key (what Chromium pins)."""
    # The file may hold a chain; the service's own certificate comes first.
    first = cert_pem[: cert_pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
    der = ssl.PEM_cert_to_DER_cert(first)
    # openssl extracts the SubjectPublicKeyInfo; the stdlib can't parse X.509.
    pub = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-pubkey", "-noout"], input=der, capture_output=True, check=True
    ).stdout
    spki = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"], input=pub, capture_output=True, check=True
    ).stdout
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


class Screen:
    def __init__(self):
        self.token = agent_token()
        # Loopback: the certificate is for phone.home, so the name can't match;
        # the chain is still checked against the homelab CA.
        self.tls = ssl.create_default_context(cafile=str(CA))
        self.tls.check_hostname = False
        self.browser: subprocess.Popen | None = None
        self.idle_since: float | None = None

    def status(self) -> dict:
        req = urllib.request.Request(STATUS, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, context=self.tls, timeout=5) as resp:
            return json.load(resp)

    def open(self) -> None:
        on_wayland = wayland()
        if on_wayland:
            subprocess.run(["wlopm", "--on", "*"], capture_output=True)  # wake a blanked display
        else:
            wake_x11()
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.browser = subprocess.Popen(
            [
                "chromium",
                "--kiosk",
                f"--ozone-platform={'wayland' if on_wayland else 'x11'}",
                f"--user-data-dir={PROFILE}",
                "--host-resolver-rules=MAP phone.home 127.0.0.1",
                f"--ignore-certificate-errors-spki-list={spki_hash(CERT.read_text())}",
                "--noerrdialogs",
                "--disable-infobars",
                "--no-first-run",
                "--password-store=basic",
                "--disable-features=Translate",
                URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("call: screen opened")

    def close(self) -> None:
        if self.browser and self.browser.poll() is None:
            os.killpg(self.browser.pid, signal.SIGTERM)
            try:
                self.browser.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(self.browser.pid, signal.SIGKILL)
        self.browser = None
        log.info("call over: screen closed")

    def step(self) -> None:
        status = self.status()
        # Home on the call screen hides it for the calls up at the time; the
        # service brings it back for a new one.
        in_call = bool(status.get("inCall")) and not status.get("screenHidden")
        running = self.browser is not None and self.browser.poll() is None
        if in_call:
            self.idle_since = None
            if not running:
                self.open()
        elif running:
            self.idle_since = self.idle_since or time.monotonic()
            if time.monotonic() - self.idle_since >= CLOSE_AFTER_SECONDS:
                self.close()

    def run(self) -> None:
        last_error = None
        while True:
            try:
                self.step()
                last_error = None
            except Exception as e:  # service restarting, etc.
                if str(e) != last_error:
                    log.warning("status: %s", e)
                    last_error = str(e)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    screen = Screen()
    signal.signal(signal.SIGTERM, lambda *_: (screen.close(), exit(0)))
    screen.run()
