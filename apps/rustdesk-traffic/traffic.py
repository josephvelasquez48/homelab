#!/usr/bin/env python3
"""RustDesk's network traffic on the Pi, for the map on the Pi's display.

Every 10 s, sum the kernel's byte counters (`ss -ti`: bytes_acked out,
bytes_received in) over the established TCP connections of RustDesk's
processes - the Pi's own client (rustdesk: the remote sessions into the
Pi) and the ID and relay servers (hbbs, hbbr in docker/rustdesk) - and
write running totals to node_exporter's textfile collector, as the phone
bridge does. Prometheus's rate() turns them into the map's dots.

Only connections to other machines count: the Pi's client also talks to
its own ID server, and both ends of that are on the Pi. UDP is left out
(registration heartbeats, no per-socket counters); sessions run over TCP.

A socket's counters start at zero, so a new connection adds all it has
moved so far; one that closes between two samples loses at most its last
10 s. Run as root by rustdesk-traffic.service: ss shows another user's
processes (hbbs and hbbr run as root in containers) only to root.
"""
import os
import re
import subprocess
import time
from pathlib import Path

TEXTFILE = Path(os.environ.get("RUSTDESK_TEXTFILE", "/var/lib/node_exporter/textfile/rustdesk.prom"))
INTERVAL = 10
PROCESSES = ("rustdesk", "hbbs", "hbbr")

PROC_RE = re.compile(r'users:\(\("(%s)"' % "|".join(PROCESSES))


def host(addr: str) -> str:
    """The IP of an ss address column: [::ffff:1.2.3.4]:5 or 1.2.3.4:5 -> 1.2.3.4."""
    ip = addr.rsplit(":", 1)[0].strip("[]")
    return ip.removeprefix("::ffff:")


def local_ips() -> set[str]:
    out = subprocess.run(["ip", "-o", "addr"], capture_output=True, text=True).stdout
    ips = {line.split()[3].split("/")[0] for line in out.splitlines() if len(line.split()) > 3}
    return ips | {"127.0.0.1", "::1"}


def sample(ss_output: str, mine: set[str]) -> dict[tuple[str, str, str], tuple[int, int]]:
    """(process, local, peer) -> (bytes out, bytes in) for RustDesk's sockets to other machines."""
    found = {}
    lines = ss_output.splitlines()
    for head, info in zip(lines, lines[1:]):
        m = PROC_RE.search(head)
        if not m or not info.startswith(("\t", " ")):
            continue
        cols = head.split()
        local, peer = cols[2], cols[3]
        if host(peer) in mine:
            continue
        sent = re.search(r"\bbytes_acked:(\d+)", info)
        recv = re.search(r"\bbytes_received:(\d+)", info)
        found[(m.group(1), local, peer)] = (int(sent.group(1)) if sent else 0, int(recv.group(1)) if recv else 0)
    return found


def advance(totals: list[int], before: dict, now: dict) -> None:
    """Add each socket's growth since the last sample to totals [out, in]."""
    for key, (sent, recv) in now.items():
        was_sent, was_recv = before.get(key, (0, 0))
        # A reused address pair is a new socket: its counters restarted.
        totals[0] += sent - was_sent if sent >= was_sent else sent
        totals[1] += recv - was_recv if recv >= was_recv else recv


def render(sent: int, received: int) -> str:
    return (
        "# HELP rustdesk_sent_bytes_total Bytes RustDesk sent from the Pi to other machines (screen to the viewer, relayed sessions)\n"
        "# TYPE rustdesk_sent_bytes_total counter\n"
        f"rustdesk_sent_bytes_total {sent}\n"
        "# HELP rustdesk_received_bytes_total Bytes RustDesk received on the Pi from other machines (input, relayed sessions)\n"
        "# TYPE rustdesk_received_bytes_total counter\n"
        f"rustdesk_received_bytes_total {received}\n"
    )


def write(text: str, path: Path = TEXTFILE) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)  # atomic: node_exporter never reads half a file


def main() -> None:
    totals = [0, 0]
    before: dict = {}
    mine = local_ips()
    checked = time.monotonic()
    while True:
        if time.monotonic() - checked > 600:  # addresses change rarely (DHCP, IPv6)
            mine, checked = local_ips(), time.monotonic()
        out = subprocess.run(["ss", "-tinpH", "state", "established"], capture_output=True, text=True).stdout
        now = sample(out, mine)
        advance(totals, before, now)
        before = now
        write(render(*totals))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
