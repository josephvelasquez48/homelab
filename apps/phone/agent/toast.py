"""The iPhone's notifications as native Windows notifications, with app icons.

Windows gives a desktop app a notification identity through the user's
registry (HKCU\\Software\\Classes\\AppUserModelId\\<id>): a display name and
an icon, no installer or Start menu shortcut needed. Under it each
notification is a toast - the iPhone app's icon as the picture, the title,
the message, and "via <app>" - shown through Windows' own toast API from the
PowerShell every Windows has, so the agent's venv needs nothing new.

Icons: the iPhone sends only the app's bundle id (com.burbn.instagram) over
Bluetooth, no picture. The App Store's public lookup gives an app's icon by
bundle id - Apple's own apps (Messages) included; it's fetched once per
app, cached in ICON_DIR with rounded corners, and a miss is remembered too,
falling back to a phone icon.
"""
import json
import logging
import os
import subprocess
import urllib.parse
import urllib.request
import winreg
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw

log = logging.getLogger("phone-agent")

APP_ID = "Homelab.PhoneBridge"  # taskbar.APP_ID: the agent's identity
DISPLAY_NAME = "iPhone"
BASE = Path(os.environ["LOCALAPPDATA"]) / "phone-bridge"
ICON_DIR = BASE / "app-icons"
SOURCE_ICON = BASE / "iphone.png"  # the notifications' sender icon
LOOKUP = "https://itunes.apple.com/lookup?bundleId={}&country=us"
ICON_PX = 128
CREATE_NO_WINDOW = 0x08000000

SHOW = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml([Console]::In.ReadToEnd())
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:TOAST_APP_ID).Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def _rounded(img: Image.Image) -> Image.Image:
    """An iOS-style rounded square, as the icon looks on the iPhone."""
    img = img.convert("RGBA").resize((ICON_PX, ICON_PX), Image.LANCZOS)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, ICON_PX - 1, ICON_PX - 1), radius=ICON_PX // 4.5, fill=255)
    img.putalpha(mask)
    return img


def _phone_icon() -> Image.Image:
    """For the sender, and for apps with no store icon: a simple phone."""
    img = Image.new("RGBA", (ICON_PX, ICON_PX), (48, 209, 88, 255))
    d = ImageDraw.Draw(img)
    s = ICON_PX
    d.rounded_rectangle((s * 0.33, s * 0.16, s * 0.67, s * 0.84), radius=s // 14, outline="white", width=s // 18)
    d.line((s * 0.45, s * 0.75, s * 0.55, s * 0.75), fill="white", width=s // 22)
    return _rounded(img)


def register() -> None:
    """Give the agent its notification name and icon (idempotent)."""
    BASE.mkdir(parents=True, exist_ok=True)
    if not SOURCE_ICON.exists():
        _phone_icon().save(SOURCE_ICON)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, DISPLAY_NAME)
        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(SOURCE_ICON))


def app_icon(bundle_id: str) -> Path:
    """The app's icon file, looked up once; the phone icon if it has none."""
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in bundle_id if c.isalnum() or c in "._-") or "unknown"
    path, miss = ICON_DIR / f"{safe}.png", ICON_DIR / f"{safe}.none"
    if path.exists():
        return path
    if miss.exists() or not bundle_id:
        return SOURCE_ICON
    try:
        with urllib.request.urlopen(LOOKUP.format(urllib.parse.quote(bundle_id)), timeout=8) as r:
            results = json.load(r).get("results") or []
        url = results[0].get("artworkUrl512") or results[0].get("artworkUrl100") if results else None
        if not url:
            miss.touch()  # not in the US store
            return SOURCE_ICON
        tmp = ICON_DIR / f"{safe}.download"
        with urllib.request.urlopen(url, timeout=10) as r, open(tmp, "wb") as f:
            f.write(r.read())
        with Image.open(tmp) as img:
            _rounded(img).save(path)
        tmp.unlink(missing_ok=True)
        return path
    except (OSError, ValueError) as e:
        log.info("icon lookup failed for an app: %s", type(e).__name__)  # tried again next time
        return SOURCE_ICON


def show(n: dict) -> bool:
    """One iPhone notification as a native toast. False if Windows refused,
    so the caller can fall back to the tray's plain one."""
    icon = app_icon(n.get("appId", ""))
    title = n.get("title") or n.get("app") or "iPhone"
    lines = f"<text>{escape(title)}</text>"
    if n.get("message"):
        lines += f"<text>{escape(n['message'])}</text>"
    if n.get("app") and n.get("title"):
        lines += f'<text placement="attribution">via {escape(n["app"])}</text>'
    xml = (f'<toast><visual><binding template="ToastGeneric">'
           f'<image placement="appLogoOverride" src="{escape(icon.as_uri())}"/>{lines}'
           f'</binding></visual></toast>')
    # ASCII only, the rest as XML character references: PowerShell's stdin
    # is the Windows code page, which has no emoji - "🤍" broke
    # every toast with one (2026-10-29).
    xml = xml.encode("ascii", "xmlcharrefreplace").decode("ascii")
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", SHOW],
            input=xml, text=True, capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW,
            env={**os.environ, "TOAST_APP_ID": APP_ID},
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("toast failed: %s", type(e).__name__)
        return False
    if done.returncode:
        log.warning("toast failed: %s", (done.stderr or "").strip().splitlines()[-1:] or done.returncode)
        return False
    return True
