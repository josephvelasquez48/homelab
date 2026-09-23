"""Pair a phone with the Pi as a Bluetooth hands-free device.

Run on the Pi (system python3 - uses the distro's python3-dbus/gi, no venv):

    python3 pair.py [seconds]

Opens a pairing window (default 120s): the adapter is made connectable +
discoverable and a BlueZ agent auto-confirms pairing requests - but only
while this script runs. The passkey is printed so it can be compared with
the one the phone shows before tapping Pair there. Every newly paired
device is marked Trusted, so it reconnects on its own later without this
script. Discoverable is switched back off on exit; connectable stays on,
because a paired phone needs it to reconnect.
"""

import sys

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS_NAME = "org.bluez"
AGENT_PATH = "/homelab/phone/agent"
ADAPTER_PATH = "/org/bluez/hci0"


class Agent(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, AGENT_PATH)
        self.bus = bus

    def _trust(self, device):
        props = dbus.Interface(self.bus.get_object(BUS_NAME, device), "org.freedesktop.DBus.Properties")
        props.Set("org.bluez.Device1", "Trusted", True)
        name = props.Get("org.bluez.Device1", "Alias")
        print(f"trusted {name} ({device})", flush=True)

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        print(f"pairing request from {device}: passkey {passkey:06d} - confirm it matches the phone", flush=True)
        self._trust(device)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        self._trust(device)

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        print(f"authorized service {uuid} for {device}", flush=True)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        print("pairing cancelled by the phone", flush=True)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    Agent(bus)
    manager = dbus.Interface(bus.get_object(BUS_NAME, "/org/bluez"), "org.bluez.AgentManager1")
    manager.RegisterAgent(AGENT_PATH, "DisplayYesNo")
    manager.RequestDefaultAgent(AGENT_PATH)

    adapter = dbus.Interface(bus.get_object(BUS_NAME, ADAPTER_PATH), "org.freedesktop.DBus.Properties")
    adapter.Set("org.bluez.Adapter1", "Powered", True)
    adapter.Set("org.bluez.Adapter1", "Connectable", True)
    adapter.Set("org.bluez.Adapter1", "Pairable", True)
    adapter.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(seconds))
    adapter.Set("org.bluez.Adapter1", "Discoverable", True)
    name = adapter.Get("org.bluez.Adapter1", "Alias")
    print(f"'{name}' is discoverable for {seconds}s - pair from the phone's Bluetooth settings", flush=True)

    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(seconds, loop.quit)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        adapter.Set("org.bluez.Adapter1", "Discoverable", False)
        manager.UnregisterAgent(AGENT_PATH)
        print("pairing window closed", flush=True)


if __name__ == "__main__":
    main()
