"""Play the phone's music and videos on the PC, in "All audio" mode.

The Pi streams the phone's A2DP audio as raw PCM from /api/agent/media
(48 kHz stereo s16, 10 ms chunks). This plays it through Windows' waveOut
- plain ctypes, so the agent's venv needs nothing new - on the default
output device. It runs in its own thread for the agent's whole life: in
"Calls only" mode the Pi answers 204 and this asks again every few
seconds; the call window's audio is separate (it's the page's, for echo
cancellation).

Delay is kept short on purpose: playback starts once PRIME_CHUNKS are
queued, and past MAX_CHUNKS queued (the Pi's clock and the sound card's
drift apart, or the network hiccuped) a chunk is dropped instead of the
delay growing. After a gap - the phone paused - it primes again.
"""
import ctypes
import logging
import socket
import threading
import time
import urllib.request
from ctypes import wintypes

log = logging.getLogger("phone-agent.media")

RATE = 48000
CHANNELS = 2
CHUNK_BYTES = RATE * CHANNELS * 2 // 100  # 10 ms, as the Pi sends it
PRIME_CHUNKS = 6  # 60 ms queued before playback starts
MAX_CHUNKS = 12  # 120 ms: beyond this, drop instead of lagging
RETRY_SECONDS = 5

WAVE_MAPPER = 0xFFFFFFFF  # the default output device
WAVE_FORMAT_PCM = 1
WHDR_DONE = 0x1

winmm = ctypes.WinDLL("winmm")


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [
    ("lpData", ctypes.c_void_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_size_t),
    ("dwFlags", wintypes.DWORD),
    ("dwLoops", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(WAVEHDR)),
    ("reserved", ctypes.c_size_t),
]
HDR_SIZE = ctypes.sizeof(WAVEHDR)

_P_HDR = ctypes.POINTER(WAVEHDR)
winmm.waveOutOpen.argtypes = [
    ctypes.POINTER(wintypes.HANDLE), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX), ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD,
]
for _name in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
    getattr(winmm, _name).argtypes = [wintypes.HANDLE, _P_HDR, wintypes.UINT]
winmm.waveOutReset.argtypes = [wintypes.HANDLE]
winmm.waveOutClose.argtypes = [wintypes.HANDLE]


def _check(result: int, what: str) -> None:
    if result:
        raise OSError(f"{what} failed (MMSYSERR {result})")


class WaveOut:
    """A waveOut device fed 10 ms chunks, with a short, bounded queue."""

    def __init__(self):
        fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, CHANNELS, RATE, RATE * CHANNELS * 2, CHANNELS * 2, 16, 0)
        self.handle = wintypes.HANDLE()
        _check(winmm.waveOutOpen(ctypes.byref(self.handle), WAVE_MAPPER, ctypes.byref(fmt), 0, 0, 0), "waveOutOpen")
        self.queued: list[tuple[WAVEHDR, ctypes.Array]] = []  # buffers kept alive while Windows plays them
        self.priming: list[bytes] = []
        self.dropped = 0

    def _reap(self) -> None:
        still = []
        for hdr, buf in self.queued:
            if hdr.dwFlags & WHDR_DONE:
                winmm.waveOutUnprepareHeader(self.handle, ctypes.byref(hdr), HDR_SIZE)
            else:
                still.append((hdr, buf))
        self.queued = still

    def _write(self, chunk: bytes) -> None:
        buf = ctypes.create_string_buffer(chunk, len(chunk))
        hdr = WAVEHDR(ctypes.cast(buf, ctypes.c_void_p), len(chunk), 0, 0, 0, 0, None, 0)
        _check(winmm.waveOutPrepareHeader(self.handle, ctypes.byref(hdr), HDR_SIZE), "waveOutPrepareHeader")
        _check(winmm.waveOutWrite(self.handle, ctypes.byref(hdr), HDR_SIZE), "waveOutWrite")
        self.queued.append((hdr, buf))

    def play(self, chunk: bytes) -> None:
        self._reap()
        if not self.queued:
            # Starting, or ran dry (the phone paused): build a small
            # cushion first, or network jitter would click.
            self.priming.append(chunk)
            if len(self.priming) >= PRIME_CHUNKS:
                for c in self.priming:
                    self._write(c)
                self.priming = []
            return
        if len(self.queued) >= MAX_CHUNKS:
            self.dropped += 1
            return
        self._write(chunk)

    def close(self) -> None:
        winmm.waveOutReset(self.handle)
        self._reap()
        winmm.waveOutClose(self.handle)


class MediaPlayer:
    def __init__(self, pi):
        self.pi = pi  # url, token, context - the agent's connection to the Pi
        self.playing = False

    def start(self) -> None:
        threading.Thread(target=self._run, name="media", daemon=True).start()

    def _run(self) -> None:
        last_error = None
        while True:
            try:
                self._stream()
                last_error = None
            except (socket.timeout, TimeoutError):
                continue  # nothing played for a while: just reconnect
            except Exception as e:
                if str(e) != last_error:
                    log.warning("media stream: %s", e)
                    last_error = str(e)
            self.playing = False
            time.sleep(RETRY_SECONDS)

    def _stream(self) -> None:
        req = urllib.request.Request(
            f"{self.pi.url}/api/agent/media", headers={"Authorization": f"Bearer {self.pi.token}"}
        )
        # The read timeout only ends an idle stream (nothing playing); it's
        # reopened straight away.
        with urllib.request.urlopen(req, context=self.pi.context, timeout=60) as resp:
            if resp.status == 204:
                return  # "Calls only"
            out = WaveOut()
            log.info("media stream open")
            try:
                while chunk := resp.read(CHUNK_BYTES):
                    self.playing = True
                    out.play(chunk)
            finally:
                if out.dropped:
                    log.info("media: dropped %d late chunks", out.dropped)
                out.close()
                self.playing = False
                log.info("media stream closed")
