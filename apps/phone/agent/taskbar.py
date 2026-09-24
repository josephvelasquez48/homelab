"""Give the agent its own taskbar identity, so its window can be pinned.

Windows groups and pins taskbar buttons by AppUserModelID. Without one the
agent's window belongs to pythonw.exe: pinning it pinned Python itself, with
no script and no icon. With an explicit ID on the process, and on the window
together with a relaunch command, name and icon, pinning the window pins
"Phone", and clicking the pin starts the agent (`--show`) and opens it.

Plain ctypes COM (IPropertyStore) rather than pywin32/comtypes, so the
agent's venv needs nothing new.
"""
import ctypes
from ctypes import wintypes

APP_ID = "Homelab.PhoneBridge"

VT_LPWSTR = 31


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, text: str):
        super().__init__()
        ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(self))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    # vt + 3 reserved words, then the value union (16 bytes on x64);
    # only VT_LPWSTR is used here.
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort * 3),
        ("pwszVal", ctypes.c_wchar_p),
        ("padding", ctypes.c_void_p),
    ]


# PKEY_AppUserModel_* (propkey.h)
_FMTID = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
PKEY_RELAUNCH_COMMAND = 2
PKEY_RELAUNCH_ICON_RESOURCE = 3
PKEY_RELAUNCH_DISPLAY_NAME_RESOURCE = 4
PKEY_ID = 5

IID_IPROPERTYSTORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
# IPropertyStore vtable: QueryInterface, AddRef, Release, GetCount, GetAt,
# GetValue, SetValue, Commit.
_RELEASE, _SET_VALUE, _COMMIT = 2, 6, 7


def set_process_app_id() -> None:
    """Before any window exists, so every window starts in the Phone group."""
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def set_window_identity(hwnd: int, relaunch_command: str, display_name: str, icon: str) -> None:
    """Tag one window with the app ID and how to start the app from a pin."""
    store = ctypes.c_void_p()
    ctypes.oledll.shell32.SHGetPropertyStoreForWindow(
        wintypes.HWND(hwnd), ctypes.byref(GUID(IID_IPROPERTYSTORE)), ctypes.byref(store)
    )
    vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))

    def method(index, *argtypes):
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])

    set_value = method(_SET_VALUE, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))
    try:
        # The relaunch properties only count alongside the ID; set it last.
        for pid, value in (
            (PKEY_RELAUNCH_COMMAND, relaunch_command),
            (PKEY_RELAUNCH_DISPLAY_NAME_RESOURCE, display_name),
            (PKEY_RELAUNCH_ICON_RESOURCE, icon),
            (PKEY_ID, APP_ID),
        ):
            key = PROPERTYKEY(GUID(_FMTID), pid)
            var = PROPVARIANT(vt=VT_LPWSTR, pwszVal=value)
            set_value(store, ctypes.byref(key), ctypes.byref(var))
        method(_COMMIT)(store)
    finally:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(store)
