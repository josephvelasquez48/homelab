"""Tray icon for the ring agent: status at a glance, a small menu, notifications.

pystray runs its own Win32 message loop, so it gets its own thread; the
agent updates it from the polling thread. The icon is drawn with Pillow
rather than shipped as a file: a phone outline on a circle that's green
while the iPhone is connected, amber during a call and grey otherwise.
"""
import threading

import pystray
from PIL import Image, ImageDraw

GREEN, AMBER, GREY = (21, 128, 61), (217, 119, 6), (107, 114, 128)


def icon_image(color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=color)
    d.rounded_rectangle((21, 12, 43, 52), radius=5, outline="white", width=4)
    d.ellipse((29, 43, 35, 49), fill="white")
    return img


class Tray:
    def __init__(self, agent):
        self.agent = agent
        self._color = None
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self.agent.status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open phone page", lambda: self.agent.open_page()),
            pystray.MenuItem(
                "Pause call popups", lambda: self.agent.toggle_pause(), checked=lambda item: self.agent.paused
            ),
            pystray.MenuItem("Hotkeys: Ctrl+Alt+A answer, Ctrl+Alt+H hang up, Ctrl+Alt+M mute", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.agent.quit()),
        )
        self.icon = pystray.Icon("phone-bridge", icon_image(GREY), "Phone", menu)

    def start(self) -> None:
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

    def stop(self) -> None:
        self.icon.stop()

    def update(self, color: tuple[int, int, int], tooltip: str) -> None:
        if color != self._color:
            self.icon.icon = icon_image(color)
            self._color = color
        if self.icon.title != tooltip:
            self.icon.title = tooltip[:120]
        self.icon.update_menu()

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message[:250], title[:60])
        except Exception:
            pass  # the tray may still be starting up
