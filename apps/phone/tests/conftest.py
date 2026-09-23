import os

# The app module wires real contacts/history/reconnect into its hub
# at import unless told not to; tests must not touch Bluetooth, D-Bus or disk.
os.environ.setdefault("PHONE_EXTRAS", "0")
