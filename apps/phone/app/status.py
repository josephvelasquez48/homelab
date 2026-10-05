"""Status for the iPhone's app: how the homelab is, and two controls.

The health, alerts, vitals and events come from the dashboard
(https://dashboard.home/api/display, the same data the Pi's display draws),
fetched from here on the Pi and cached for CACHE_SECONDS so the phone
polling can't add load. What only this service knows is added: whether the
Pi's Bluetooth is on (reconnect.py) and whether the display is open.

The display is pi-display.service, a user unit like this one, so opening and
closing it is a systemctl --user call - the same as the display's Desktop
button and the "Homelab display" icon (apps/pi-display).
"""
import asyncio
import json
import os
import ssl
import time
import urllib.request
from pathlib import Path

DASHBOARD = os.environ.get("PHONE_DASHBOARD_URL", "https://dashboard.home/api/display")
CA_FILE = Path(__file__).resolve().parents[3] / "certificates" / "homelab-ca.crt"
CACHE_SECONDS = 5
DISPLAY_UNIT = "pi-display.service"
X11_SOCKET = Path("/tmp/.X11-unix/X0")

# What a phone needs of the dashboard's answer; the rest (pods, line rates,
# weather) is for drawing the map.
STATS = ("pi_temp_c", "pi_cpu", "pi_mem", "mac_temp_c", "mac_cpu", "mac_mem",
         "pc_temp_c", "pc_cpu", "pc_mem", "pods_ready", "pods_total", "dns_per_min", "blocked_pct")


def summarize(d: dict) -> dict:
    services = d.get("services", {})
    return {
        "services": services,
        "down": sorted(k for k, v in services.items() if v == "down"),
        "unknown": sorted(k for k, v in services.items() if v == "unknown"),
        "alerts": d.get("alerts") or [],
        "events": d.get("events") or [],
        "stats": {k: d.get("stats", {}).get(k) for k in STATS},
        "nodes": d.get("nodes") or [],
    }


class Status:
    def __init__(self):
        self._cache: dict | None = None
        self._fetched = 0.0
        self._error: str | None = None
        self._context = ssl.create_default_context(cafile=str(CA_FILE)) if CA_FILE.exists() else None
        self._lock = asyncio.Lock()

    def _fetch(self) -> dict:
        with urllib.request.urlopen(DASHBOARD, context=self._context, timeout=8) as r:
            return summarize(json.load(r))

    async def homelab(self) -> dict:
        """The dashboard's view, at most CACHE_SECONDS old; on failure the last
        good one with an error saying so."""
        async with self._lock:
            if time.monotonic() - self._fetched >= CACHE_SECONDS:
                try:
                    self._cache = await asyncio.to_thread(self._fetch)
                    self._error = None
                except (OSError, ValueError) as e:
                    self._error = f"Couldn't reach the dashboard: {e}"
                self._fetched = time.monotonic()
        return {**(self._cache or {}), "error": self._error, "age": round(time.monotonic() - self._fetched)}


async def _systemctl(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode().strip()


async def display_on() -> bool | None:
    """Is the display open? None if systemd can't say."""
    try:
        _, out = await _systemctl("is-active", DISPLAY_UNIT)
    except OSError:
        return None
    return out == "active"


async def set_display(on: bool) -> str | None:
    """Open or close the display; an error message, or None."""
    if not on:
        code, out = await _systemctl("stop", DISPLAY_UNIT)
        return None if code == 0 else out or "couldn't close the display"
    # Opening needs the desktop's X server, as the "Homelab display" icon has:
    # this service's own environment has none. Without a desktop up, say so
    # rather than leave Chromium restarting against nothing.
    if not X11_SOCKET.exists():
        return "The Pi's desktop isn't running (nobody is logged in on its screen)"
    code, out = await _systemctl("set-environment", "DISPLAY=:0", f"XAUTHORITY={Path.home() / '.Xauthority'}")
    if code != 0:
        return out or "couldn't prepare the display"
    code, out = await _systemctl("restart", DISPLAY_UNIT)
    return None if code == 0 else out or "couldn't open the display"
