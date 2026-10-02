// The live view: a WHEP client. Sends a WebRTC offer to /api/whep (the app
// relays it to MediaMTX on the Mac) and plays what comes back. The video
// flows straight from the Mac; this page and the app only set it up.

import { fillNav, me } from "/static/common.js";

const $ = (id) => document.getElementById(id);
const video = $("video");
const stage = $("stage");

let pc = null;
let sessionUrl = null;
let retryTimer = null;
let retryDelay = 2000;
let statsTimer = null;
let hiddenTimer = null;
let wanted = true;  // false while the tab is in the background

function setStatus(kind, label) {
  const pill = $("status");
  pill.className = `pill ${kind}`;
  pill.textContent = label;
}

function showOverlay({ title, sub = "", spinner = false, retry = false }) {
  $("overlay").hidden = false;
  $("overlay-title").textContent = title;
  $("overlay-sub").textContent = sub;
  $("overlay-sub").hidden = !sub;
  $("spinner").hidden = !spinner;
  $("retry").hidden = !retry;
}

function waitForIce(peer, ms) {
  // Send the offer with our candidates in it rather than trickling them:
  // one request instead of several, and on a LAN gathering takes ~50 ms.
  if (peer.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { clearTimeout(timer); peer.removeEventListener("icegatheringstatechange", check); resolve(); };
    const check = () => { if (peer.iceGatheringState === "complete") done(); };
    const timer = setTimeout(done, ms);
    peer.addEventListener("icegatheringstatechange", check);
  });
}

async function start() {
  clearTimeout(retryTimer);
  stop();
  setStatus("connecting", "Connecting");
  showOverlay({ title: "Connecting to the camera…", spinner: true });

  const peer = new RTCPeerConnection();
  pc = peer;
  peer.addTransceiver("video", { direction: "recvonly" });
  peer.addEventListener("track", (event) => {
    video.srcObject = event.streams[0] || new MediaStream([event.track]);
  });
  peer.addEventListener("connectionstatechange", () => {
    if (peer !== pc) return;
    if (peer.connectionState === "connected") {
      retryDelay = 2000;
      setStatus("live", "Live");
      $("overlay").hidden = true;
      startStats();
    } else if (peer.connectionState === "failed") {
      fail("Lost the stream", "Reconnecting…");
    } else if (peer.connectionState === "disconnected") {
      // Often a Wi-Fi blip that recovers by itself; give it a few seconds.
      setTimeout(() => {
        if (peer === pc && peer.connectionState === "disconnected") fail("Lost the stream", "Reconnecting…");
      }, 4000);
    }
  });

  try {
    await peer.setLocalDescription(await peer.createOffer());
    await waitForIce(peer, 2000);
    const res = await fetch("/api/whep", {
      method: "POST",
      headers: { "Content-Type": "application/sdp", "X-Requested-With": "cam" },
      body: peer.localDescription.sdp,
    });
    if (res.status === 401) { location.replace("/login"); return; }
    if (!res.ok) {
      let detail = `The camera server answered ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    sessionUrl = res.headers.get("Location");
    const answer = await res.text();
    if (peer !== pc) return;  // replaced while we waited
    await peer.setRemoteDescription({ type: "answer", sdp: answer });
  } catch (err) {
    if (peer === pc) fail("Camera offline", err.message);
  }
}

function fail(title, sub) {
  stop();
  setStatus("offline", "Offline");
  const seconds = Math.round(retryDelay / 1000);
  const sentence = /[.!?…]$/.test(sub) ? sub : `${sub}.`;
  showOverlay({ title, sub: `${sentence} Retrying in ${seconds} s.`, retry: true });
  if (wanted) retryTimer = setTimeout(start, retryDelay);
  retryDelay = Math.min(retryDelay * 2, 30000);
}

function stop() {
  clearInterval(statsTimer);
  $("stats").textContent = "";
  if (sessionUrl) {
    // keepalive lets this finish even as the page unloads.
    fetch(sessionUrl, { method: "DELETE", headers: { "X-Requested-With": "cam" }, keepalive: true }).catch(() => {});
    sessionUrl = null;
  }
  if (pc) {
    const old = pc;
    pc = null;
    old.close();
  }
  video.srcObject = null;
}

// Resolution, frame rate and how far behind real time the picture is.
function startStats() {
  clearInterval(statsTimer);
  let last = null;
  statsTimer = setInterval(async () => {
    if (!pc) return;
    const report = await pc.getStats();
    report.forEach((s) => {
      if (s.type !== "inbound-rtp" || s.kind !== "video") return;
      const parts = [];
      if (s.frameHeight) parts.push(`${s.frameHeight}p`);
      if (s.framesPerSecond) parts.push(`${Math.round(s.framesPerSecond)} fps`);
      if (last && s.jitterBufferEmittedCount > last.jitterBufferEmittedCount) {
        const delay = (s.jitterBufferDelay - last.jitterBufferDelay) /
          (s.jitterBufferEmittedCount - last.jitterBufferEmittedCount);
        parts.push(`${Math.round(delay * 1000)} ms buffer`);
      }
      last = s;
      $("stats").textContent = parts.join(" · ");
    });
  }, 2000);
}

function snapshot() {
  if (!video.videoWidth) return;
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  canvas.toBlob((blob) => {
    const stamp = new Date().toISOString().slice(0, 19).replace("T", "-").replaceAll(":", "");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `camera-${stamp}.jpg`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  }, "image/jpeg", 0.92);
  const flash = $("flash");
  flash.classList.remove("go");
  void flash.offsetWidth;  // restart the animation
  flash.classList.add("go");
}

function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen();
  else if (stage.requestFullscreen) stage.requestFullscreen();
  else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();  // iPhone Safari
}

// A background tab doesn't need the camera on. Stop after 30 s hidden,
// so a quick app switch doesn't drop the stream, and start again on return.
document.addEventListener("visibilitychange", () => {
  clearTimeout(hiddenTimer);
  if (document.hidden) {
    hiddenTimer = setTimeout(() => {
      wanted = false;
      clearTimeout(retryTimer);
      stop();
      setStatus("connecting", "Paused");
      showOverlay({ title: "Paused while the tab was in the background" });
    }, 30000);
  } else if (!wanted) {
    wanted = true;
    start();
  }
});
window.addEventListener("pagehide", stop);

$("retry").addEventListener("click", () => { retryDelay = 2000; start(); });
$("snapshot").addEventListener("click", snapshot);
$("fullscreen").addEventListener("click", toggleFullscreen);
video.addEventListener("dblclick", toggleFullscreen);
// Touch: a tap shows the controls for a few seconds.
let controlsTimer = null;
stage.addEventListener("touchstart", () => {
  stage.classList.add("show-controls");
  clearTimeout(controlsTimer);
  controlsTimer = setTimeout(() => stage.classList.remove("show-controls"), 3000);
}, { passive: true });

me().then((user) => { fillNav(user); start(); }).catch(() => {});
