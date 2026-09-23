"""System-wide hotkeys for calls, via the Win32 RegisterHotKey API (ctypes).

RegisterHotKey delivers WM_HOTKEY to the thread that registered the key,
so registration and the GetMessage loop share one dedicated thread.
Each callback runs on its own short-lived thread so a slow action (the
popup loading) can't stall the loop. A key another app already owns
can't be registered; that's logged, and the other keys still work.
"""
import ctypes
import ctypes.wintypes
import logging
import threading

log = logging.getLogger("phone-agent")

MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
WM_HOTKEY = 0x0312


def listen(bindings: dict[str, tuple[int, int, callable]]) -> None:
    """bindings: name -> (modifiers, virtual key, callback)."""

    def run() -> None:
        user32 = ctypes.windll.user32
        callbacks = {}
        for hotkey_id, (name, (mods, vk, callback)) in enumerate(bindings.items(), start=1):
            if user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
                callbacks[hotkey_id] = (name, callback)
            else:
                log.warning("hotkey %s is taken by another app; not registered", name)
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in callbacks:
                name, callback = callbacks[msg.wParam]
                log.info("hotkey: %s", name)
                threading.Thread(target=callback, daemon=True).start()

    threading.Thread(target=run, name="hotkeys", daemon=True).start()


CALL_HOTKEYS = {
    "answer (Ctrl+Alt+A)": (MOD_CONTROL | MOD_ALT, ord("A")),
    "hang up (Ctrl+Alt+H)": (MOD_CONTROL | MOD_ALT, ord("H")),
    "mute (Ctrl+Alt+M)": (MOD_CONTROL | MOD_ALT, ord("M")),
}
