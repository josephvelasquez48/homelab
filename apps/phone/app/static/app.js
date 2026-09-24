// Phone page: call state + controls over one WebSocket, and the call's
// audio over the same socket as binary frames (16 kHz s16 mono, 20 ms).
const $ = (id) => document.getElementById(id);
// /popup is the compact view the desktop ring agent opens for a call:
// just the call card. It tries to close itself once the call is over;
// Firefox only honours that for windows a script opened.
const POPUP = location.pathname === "/popup";
// ?agent=1: embedded in the desktop agent's own window - as the compact
// call popup (/popup) or the full app (/) - which shows and hides itself,
// so no self-closing and no browser notifications.
const AGENT = new URLSearchParams(location.search).has("agent");
if (POPUP) document.body.classList.add("popup");
let popupHadCall = false;
let popupCloseTimer = null;
let ws = null;
let state = null;
let audio = null; // { ctx, capture, player, gain, stream, mic, micBus, denoise, analyser }

const MIC_OPTIONS = { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 };

// Which mic to use. Saved on the Pi as the device's *label* - Firefox and
// the ring agent's window give the same device different IDs, but the
// same label - so both use it, whatever Windows' default is (on this
// desktop that default is a silent Oculus virtual mic).
// Chromium (the agent's WebView2) appends a USB "(vid:pid)" to device
// labels and Firefox doesn't, so compare without it.
const micName = (label) => (label || "").replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)$/i, "");

async function micConstraints(label) {
  if (label) {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const match = devices.find((d) => d.kind === "audioinput" && micName(d.label) === micName(label));
    if (match) return { ...MIC_OPTIONS, deviceId: { exact: match.deviceId } };
  }
  return MIC_OPTIONS;
}

let gotFirstState;
const firstState = new Promise((resolve) => { gotFirstState = resolve; });

function savedMic() {
  return (state && state.settings && state.settings.micLabel) || "";
}
let muted = false;
let callStartedAt = {};
let ringer = null;

// ---- socket ---------------------------------------------------------------

function connect(delay = 500) {
  ws = new WebSocket(`wss://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    delay = 500;
    announceAudio();
  };
  ws.onmessage = (e) => {
    if (typeof e.data !== "string") {
      if (audio) audio.player.port.postMessage(e.data, [e.data]);
      return;
    }
    const msg = JSON.parse(e.data);
    if (msg.type === "state") { render(msg); gotFirstState(); }
    if (msg.type === "extras") renderExtras(msg);
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

// One start at a time: render() calls this on every state message while a
// call rings, and a second concurrent start would open a second
// AudioContext and mic stream.
let enabling = null;
function enableAudio() {
  if (!enabling) enabling = startAudio().finally(() => { enabling = null; });
  return enabling;
}

async function startAudio() {
  if (audio) {
    // Started without a click (auto-answer), Firefox leaves the context
    // suspended; any later click must resume it or the call stays silent.
    if (audio.ctx.state !== "running") {
      await audio.ctx.resume().catch(() => {});
      // Don't wait for the statechange event: the caller's next message
      // (answer/dial) must reach the Pi after "audio ready", not before.
      announceAudio();
    }
    return true;
  }
  try {
    // 48 kHz: the rate RNNoise works at. The capture worklet resamples
    // to the wire's 16 kHz from whatever the context runs at.
    const ctx = new AudioContext({ sampleRate: 48000 });
    ctx.resume().catch(() => {});
    await ctx.audioWorklet.addModule("/static/worklets.js");
    const denoise = await makeNoiseFilter(ctx);
    const stream = await navigator.mediaDevices.getUserMedia({ audio: await micConstraints(savedMic()) });
    const mic = ctx.createMediaStreamSource(stream);
    const capture = new AudioWorkletNode(ctx, "capture");
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    // The mic (swapped by switchMic) feeds micBus; routeMic puts the
    // noise filter between it and the capture/meter.
    const micBus = ctx.createGain();
    mic.connect(micBus);
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
    audio = { ctx, capture, player, gain, stream, mic, micBus, denoise, analyser };
    routeMic();
    listMics();
    followSavedMic();
    ctx.onstatechange = () => { announceAudio(); render(state); };
    if (!AGENT && "Notification" in window && Notification.permission === "default") Notification.requestPermission();
    announceAudio();
    drawMeter();
    render(state);
    return true;
  } catch (err) {
    showError(`Couldn't start PC audio: ${err.message}`);
    return false;
  }
}

// RNNoise in an AudioWorklet: removes steady background noise (a fan)
// from the mic far better than the browser's own noiseSuppression, which
// stays on under it. null if it can't load - the call works without it.
async function makeNoiseFilter(ctx) {
  try {
    const simd = WebAssembly.validate(new Uint8Array([
      0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0, 253, 15, 253, 98, 11,
    ]));
    const [wasmBinary] = await Promise.all([
      fetch(simd ? "/static/rnnoise_simd.wasm" : "/static/rnnoise.wasm").then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.arrayBuffer();
      }),
      ctx.audioWorklet.addModule("/static/rnnoise-worklet.js"),
    ]);
    return new AudioWorkletNode(ctx, "@sapphi-red/web-noise-suppressor/rnnoise", {
      processorOptions: { wasmBinary, maxChannels: 1 },
    });
  } catch (err) {
    console.warn("noise filter unavailable:", err);
    return null;
  }
}

// Always on when it loaded: there's no switch.
function routeMic() {
  const { micBus, denoise, capture, analyser } = audio;
  let out = micBus;
  if (denoise) {
    micBus.connect(denoise);
    out = denoise;
  }
  out.connect(capture);
  out.connect(analyser);
}

// Tell the Pi this page can take a call's audio - only once it's actually
// running. A context Firefox's autoplay policy left suspended can't play
// anything, and the Pi would otherwise send a call's audio to it.
// In the ring agent's window the mic opens while the call is still
// ringing (see render), but the page only announces itself once Answer
// was clicked there - otherwise answering on the iPhone would pull the
// call's audio to the PC.
let answeredHere = false;
function announceAudio() {
  if (AGENT && !answeredHere) return;
  if (audio && audio.ctx.state === "running") send({ action: "audio-ready" });
}

// Swap the mic under a running page, mid-call included: new stream in,
// old one stopped. The capture worklet doesn't notice.
async function switchMic(label, { save = true } = {}) {
  if (save) send({ action: "set-mic", value: label });
  if (!audio) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: await micConstraints(label) });
    const mic = audio.ctx.createMediaStreamSource(stream);
    mic.connect(audio.micBus);
    audio.mic.disconnect();
    audio.stream.getTracks().forEach((t) => t.stop());
    Object.assign(audio, { stream, mic });
  } catch (err) {
    showError(`Couldn't switch mic: ${err.message}`);
  }
  listMics();
}

// Labels are only visible once the page has mic permission, so this runs
// after audio is on (and again when a device is plugged in or removed).
async function listMics() {
  if (!audio) return;
  const inUse = micName(audio.stream.getAudioTracks()[0]?.label);
  // Chromium also lists "default"/"communications" aliases of real devices.
  const mics = (await navigator.mediaDevices.enumerateDevices()).filter(
    (d) => d.kind === "audioinput" && d.label && d.deviceId !== "default" && d.deviceId !== "communications",
  );
  const select = $("mic");
  select.replaceChildren(...mics.map((d) => {
    const o = document.createElement("option");
    o.textContent = micName(d.label);
    o.value = micName(d.label);
    o.selected = micName(d.label) === inUse;
    return o;
  }));
}
// Keep this page on the saved mic. The Samson on this desktop drops off
// USB when idle and comes back later; opened while it was gone, the page
// fell back to Windows' default (a silent Oculus virtual mic) and stayed
// there for the whole call. Now: say so on the page, and switch to the
// saved mic the moment it's back - on devicechange, and on a 3 s check in
// case that event never fires.
let following = false;
async function followSavedMic() {
  if (!audio || following) return;
  following = true;
  try {
    const want = micName(savedMic());
    const track = audio.stream.getAudioTracks()[0];
    const current = micName(track?.label);
    const live = track && track.readyState === "live";
    let missing = false;
    if (want && (current !== want || !live)) {
      const devices = await navigator.mediaDevices.enumerateDevices();
      if (devices.some((d) => d.kind === "audioinput" && micName(d.label) === want)) {
        await switchMic(want, { save: false });
      } else {
        missing = true;
        // The mic in use vanished too: take whatever Windows offers rather
        // than send nothing at all.
        if (!live) await switchMic("", { save: false });
      }
    }
    $("mic-warning").hidden = !missing;
    $("mic-warning").textContent = missing
      ? `${want} isn't connected right now (asleep or unplugged) - using ${micName(audio.stream.getAudioTracks()[0]?.label) || "the default mic"}. This switches back by itself when it's there.`
      : "";
  } finally {
    following = false;
  }
}
navigator.mediaDevices?.addEventListener?.("devicechange", () => { listMics(); followSavedMic(); });
setInterval(followSavedMic, 3000);

function drawMeter() {
  if (!audio) return;
  const data = new Uint8Array(audio.analyser.fftSize);
  audio.analyser.getByteTimeDomainData(data);
  let peak = 0;
  for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
  const width = `${muted ? 0 : Math.min(100, (peak / 128) * 300)}%`;
  $("meter").style.width = width;
  $("mic-meter").style.width = width;
  requestAnimationFrame(drawMeter);
}

// Ringtone: a marimba-style rising arpeggio (E5 G#5 B5 E6) twice, then a
// pause - the same pattern as the desktop ring agent (phone_agent.pyw).
// Not the 440+480 Hz ringback it used to be, which is what a caller hears.
const RINGTONE = { notes: [659.25, 830.61, 987.77, 1318.51], note: 0.13, cycle: 2.6 };

function strike(ctx, freq, t) {
  // Fundamental plus the bar's ~4x overtone, each with its own decay.
  for (const [mult, level, decay] of [[1, 0.12, 0.9], [3.9, 0.04, 0.25]]) {
    const o = ctx.createOscillator(); const g = ctx.createGain();
    o.frequency.value = freq * mult;
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(level, t + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, t + decay);
    o.connect(g).connect(ctx.destination);
    o.start(t); o.stop(t + decay + 0.05);
  }
}

function startRinging() {
  // The agent rings its own window; the page would only double it.
  if (ringer || !audio || AGENT) return;
  const { ctx } = audio;
  const ring = () => {
    const t0 = ctx.currentTime + 0.05;
    const n = RINGTONE.notes.length;
    for (let rep = 0; rep < 2; rep++) {
      RINGTONE.notes.forEach((f, i) => strike(ctx, f, t0 + (i + rep * (n + 1)) * RINGTONE.note));
    }
  };
  ring();
  ringer = setInterval(ring, RINGTONE.cycle * 1000);
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

  // Shown until audio is actually running: an auto-enabled context that
  // Firefox's autoplay policy left suspended still needs one click.
  const audioRunning = !!audio && audio.ctx.state === "running";
  $("audio-setup").hidden = audioRunning;
  $("enable-audio").textContent = audio ? "Turn on speakers" : "Enable PC mic & speakers";
  $("vol-row").hidden = !audio;
  $("mic-row").hidden = !audio;
  // Another page (or the other browser) may have picked a different mic.
  followSavedMic();
  $("audio-dot").className = `dot ${audio ? (s.bridged ? "on" : "") : "off"}`;
  $("audio-status").textContent = !audio
    ? "PC audio off - calls stay on the iPhone"
    : s.bridged ? "Call audio is on this PC" : "PC audio ready";
  if (s.audioError) showError(`Audio: ${s.audioError}`);
  if (s.settings) $("keep-phone").checked = !!s.settings.keepPhoneAnswered;

  const call = pickCall(s.calls);
  $("call-card").hidden = !call;
  $("dial-card").hidden = !!call || POPUP;
  if (POPUP) {
    // Opened for a call that was already answered elsewhere, or one that
    // just ended: nothing to show, so get out of the way.
    if (call) {
      popupHadCall = true;
      clearTimeout(popupCloseTimer);
      popupCloseTimer = null;
    } else if (!popupCloseTimer && !AGENT) {
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
  // Offered for any call whose audio isn't reaching a PC page: on the
  // iPhone - including a call answered on the phone, where PC audio isn't
  // on yet (the click turns it on) - or stuck on the Pi with no page
  // taking it, which happens after the phone reconnects mid-call.
  $("to-pc-row").hidden = !(call.state === "active" && !s.bridged);
  // Only from the page that has the call: elsewhere it would take the
  // audio away from whoever is actually talking on the PC.
  $("to-phone-row").hidden = !(call.state === "active" && s.bridged && audio && audio.ctx.state === "running");

  if (ringing) {
    // Wake the mic while it rings: the Samson on this desktop sleeps when
    // idle, and opening it is what wakes it. Opened at Answer, it wasn't
    // there in time and the call went out on the default (silent) mic.
    if (AGENT && !audio) enableAudio();
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

// ---- history, contacts --------------------------------------------------

function when(ts) {
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function duration(secs) {
  if (secs == null) return "";
  return secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function li(...children) {
  const el = document.createElement("li");
  el.append(...children);
  return el;
}

function div(text, cls) {
  const el = document.createElement("div");
  el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

function renderExtras(x) {
  const arrows = { in: "↙", out: "↗", unknown: "•" };
  $("recent").replaceChildren(...x.history.map((c) => {
    const who = document.createElement("div");
    who.className = "who";
    const label = c.missed ? "Missed" : c.direction === "out" ? "Outgoing" : c.direction === "in" ? "Incoming" : "Call";
    who.append(
      div(c.name || c.number || "Unknown", c.missed ? "missed" : ""),
      div(`${arrows[c.direction]} ${label} · ${when(c.started)}${c.seconds != null ? " · " + duration(c.seconds) : ""}${c.on_pc ? " · PC" : ""}`, "meta"),
    );
    const back = document.createElement("button");
    back.className = "small";
    back.textContent = "Call";
    back.disabled = !c.number;
    back.onclick = () => dialNumber(c.number);
    return li(who, back);
  }));
  $("recent-empty").hidden = x.history.length > 0;

  $("contacts-status").textContent = x.contactsError
    ? x.contactsError
    : x.contacts ? `${x.contacts} contact numbers synced from the iPhone` : "No contacts synced yet";
}

// Dial from this page: say "audio ready" before the dial, so the Pi lifts
// RejectSCO before the phone opens the call's audio link. The other order
// left an outgoing call's audio on the iPhone (seen live: connected, never
// bridged) - Answer and "Move call audio" already did it this way.
async function dialNumber(number) {
  answeredHere = true; // in the agent's window, dialing out claims the call like Answer does
  if (!(await enableAudio())) return;
  announceAudio();
  send({ action: "dial", number });
}

// The call the card is about. A ringing one comes first, so a call waiting
// behind an active call can be answered (or declined) from the card.
const CARD_ORDER = ["incoming", "waiting", "dialing", "alerting", "active", "held"];
function pickCall(calls) {
  return calls
    .filter((c) => c.state !== "disconnected")
    .sort((a, b) => CARD_ORDER.indexOf(a.state) - CARD_ORDER.indexOf(b.state))[0];
}

function currentCall() {
  return state && pickCall(state.calls);
}

$("enable-audio").onclick = enableAudio;
// Stored on the Pi, so it holds for every browser and across restarts.
$("keep-phone").onchange = (e) => send({ action: "set-keep-phone", value: e.target.checked });
$("mic").onchange = (e) => switchMic(e.target.value);
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
  answeredHere = true;
  if (!(await enableAudio())) return;
  announceAudio();
  send({ action: "answer", call: currentCall().path });
};
$("decline").onclick = () => send({ action: "hangup", call: currentCall().path });
$("hangup").onclick = () => send({ action: "hangup", call: currentCall().path });
$("to-phone").onclick = () => send({ action: "audio-to-phone" });
$("refresh-contacts").onclick = () => send({ action: "refresh-contacts" });
$("to-pc").onclick = async () => {
  answeredHere = true; // this window now holds the call's audio
  if (!(await enableAudio())) return;
  announceAudio(); // the Pi takes the audio only for a page that announced
  send({ action: "audio-to-pc" });
};
$("mute").onclick = (e) => {
  muted = !muted;
  e.target.setAttribute("aria-pressed", String(muted));
  e.target.textContent = muted ? "Unmute" : "Mute";
};
$("show-keypad").onclick = () => { $("tone-pad").hidden = !$("tone-pad").hidden; };

$("dial").onclick = () => {
  const number = $("number").value.trim();
  if (number) dialNumber(number);
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

// Turn audio on without the button when the mic is already granted for
// good - a remembered permission, not a one-off. Only checked, never
// requested, so opening the page can't pop a permission prompt. (The ring
// agent's window turns audio on when Answer is clicked instead.)
async function autoEnableAudio() {
  if (AGENT || !navigator.permissions) return;
  try {
    const mic = await navigator.permissions.query({ name: "microphone" });
    // Wait for the Pi's first state message: it carries the saved mic, and
    // starting before it would open Windows' default one instead.
    if (mic.state === "granted") { await firstState; await enableAudio(); }
  } catch {
    // Browser can't query mic permission: keep the button.
  }
}

connect();
autoEnableAudio();
