"""Metrics for Prometheus, via the Pi's node_exporter textfile collector.

The same route the backup metrics take (backup/publish-backup-metrics.py):
write /var/lib/node_exporter/textfile/phone_bridge.prom, which the
node-pi scrape job already collects - no new scrape target, port or ufw
rule for a host service. Written atomically (temp file + rename) so
node_exporter never reads half a file. Alert rules are in
kubernetes/monitoring/alertmanager.yaml.
"""
import os
import time
from pathlib import Path

TEXTFILE = Path(os.environ.get("PHONE_TEXTFILE", "/var/lib/node_exporter/textfile/phone_bridge.prom"))


def render(values: dict[str, float], counters: dict[tuple[str, str], int], help_text: dict[str, str]) -> str:
    lines = []
    for name, value in values.items():
        kind = "counter" if name.endswith("_total") else "gauge"
        # repr, not :g - :g keeps 6 significant digits, which turned the
        # update timestamp into 1.79013e+09 (~54 min "stale") and tripped
        # PhoneBridgeDown on a healthy bridge.
        text = repr(float(value)) if isinstance(value, float) else str(value)
        lines += [f"# HELP {name} {help_text.get(name, name)}", f"# TYPE {name} {kind}", f"{name} {text}"]
    name = "phone_calls_total"
    lines += [f"# HELP {name} Finished calls by direction and outcome, from the call history", f"# TYPE {name} counter"]
    for (direction, outcome), n in sorted(counters.items()):
        lines.append(f'{name}{{direction="{direction}",outcome="{outcome}"}} {n}')
    return "\n".join(lines) + "\n"


HELP = {
    "phone_bridge_last_update_timestamp_seconds": "When the phone bridge last wrote these metrics",
    "phone_connected": "1 if the iPhone is connected to the Pi as a hands-free unit",
    "phone_call_active": "1 while any call exists (ringing, dialing or in progress)",
    "phone_audio_on_pi": "1 while a call's audio link (SCO) is on the Pi",
    "phone_bridge_running": "1 while call audio is being bridged to a PC page",
    "phone_pages": "Phone pages connected (browser tabs and the ring agent's window)",
    "phone_audio_pages": "Pages with PC audio on",
    "phone_audio_rx_peak": "Peak caller audio level (0-32767) since the last write, while bridged",
    "phone_audio_tx_peak": "Peak PC mic level (0-32767) sent to the phone since the last write, while bridged",
    "phone_pc_present": "1 while the PC has checked in lately; while 0 the iPhone is kept disconnected",
    "phone_audio_mode_all": "1 while music and videos come to the PC too, 0 for calls only",
    "phone_media_listeners": "Media streams open to the PC (the desktop agent's player)",
    "phone_contacts": "Phone numbers known from the iPhone's phonebook",
    "phone_reconnect_attempts_total": "Times the Pi asked a disconnected iPhone to reconnect",
    "phone_reconnect_successes_total": "Reconnect attempts that connected",
}


def write(values: dict[str, float], counters: dict[tuple[str, str], int], path: Path = TEXTFILE) -> None:
    values = {"phone_bridge_last_update_timestamp_seconds": time.time(), **values}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render(values, counters, HELP))
    tmp.replace(path)
