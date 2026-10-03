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
// ?touch=1: the Pi's own touchscreen (phone-screen.service). A remote control
// - no speakers or mic there - so Answer sends the call to the PC.
const TOUCH = new URLSearchParams(location.search).has("touch");
if (POPUP) document.body.classList.add("popup");
if (TOUCH) document.body.classList.add("touch");
let popupHadCall = false;
let popupCloseTimer = null;
let ws = null;
let state = null;
let audio = null; // { ctx, capture, player, gain, stream, mic, micBus, denoise, gate, rawAnalyser, analyser }

// No automatic gain control: the Samson has a hardware gain knob, and AGC
// fighting it turned the mic down while talking and up in pauses, lifting
// the room's echo between sentences - callers said it sounded like a
// bathroom. The knob sets the level; nothing re-levels it.
const MIC_OPTIONS = { echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1 };

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
  if (text) PRESSABLE.forEach(clearPressed); // a failed action shouldn't leave a button stuck
}

// First letters of the first two words of a contact name ("Caroline" ->
// "C", "Joe Smith" -> "JS"); emoji and punctuation don't count.
function initials(name) {
  const words = (name || "").match(/\p{L}[\p{L}'-]*/gu) || [];
  return words.slice(0, 2).map((w) => w[0].toUpperCase()).join("");
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
    filterFailed = !denoise;
    const gate = new AudioWorkletNode(ctx, "gate", { outputChannelCount: [1] });
    const stream = await navigator.mediaDevices.getUserMedia({ audio: await micConstraints(savedMic()) });
    const mic = ctx.createMediaStreamSource(stream);
    const capture = new AudioWorkletNode(ctx, "capture");
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    // The mic (swapped by switchMic) feeds micBus; routeMic puts the
    // noise filter between it and the capture/meter.
    // Mono from here on: RNNoise filters only its first channel, and a
    // stereo mic (the Samson can be) left the second one unfiltered.
    const micBus = ctx.createGain();
    micBus.channelCount = 1;
    micBus.channelCountMode = "explicit";
    micBus.channelInterpretation = "speakers";
    mic.connect(micBus);
    // Before the filter, for "removing N dB" - the page's only evidence
    // that the filter is doing anything.
    const rawAnalyser = ctx.createAnalyser();
    rawAnalyser.fftSize = 2048;
    capture.port.onmessage = (e) => {
      // Sent even when muted: the phone service swaps in silence (hub.audio_in).
      if (ws && ws.readyState === WebSocket.OPEN && state && state.bridged) ws.send(e.data);
    };
    const player = new AudioWorkletNode(ctx, "player", { outputChannelCount: [2] });
    // Call volume in dB (the slider), with a limiter after it so a loud
    // caller at a high setting doesn't clip. The phone used to send calls
    // quiet (peaks ~1000 of 32767); since the Pi also registers as a
    // speaker it sends them near full scale, so the useful range is mostly
    // below 0 dB - and a straight 0-10 multiplier crammed it all into the
    // bottom tenth of the slider.
    const gain = ctx.createGain();
    gain.gain.value = dbToGain($("volume").value);
    const limiter = ctx.createDynamicsCompressor();
    limiter.threshold.value = -3;
    limiter.knee.value = 0;
    limiter.ratio.value = 20;
    limiter.attack.value = 0.003;
    limiter.release.value = 0.15;
    player.connect(gain).connect(limiter).connect(ctx.destination);
    analyser.fftSize = 2048;
    audio = { ctx, capture, player, gain, stream, mic, micBus, denoise, gate, rawAnalyser, analyser };
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
      outputChannelCount: [1],
    });
  } catch (err) {
    console.warn("noise filter unavailable:", err);
    return null;
  }
}

// Noise filter strength, per window: off (mic as is), normal (RNNoise) or
// strong (RNNoise, then a gate that mutes what's left between words).
// Not a wet/dry mix: RNNoise delays its output, and mixing that with the
// dry mic would comb-filter the voice.
let filterLevel = "normal";
try { filterLevel = localStorage.getItem("phone-noise-level") || "normal"; } catch {}
let filterFailed = false;

function routeMic() {
  const { micBus, denoise, gate, capture, analyser, rawAnalyser } = audio;
  for (const node of [micBus, denoise, gate]) {
    try { node && node.disconnect(); } catch {}
  }
  micBus.connect(rawAnalyser);
  let out = micBus;
  if (denoise && filterLevel !== "off") {
    out.connect(denoise);
    out = denoise;
    if (filterLevel === "strong") {
      out.connect(gate);
      out = gate;
    }
  }
  out.connect(capture);
  out.connect(analyser);
}

function setFilterLevel(level) {
  filterLevel = level;
  try { localStorage.setItem("phone-noise-level", level); } catch {}
  if (audio) routeMic();
  renderFilter();
  renderAudioSummary();
}

// Levels before and after the filter, smoothed over ~1 s, from drawMeter.
const filterMeter = { raw: -100, out: -100 };
function rmsDb(analyser) {
  const data = new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(data);
  let sum = 0;
  for (const v of data) sum += v * v;
  return 10 * Math.log10(sum / data.length + 1e-12);
}

function renderFilter() {
  for (const b of document.querySelectorAll("#filter-level button")) {
    b.setAttribute("aria-checked", String(b.dataset.level === filterLevel));
  }
  const name = { off: "Off", normal: "Normal", strong: "Strong" }[filterLevel];
  let dot = "";
  let text;
  if (filterFailed) {
    dot = "off";
    text = "Didn't load - the mic goes out unfiltered";
  } else if (filterLevel === "off") {
    text = "Off - the mic goes out as is";
  } else if (!audio) {
    text = `${name} - runs while the mic is on`;
  } else {
    dot = "on";
    const removed = filterMeter.raw - filterMeter.out;
    text = filterMeter.raw < -70 ? `${name} - listening` : `${name} - removing ${Math.max(0, Math.round(removed))} dB`;
  }
  $("filter-dot").className = `dot ${dot}`;
  $("filter-status").textContent = text;
}

// The agent's window keeps the mic only while there's a call: open while
// it rings (to wake the Samson) or once used, closed a few seconds after
// the last call ends - not left open, keeping the mic awake, until the
// window reloads. The delay covers a dial, whose call appears a moment
// after the mic opens.
let audioIdleTimer = null;
function releaseAudioWhenIdle(hasCall) {
  if (!AGENT) return;
  if (hasCall) {
    clearTimeout(audioIdleTimer);
    audioIdleTimer = null;
  } else if (audio && !audioIdleTimer) {
    audioIdleTimer = setTimeout(stopAudio, 5000);
  }
}

async function stopAudio() {
  audioIdleTimer = null;
  if (!audio || enabling) return;
  const closing = audio;
  audio = null;
  answeredHere = false;
  send({ action: "audio-off" }); // or the Pi would send the next call here
  closing.stream.getTracks().forEach((t) => t.stop());
  await closing.ctx.close().catch(() => {});
  filterMeter.raw = filterMeter.out = -100;
  render(state);
  renderFilter();
}

// Tell the Pi this page can take a call's audio - only once it's actually
// running. A context Firefox's autoplay policy left suspended can't play
// anything, and the Pi would otherwise send a call's audio to it.
// In the ring agent's window the mic opens while the call is still
// ringing (see render), but the page only announces itself once Answer
// was clicked there - otherwise answering on the iPhone would pull the
// call's audio to the PC.
let answeredHere = false;
let lastCallKey = "";
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

// A timer, not drawMeter's animation frames: those stop while the window
// is hidden or covered.
setInterval(() => {
  if (!audio) return;
  filterMeter.raw += (rmsDb(audio.rawAnalyser) - filterMeter.raw) * 0.3;
  filterMeter.out += (rmsDb(audio.analyser) - filterMeter.out) * 0.3;
  renderFilter();
}, 250);

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
  muted = !!s.muted;
  renderMute();
  // On the Pi's screen only while the PC has the call's audio: muting
  // silences what the PC sends, and there's no Bluetooth command to mute
  // the iPhone itself.
  $("mute").hidden = TOUCH && !s.bridged;

  $("phone-dot").className = `dot ${s.connected ? "on" : "off"}`;
  $("phone-status").textContent = s.connected
    ? "iPhone connected"
    : s.bluetooth === false ? "The Pi's Bluetooth is off"
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
  if (s.settings) $("media-on-pc").checked = !!s.settings.mediaOnPc;
  // The adapter itself, so this follows the display's Bluetooth button too.
  $("bluetooth-row").hidden = s.bluetooth == null;
  $("bluetooth").checked = !!s.bluetooth;
  renderAudioSummary();

  const call = pickCall(s.calls);
  // The call moved on (answered, ended, or a different call): any
  // "Answering…" / "Ending…" feedback is done.
  const callKey = call ? `${call.path}:${call.state}` : "";
  if (callKey !== lastCallKey) {
    lastCallKey = callKey;
    PRESSABLE.forEach(clearPressed);
  }
  // Answered on the Pi's touchscreen: this window (the PC's) takes the audio,
  // as if Answer had been clicked here. The Pi answers once we announce.
  if (AGENT && call && s.handoff === call.path && !answeredHere) {
    answeredHere = true;
    enableAudio().then((ok) => { if (ok) announceAudio(); });
  }
  releaseAudioWhenIdle(!!call);
  // A ringing call takes the whole pop-up (style.css, body.incoming);
  // once answered, the volume slider comes back underneath.
  document.body.classList.toggle("incoming", POPUP && !!call && (call.state === "incoming" || call.state === "waiting"));
  $("call-card").hidden = !call;
  $("dial-card").hidden = !!call || POPUP;
  if (POPUP) {
    // Opened for a call that was already answered elsewhere, or one that
    // just ended: nothing to show, so get out of the way.
    if (call) {
      popupHadCall = true;
      clearTimeout(popupCloseTimer);
      popupCloseTimer = null;
    } else if (!popupCloseTimer && !AGENT && !TOUCH) {
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
  $("call-avatar").textContent = initials(call.name) || "#";
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
  // Only on the Pi's screen: back to the homelab display for the rest of
  // this call (the screen's watcher closes this window; a new call brings it back).
  $("home-row").hidden = !TOUCH;

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
}

setInterval(() => {
  const call = state && state.calls.find((c) => c.state === "active");
  // When the phone service saw the call connect, so every page shows the
  // real length - not since this page opened, and never an earlier call's.
  const started = call && call.connectedAt;
  if (!started) { $("call-timer").textContent = ""; return; }
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - started));
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

// Recent calls show the latest few; the rest (the Pi sends up to 30) fold
// away behind "Show all". Folded again whenever the window reloads.
const RECENT_SHOWN = 3;
let recentExpanded = false;
let lastExtras = null;

function renderExtras(x) {
  lastExtras = x;
  const arrows = { in: "↙", out: "↗", unknown: "•" };
  const shown = recentExpanded ? x.history : x.history.slice(0, RECENT_SHOWN);
  const more = $("recent-more");
  more.hidden = x.history.length <= RECENT_SHOWN;
  more.textContent = recentExpanded ? "Show fewer" : `Show all (${x.history.length})`;
  more.setAttribute("aria-expanded", String(recentExpanded));
  $("recent").replaceChildren(...shown.map((c) => {
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
$("media-on-pc").onchange = (e) => send({ action: "set-media-on-pc", value: e.target.checked });
$("bluetooth").onchange = (e) => {
  const on = e.target.checked;
  if (!on && !confirm("Turn off the Pi's Bluetooth? The iPhone disconnects: no calls or music through the Pi until it's back on.")) {
    e.target.checked = true;
    return;
  }
  send({ action: "set-bluetooth", value: on });
};
$("mic").onchange = (e) => switchMic(e.target.value);
function dbToGain(db) {
  return Math.pow(10, Number(db) / 20);
}
function renderVolume() {
  const db = Number($("volume").value);
  $("volume-db").textContent = `${db > 0 ? "+" : ""}${db} dB`;
}
// Remembered per browser; storage can be unavailable (private window),
// in which case the slider just starts at its default. A new key: the old
// "phone-volume" was a 0-10 multiplier, meaningless as dB.
try {
  const saved = localStorage.getItem("phone-volume-db");
  if (saved !== null) $("volume").value = saved;
} catch {}
renderVolume();
$("volume").oninput = (e) => {
  if (audio) audio.gain.gain.value = dbToGain(e.target.value);
  renderVolume();
  try { localStorage.setItem("phone-volume-db", e.target.value); } catch {}
};

// Instant feedback on a tap: the button dims and says what's happening,
// and repeat taps are ignored, until the call's next state arrives (render
// clears it) or 8 s pass. Without it, a tap looked like nothing happened
// until the phone responded.
const PRESSABLE = ["answer", "decline", "hangup"];
function pressed(id, label) {
  const b = $(id);
  if (!b.dataset.label) b.dataset.label = b.querySelector("span").textContent;
  b.querySelector("span").textContent = label;
  b.classList.add("pending");
  b.disabled = true;
  clearTimeout(b._pendingTimer);
  b._pendingTimer = setTimeout(() => clearPressed(id), 8000);
}
function clearPressed(id) {
  const b = $(id);
  if (!b.classList.contains("pending")) return;
  b.classList.remove("pending");
  b.disabled = false;
  if (b.dataset.label) b.querySelector("span").textContent = b.dataset.label;
}

$("answer").onclick = async () => {
  pressed("answer", "Answering…");
  if (TOUCH) {
    // The Pi's screen: the PC's window takes the audio, then the Pi answers.
    send({ action: "answer-on-pc", call: currentCall().path });
    return;
  }
  // Answering from here should bring the audio here too, so make sure the
  // PC side is ready before the phone moves the call over.
  answeredHere = true;
  if (!(await enableAudio())) return;
  announceAudio();
  send({ action: "answer", call: currentCall().path });
};
$("decline").onclick = () => { pressed("decline", "Declining…"); send({ action: "hangup", call: currentCall().path }); };
$("hangup").onclick = () => { pressed("hangup", "Ending…"); send({ action: "hangup", call: currentCall().path }); };
$("to-phone").onclick = () => send({ action: "audio-to-phone" });
$("screen-home").onclick = () => { $("screen-home").disabled = true; send({ action: "screen-home" }); };
$("refresh-contacts").onclick = () => send({ action: "refresh-contacts" });
$("recent-more").onclick = () => {
  recentExpanded = !recentExpanded;
  if (lastExtras) renderExtras(lastExtras);
};
$("to-pc").onclick = async () => {
  answeredHere = true; // this window now holds the call's audio
  if (!(await enableAudio())) return;
  announceAudio(); // the Pi takes the audio only for a page that announced
  send({ action: "audio-to-pc" });
};
// Mute is the phone service's, shared by every page: the Pi's screen can
// mute a call whose mic is on the PC. Shown at once here, confirmed by the
// next state.
function renderMute() {
  $("mute").setAttribute("aria-pressed", String(muted));
  $("mute-label").textContent = muted ? "Unmute" : "Mute";
}
$("mute").onclick = () => {
  muted = !muted;
  renderMute();
  send({ action: "set-mute", value: muted });
};
$("show-keypad").onclick = () => { $("tone-pad").hidden = !$("tone-pad").hidden; };

// The dial pad stays folded away until switched on; remembered per window.
function showDialer(on, focus = false) {
  $("show-dialer").checked = on;
  $("dialer").hidden = !on;
  try { localStorage.setItem("phone-dialer", on ? "on" : "off"); } catch {}
  if (on && focus) $("number").focus();
}
try { showDialer(localStorage.getItem("phone-dialer") === "on"); } catch { showDialer(false); }
$("show-dialer").onchange = (e) => showDialer(e.target.checked, true);

// The Audio section folds to one summary line; open or closed is
// remembered per window, and it starts closed.
function showAudioSection(open) {
  $("audio-body").hidden = !open;
  $("audio-toggle").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem("phone-audio-open", open ? "1" : "0"); } catch {}
}
function renderAudioSummary() {
  const media = state && state.settings && state.settings.mediaOnPc ? "Music on PC" : "Music on iPhone";
  const filter = { off: "Off", normal: "Normal", strong: "Strong" }[filterLevel];
  $("audio-summary").textContent = `${media} · Filter: ${filter}`;
}
try { showAudioSection(localStorage.getItem("phone-audio-open") === "1"); } catch { showAudioSection(false); }
$("audio-toggle").onclick = () => showAudioSection($("audio-body").hidden);
for (const b of document.querySelectorAll("#filter-level button")) b.onclick = () => setFilterLevel(b.dataset.level);
renderFilter();

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
  if (AGENT || TOUCH || !navigator.permissions) return;
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
