"""Media audio (music, videos) from the phone to the PC, when asked for.

"Calls only" is the default: WirePlumber registers the Pi as a hands-free
unit and nothing else (51-phone-bridge.conf), so iOS keeps media on the
phone. "All audio" adds a drop-in, 52-phone-media.conf, that also
registers the Pi as an A2DP speaker (a2dp_sink). A later drop-in wins for
the same key - checked live: the Pi advertised Audio Sink, and the phone
opened an A2DP transport as soon as it reconnected. Switching means
restarting WirePlumber, which drops the phone's profiles for a moment, so
it's refused during a call.

While the phone plays, PipeWire exposes the A2DP stream as a bluez_input
node. The bridge records it at 48 kHz stereo (s16) in 10 ms chunks with a
10 ms node latency - pw-record's default is 100 ms, which alone would be
half the delay budget - and hands the chunks to every listener (the
desktop agent's media stream). Like the call bridge it starts pw-record
with --target 0 and links the ports itself.

Delay: the phone can hold video back by what the speaker reports
(A2DP delay reporting). The Pi only knows its own part, so the node gets
a latency offset for the rest - the network, the agent's buffer and
Windows' output - and PipeWire adds that to the delay it reports.
"""
import asyncio
import json
import logging
from pathlib import Path

from app.audio import _run, pair_ports, peak, ports_of

log = logging.getLogger("phone.media")

RATE = 48000
CHANNELS = 2
CHUNK_BYTES = RATE * CHANNELS * 2 // 100  # 10 ms
MEDIA_NODE = "phone-bridge-media"
# What happens after the Pi: LAN, the agent's ~60 ms start-up buffer and
# waveOut/Windows output. Reported to the phone on top of the Pi's own.
PC_LATENCY_NS = 100_000_000
# Per listener: past this many chunks (100 ms) a slow reader loses the
# oldest, so delay can't build up behind it.
QUEUE_CHUNKS = 10

MODES = ("calls", "all")
ROLES_FILE = Path.home() / ".config/wireplumber/wireplumber.conf.d/52-phone-media.conf"
ROLES_CONF = """\
# Written by the phone bridge while its audio mode is "All audio"; removed
# for "Calls only". Also registers the Pi as an A2DP speaker, so the phone
# sends music and videos here too. See apps/phone/app/media.py.
monitor.bluez.properties = {
  bluez5.roles = [ hfp_hf a2dp_sink ]
  # The iPhone sends media at full scale and sets the speaker's volume
  # (AVRCP absolute volume); ignored, as 51-phone-bridge.conf has it for
  # calls, music came out far too loud whatever the phone said. Honour it
  # for media only - the call streams keep ignoring the phone's volume.
  bluez5.enable-hw-volume = true
  bluez5.hw-volume = [ a2dp_sink ]
}
"""


def write_roles(mode: str, path: Path = ROLES_FILE) -> bool:
    """Make the drop-in match the mode; True if that changed anything."""
    if mode == "all":
        if path.exists() and path.read_text() == ROLES_CONF:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ROLES_CONF)
        return True
    if path.exists():
        path.unlink()
        return True
    return False


def find_media_node(dump: list) -> tuple[str, int] | None:
    """(node.name, id) of the phone's A2DP stream in pw-dump output."""
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        name = str(props.get("node.name", ""))
        if name.startswith("bluez_input.") and "a2dp" in str(props.get("api.bluez5.profile", "")):
            return name, obj["id"]
    return None


class MediaBridge:
    def __init__(self):
        self.listeners: set[asyncio.Queue] = set()
        self.proc: asyncio.subprocess.Process | None = None
        self.node: str | None = None
        self.peak = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(QUEUE_CHUNKS)
        self.listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.listeners.discard(q)

    def end_streams(self) -> None:
        """Tell every listener to stop (the mode went back to calls only)."""
        for q in list(self.listeners):
            self._offer(q, None)

    @staticmethod
    def _offer(q: asyncio.Queue, item) -> None:
        if q.full():
            q.get_nowait()  # drop the oldest: fresh audio beats complete audio
        q.put_nowait(item)

    async def _start(self, node: str, node_id: int) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            "pw-record", "--raw", "--rate", str(RATE), "--channels", str(CHANNELS), "--format", "s16",
            "--latency", "10ms", "--target", "0", "-P", f"{{ node.name = {MEDIA_NODE} }}", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(40):
            outs = ports_of(await _run("pw-link", "-o"), node)
            ins = ports_of(await _run("pw-link", "-i"), MEDIA_NODE)
            if outs and ins:
                for out_port, in_port in pair_ports(outs, ins):
                    await _run("pw-link", out_port, in_port)
                break
            await asyncio.sleep(0.05)
        else:
            await self._stop()
            raise RuntimeError(f"ports never appeared for {node}")
        try:
            await _run("pw-cli", "set-param", str(node_id), "Props", f"{{ latencyOffsetNsec: {PC_LATENCY_NS} }}")
        except RuntimeError as e:
            log.info("couldn't set the delay report offset: %s", e)
        self.node = node
        log.info("streaming media from %s", node)

    async def _stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
        self.node = None

    async def _pump(self) -> None:
        while self.proc:
            try:
                chunk = await self.proc.stdout.readexactly(CHUNK_BYTES)
            except (asyncio.IncompleteReadError, ConnectionResetError, AttributeError):
                break
            self.peak = max(self.peak, peak(chunk))
            for q in list(self.listeners):
                self._offer(q, chunk)
        await self._stop()

    async def run(self, enabled) -> None:
        """Record the phone's media stream while it exists, the mode is
        "all" (enabled()) and someone is listening; stop otherwise."""
        pump: asyncio.Task | None = None
        while True:
            try:
                found = None
                if enabled() and self.listeners:
                    found = find_media_node(json.loads(await _run("pw-dump")))
                if found and not self.proc:
                    await self._start(*found)
                    pump = asyncio.create_task(self._pump())
                elif self.proc and (not found or found[0] != self.node):
                    await self._stop()
                    if pump:
                        await pump
            except Exception:
                log.exception("media bridge")
            await asyncio.sleep(1)
