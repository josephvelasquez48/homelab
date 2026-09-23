// Phone page: call state + controls over one WebSocket, and the call's
// audio over the same socket as binary frames (16 kHz s16 mono, 20 ms).
const $ = (id) => document.getElementById(id);
// /popup is the compact view the desktop ring agent opens for a call:
// just the call card. It tries to close itself once the call is over;
// Firefox only honours that for windows a script opened.
const POPUP = location.pathname === "/popup";
if (POPUP) document.body.classList.add("popup");
let popupHadCall = false;
let popupCloseTimer = null;
let ws = null;
let state = null;
let audio = null; // { ctx, capture, player, gain, stream, analyser }
let muted = false;
let callStartedAt = {};
let ringer = null;

// ---- socket ---------------------------------------------------------------

function connect(delay = 500) {
  ws = new WebSocket(`wss://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    delay = 500;
    if (audio) send({ action: "audio-ready" });
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== "string") {
      if (audio) audio.player.port.postMessage(e.data, [e.data]);
      return;
    }
    const msg = JSON.parse(e.data);
    if (msg.type === "state") { render(msg); autoAnswer(); }
    if (msg.type === "error") showError(msg.message);
  };
  ws.onclose = (e) => {
    if (e.code === 4401) { location.reload(); return; } // session expired
    $("phone-status").textContent = "Lost connection to the Pi - retrying…";
    $("phone-dot").className = "dot off";
    setTimeout(() => connect(Math.min(delay * 2, 10000)), delay);
  };
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function showError(text) {
  $("error").textContent = text || "";
  if (text) setTimeout(() => { if ($("error").textContent === text) $("error").textContent = ""; }, 6000);
}

// ---- audio ------------------------------------------------------------------

async function enableAudio() {
  if (audio) {
    // Started without a click (auto-answer), Firefox leaves the context
    // suspended; any later click must resume it or the call stays silent.
    if (audio.ctx.state !== "running") await audio.ctx.resume().catch(() => {});
    return true;
  }
  try {
    const ctx = new AudioContext();
    ctx.resume().catch(() => {});
    await ctx.audioWorklet.addModule("/static/worklets.js");
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const mic = ctx.createMediaStreamSource(stream);
    const capture = new AudioWorkletNode(ctx, "capture");
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    mic.connect(capture);
    mic.connect(analyser);
    capture.port.onmessage = (e) => {
      if (!muted && ws && ws.readyState === WebSocket.OPEN && state && state.bridged) ws.send(e.data);
    };
    const player = new AudioWorkletNode(ctx, "player", { outputChannelCount: [2] });
    // The phone sends call audio quiet - peaks ~1000 of 32767 on a live
    // call, about -30 dBFS - so the default is 4x (+12 dB), with a limiter
    // after it so a loud caller at a high setting doesn't clip.
    const gain = ctx.createGain();
    gain.gain.value = Number($("volume").value);
    const limiter = ctx.createDynamicsCompressor();
    limiter.threshold.value = -3;
    limiter.knee.value = 0;
    limiter.ratio.value = 20;
    limiter.attack.value = 0.003;
    limiter.release.value = 0.15;
    player.connect(gain).connect(limiter).connect(ctx.destination);
    audio = { ctx, capture, player, gain, stream, analyser };
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    send({ action: "audio-ready" });
    drawMeter();
    render(state);
    return true;
  } catch (err) {
    showError(`Couldn't start PC audio: ${err.message}`);
    return false;
  }
}

function drawMeter() {
  if (!audio) return;
  const data = new Uint8Array(audio.analyser.fftSize);
  audio.analyser.getByteTimeDomainData(data);
  let peak = 0;
  for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
  $("meter").style.width = `${muted ? 0 : Math.min(100, (peak / 128) * 300)}%`;
  requestAnimationFrame(drawMeter);
}

function startRinging() {
  if (ringer || !audio) return;
  // Two-tone ring, 1 s on / 2 s off, built from oscillators so there's no file to load.
  const { ctx } = audio;
  const ring = () => {
    const t = ctx.currentTime;
    for (const f of [440, 480]) {
      const o = ctx.createOscillator(); const g = ctx.createGain();
      o.frequency.value = f; g.gain.value = 0.08;
      o.connect(g).connect(ctx.destination);
      o.start(t); o.stop(t + 1);
    }
  };
  ring();
  ringer = setInterval(ring, 3000);
}

function stopRinging() {
  clearInterval(ringer);
  ringer = null;
}

// ---- rendering ----------------------------------------------------------------

const LABELS = {
  incoming: "Incoming call", waiting: "Call waiting", dialing: "Calling…",
  alerting: "Ringing…", active: "On call", held: "On hold", disconnected: "Call ended",
};

function render(s) {
  if (!s) return;
  const prev = state;
  state = s;

  $("phone-dot").className = `dot ${s.connected ? "on" : "off"}`;
  $("phone-status").textContent = s.connected
    ? "iPhone connected"
    : "iPhone not connected - check Bluetooth on the phone";

  $("audio-setup").hidden = !!audio;
  $("vol-row").hidden = !audio;
  $("audio-dot").className = `dot ${audio ? (s.bridged ? "on" : "") : "off"}`;
  $("audio-status").textContent = !audio
    ? "PC audio off - calls stay on the iPhone"
    : s.bridged ? "Call audio is on this PC" : "PC audio ready";
  if (s.audioError) showError(`Audio: ${s.audioError}`);

  const call = s.calls.find((c) => c.state !== "disconnected");
  $("call-card").hidden = !call;
  $("dial-card").hidden = !!call || POPUP;
  if (POPUP) {
    // Opened for a call that was already answered elsewhere, or one that
    // just ended: nothing to show, so get out of the way.
    if (call) {
      popupHadCall = true;
      clearTimeout(popupCloseTimer);
      popupCloseTimer = null;
    } else if (!popupCloseTimer) {
      popupCloseTimer = setTimeout(() => window.close(), popupHadCall ? 1500 : 4000);
    }
    $("popup-idle").hidden = !!call;
  }
  if (!call) {
    stopRinging();
    document.title = "Phone";
    return;
  }

  const ringing = call.state === "incoming" || call.state === "waiting";
  $("call-card").classList.toggle("ringing", ringing);
  $("call-state").textContent = LABELS[call.state] || call.state;
  $("call-who").textContent = call.name || call.number || "Unknown caller";
  $("incoming-actions").hidden = !ringing;
  $("active-actions").hidden = ringing;
  $("meter-row").hidden = !(audio && s.bridged);
  $("to-pc-row").hidden = !(call.state === "active" && !s.audioOnPi && audio);

  if (ringing) {
    startRinging();
    document.title = `Incoming: ${call.name || call.number || "call"}`;
    const wasRinging = prev && prev.calls.some((c) => c.path === call.path && c.state === call.state);
    if (!wasRinging && document.hidden && "Notification" in window && Notification.permission === "granted") {
      new Notification("Incoming call", { body: call.name || call.number || "Unknown caller" });
    }
  } else {
    stopRinging();
    document.title = "Phone - on call";
  }
  if (call.state === "active" && !callStartedAt[call.path]) callStartedAt[call.path] = Date.now();
}

setInterval(() => {
  const call = state && state.calls.find((c) => c.state === "active");
  const started = call && callStartedAt[call.path];
  if (!started) { $("call-timer").textContent = ""; return; }
  const secs = Math.floor((Date.now() - started) / 1000);
  $("call-timer").textContent = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
}, 500);

// ---- controls -------------------------------------------------------------------

function currentCall() {
  return state && state.calls.find((c) => c.state !== "disconnected");
}

$("enable-audio").onclick = enableAudio;
// Remembered per browser; storage can be unavailable (private window),
// in which case the slider just starts at its default.
try {
  const saved = localStorage.getItem("phone-volume");
  if (saved !== null) $("volume").value = saved;
} catch {}
$("volume").oninput = (e) => {
  if (audio) audio.gain.gain.value = Number(e.target.value);
  try { localStorage.setItem("phone-volume", e.target.value); } catch {}
};

$("answer").onclick = async () => {
  // Answering from here should bring the audio here too, so make sure the
  // PC side is ready before the phone moves the call over.
  if (!(await enableAudio())) return;
  send({ action: "answer", call: currentCall().path });
};
$("decline").onclick = () => send({ action: "hangup", call: currentCall().path });
$("hangup").onclick = () => send({ action: "hangup", call: currentCall().path });
$("to-pc").onclick = async () => { if (await enableAudio()) send({ action: "audio-to-pc" }); };
$("mute").onclick = (e) => {
  muted = !muted;
  e.target.setAttribute("aria-pressed", String(muted));
  e.target.textContent = muted ? "Unmute" : "Mute";
};
$("show-keypad").onclick = () => { $("tone-pad").hidden = !$("tone-pad").hidden; };

$("dial").onclick = async () => {
  const number = $("number").value.trim();
  if (!number) return;
  if (!(await enableAudio())) return;
  send({ action: "dial", number });
};
$("number").addEventListener("keydown", (e) => { if (e.key === "Enter") $("dial").click(); });

for (const [padId, onKey] of [
  ["dial-pad", (k) => { $("number").value += k; }],
  ["tone-pad", (k) => send({ action: "tones", tones: k })],
]) {
  for (const k of ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]) {
    const b = document.createElement("button");
    b.textContent = k;
    b.onclick = () => onKey(k);
    $(padId).appendChild(b);
  }
}

// Opened by the ring agent's Answer button: turn on PC audio and answer
// without another click. Firefox only allows that without a click if this
// site may use the mic (remembered) and autoplay audio; if it can't, the
// normal Answer button is still there.
const AUTO_ANSWER = new URLSearchParams(location.search).has("answer");
let autoAnswered = false;

async function autoAnswer() {
  if (!AUTO_ANSWER || autoAnswered) return;
  const call = currentCall();
  if (!call || !(call.state === "incoming" || call.state === "waiting")) return;
  autoAnswered = true;
  history.replaceState(null, "", location.pathname);
  const ok = await enableAudio();
  if (ok && audio.ctx.state === "running") {
    send({ action: "answer", call: call.path });
  } else {
    showError("Click Answer to start the call on this PC.");
  }
}

connect();
