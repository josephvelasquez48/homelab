"""Move call audio between the phone's Bluetooth stream and the browser.

While a call's audio is on the Pi, PipeWire exposes it as two stream
nodes: bluez_input.<MAC>.N (the caller's voice, coming from the phone) and
bluez_output.<MAC>.N (what the phone should send to the caller). They
only exist while the SCO link is up, so they're looked up per call.

The bridge runs one pw-record and one pw-cat, both started with
--target 0 (don't auto-link) and then linked port-by-port with pw-link.
Nothing is left to WirePlumber's linking policy, because that policy is
what caused two bugs found on a live call: it linked the phone's input
to the Dummy Output and that sink's monitor back into the phone, echoing
the caller to themselves; and pw-record, when its target is missing,
silently falls back to recording the default sink - silence that looks
exactly like a broken call. See docs/phone.md.
"""
import array
import asyncio
import json
import logging

log = logging.getLogger("phone.audio")

RATE = 16000
CHUNK_BYTES = RATE * 2 // 50  # 20 ms of s16 mono
# Browser audio arrives at real time and pw-cat drains it at real time, but
# on two different clocks. Past this much backlog, drop instead of letting
# the caller hear the PC with ever-growing delay.
MAX_TX_BACKLOG = RATE * 2 // 4  # 250 ms

RX_NODE = "phone-bridge-rx"
TX_NODE = "phone-bridge-tx"


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed: {err.decode().strip()}")
    return out.decode()


def peak(pcm: bytes) -> int:
    """Largest absolute sample in s16le PCM (20 ms frames: cheap enough per chunk)."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    return max((abs(v) for v in samples), default=0)


def find_bluez_nodes(dump: list) -> dict[str, int]:
    """node.name -> id for the phone's call-audio streams in pw-dump output."""
    nodes = {}
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        name = props.get("node.name", "")
        if "a2dp" in str(props.get("api.bluez5.profile", "")):
            continue  # music/video (media.py), not the call
        if name.startswith(("bluez_input.", "bluez_output.")):
            nodes[name] = obj["id"]
    return nodes


def ports_of(listing: str, node: str) -> list[str]:
    """Ports of one node from `pw-link -o` / `pw-link -i` output, in order."""
    return [line.strip() for line in listing.splitlines() if line.strip().startswith(node + ":")]


def pair_ports(outs: list[str], ins: list[str]) -> list[tuple[str, str]]:
    """Which output port feeds which input port.

    The phone's streams are 2-channel (the mono call duplicated to FL/FR);
    the bridge's are mono. Mono out fans out to every input. Stereo into
    mono takes FL only - linking both would sum two identical channels
    and double the level.
    """
    if not outs or not ins:
        return []
    if len(outs) == 1:
        return [(outs[0], i) for i in ins]
    if len(ins) == 1:
        return [(outs[0], ins[0])]
    return list(zip(outs, ins))


class AudioBridge:
    def __init__(self):
        self.rx: asyncio.subprocess.Process | None = None
        self.tx: asyncio.subprocess.Process | None = None
        # Loudest sample each way since take_peaks(), for the metrics: a
        # bridged call whose tx stays ~0 is the "they can't hear me" case
        # (a sleeping or wrong mic), rx ~0 the "I can't hear them" one.
        self.rx_peak = 0
        self.tx_peak = 0

    def take_peaks(self) -> tuple[int, int]:
        peaks = (self.rx_peak, self.tx_peak)
        self.rx_peak = self.tx_peak = 0
        return peaks

    @property
    def running(self) -> bool:
        return self.rx is not None

    async def _link(self, src: str, dst: str) -> None:
        # The pw-cat nodes appear a moment after spawning; wait for ports.
        for _ in range(40):
            outs = ports_of(await _run("pw-link", "-o"), src)
            ins = ports_of(await _run("pw-link", "-i"), dst)
            if outs and ins:
                for out_port, in_port in pair_ports(outs, ins):
                    await _run("pw-link", out_port, in_port)
                return
            await asyncio.sleep(0.05)
        raise RuntimeError(f"ports never appeared for {src} -> {dst}")

    async def start(self) -> None:
        if self.running:
            return
        nodes = find_bluez_nodes(json.loads(await _run("pw-dump")))
        phone_in = next((n for n in nodes if n.startswith("bluez_input.")), None)
        phone_out = next((n for n in nodes if n.startswith("bluez_output.")), None)
        if not phone_in or not phone_out:
            raise RuntimeError("call audio is not on the Pi")
        # With bluez5.enable-hw-volume off the phone can no longer push its
        # own volume here, but the incoming stream still came up at 0.019
        # (-34 dB) on every call. That's the node's master "volume" prop;
        # `wpctl set-volume` only sets channelVolumes, and reported 1.00
        # while the call stayed near-silent. Set both.
        for node_id in nodes.values():
            await _run("wpctl", "set-volume", str(node_id), "1.0")
            await _run("pw-cli", "set-param", str(node_id), "Props", "{ volume: 1.0 }")

        fmt = ["--raw", "--rate", str(RATE), "--channels", "1", "--format", "s16", "--target", "0"]
        self.rx = await asyncio.create_subprocess_exec(
            "pw-record", *fmt, "-P", f"{{ node.name = {RX_NODE} }}", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        self.tx = await asyncio.create_subprocess_exec(
            "pw-cat", "-p", *fmt, "-P", f"{{ node.name = {TX_NODE} }}", "-",
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await self._link(phone_in, RX_NODE)
            await self._link(TX_NODE, phone_out)
        except Exception:
            await self.stop()
            raise
        log.info("bridging %s / %s", phone_in, phone_out)

    async def read(self) -> bytes:
        """Next 20 ms of the caller's voice; b"" once the bridge stops."""
        if not self.rx:
            return b""
        try:
            chunk = await self.rx.stdout.readexactly(CHUNK_BYTES)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return b""
        self.rx_peak = max(self.rx_peak, peak(chunk))
        return chunk

    def write(self, pcm: bytes) -> None:
        if not self.tx or self.tx.stdin.is_closing():
            return
        if self.tx.stdin.transport.get_write_buffer_size() > MAX_TX_BACKLOG:
            return
        self.tx_peak = max(self.tx_peak, peak(pcm))
        self.tx.stdin.write(pcm)

    async def stop(self) -> None:
        for proc in (self.rx, self.tx):
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    proc.kill()
        self.rx = self.tx = None
