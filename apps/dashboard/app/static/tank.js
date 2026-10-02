// The cluster aquarium on the Pi's display, as a coral reef seen side-on.
// One fish per pod: size is memory in use, speed is CPU, and colour and
// species are the namespace; a pod near its memory limit puffs up into a
// pufferfish, a sick one floats belly-up, a pending one sinks. One sandcastle
// per node, its windows lit while the node is ready.
//
// 2.5D: layers at different depths - far reef in the haze, weed at the back,
// the sand with its coral and castles, the fish, and weed in front of the
// glass - slide at different speeds as the view drifts, and that difference
// reads as depth. The sky and water follow the real weather and time of day;
// ?wx=rain&phase=night (or clear/partly/overcast/fog/drizzle/snow/storm,
// dawn/day/dusk) previews them. The night moon shows the real phase;
// &moon=0.25 (0 new, 0.5 full) previews one.
//
// display.js owns the page: it polls /api/display, hands each state to
// Tank.sync, and calls Tank.draw every frame while the aquarium is showing.
(() => {
"use strict";

const NS_COLOR = { backend: "#5DCAA5", data: "#EF9F27", monitoring: "#AFA9EC", argocd: "#F0997B", chat: "#97C459",
  kiwix: "#85B7EB", ai: "#ED93B1", dashboard: "#FAC775", "kube-system": "#E6F1FB" };
// The same namespaces in neon, for night: saturated and bright, and still
// apart from each other (and from the warning orange and red of a puffed
// fish). nsColor() blends toward these through dusk and dawn.
const NS_NEON = { backend: "#00FFB0", data: "#FFB21F", monitoring: "#B47CFF", argocd: "#FF5A8C", chat: "#B6FF2E",
  kiwix: "#2ED8FF", ai: "#FF3EE0", dashboard: "#FFF23A", "kube-system": "#9DF4FF" };
let nightK = 0; // how much night it is this frame, 0-1 (set by drawTank)
function nsColor(ns) {
  const day = NS_COLOR[ns] || "#D3D1C7";
  return nightK > 0 ? mixHex(day, NS_NEON[ns] || "#E8F6FF", nightK) : day;
}
const NS_SPECIES = { backend: "classic", "kube-system": "classic", data: "ray", monitoring: "angel", ai: "angel",
  argocd: "slender", chat: "slender", dashboard: "tang", kiwix: "tang" };
const MIB = 2 ** 20;
const PUFF_AT = 0.85;
const SURF = 0.245; // the water's surface, as a fraction of the height
const FLOOR = 0.86; // where the sand starts
const TOP_BAR = 0.078;
const params = new URLSearchParams(location.search);
const rand = (a, b) => a + Math.random() * (b - a);

const canvas = document.getElementById("tank"), ctx = canvas.getContext("2d", { alpha: false });
let W = 0, H = 0;
let state = null; // the latest /api/display, handed over by display.js
function sizeCanvas() {
  const r = canvas.getBoundingClientRect();
  W = canvas.width = Math.round(r.width);
  H = canvas.height = Math.round(r.height);
  farKey = reefKey = "";
}

// ---- Colour ----

function hexRgb(h) { return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)); }
function mix(a, b, t) {
  const x = hexRgb(a), y = hexRgb(b);
  return `rgb(${x.map((v, i) => Math.round(v + (y[i] - v) * t)).join(",")})`;
}
function mixHex(a, b, t) {
  const x = hexRgb(a), y = hexRgb(b);
  return "#" + x.map((v, i) => Math.round(v + (y[i] - v) * t).toString(16).padStart(2, "0")).join("");
}

// ---- Weather (as the 2D tank) ----

function wxKind(code) {
  if (code == null || code <= 1) return "clear";
  if (code === 2) return "partly";
  if (code === 3) return "overcast";
  if (code === 45 || code === 48) return "fog";
  if (code >= 51 && code <= 57) return "drizzle";
  if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return "rain";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "snow";
  if (code >= 95) return "storm";
  return "clear";
}
function dayPhase(w, nowS) {
  if (w && w.sunrise && w.sunset) {
    const twilight = 45 * 60;
    if (Math.abs(nowS - w.sunrise) < twilight) return "dawn";
    if (Math.abs(nowS - w.sunset) < twilight) return "dusk";
    return nowS > w.sunrise && nowS < w.sunset ? "day" : "night";
  }
  if (w) return w.is_day ? "day" : "night";
  const h = new Date().getHours();
  return h >= 6 && h < 19 ? "day" : "night";
}
// dim: how much the sand, rocks and weed darken toward the deep colour.
const PHASES = {
  day: { sky: ["#5E9AD6", "#A9CFEF"], top: "#1592BF", deep: "#03355A", ray: "#CFE8FF", grey: "#8A929B", dim: 0 },
  dawn: { sky: ["#5E5588", "#F2A07B"], top: "#3C5D8F", deep: "#06203F", ray: "#FFD2A8", grey: "#77737D", dim: 0.35 },
  dusk: { sky: ["#35336A", "#E8845A"], top: "#34507F", deep: "#051A35", ray: "#FFC08A", grey: "#5E5A66", dim: 0.4 },
  night: { sky: ["#040A1A", "#13224A"], top: "#0B2447", deep: "#020A18", ray: "#B5C8E8", grey: "#1C222C", dim: 0.62 },
};
const KINDS = {
  clear: { grey: 0, rays: 1, clouds: 0 },
  partly: { grey: 0.15, rays: 0.75, clouds: 4 },
  overcast: { grey: 0.7, rays: 0.2, clouds: 9 },
  fog: { grey: 0.6, rays: 0.15, clouds: 0 },
  drizzle: { grey: 0.65, rays: 0.15, clouds: 8 },
  rain: { grey: 0.8, rays: 0.08, clouds: 10 },
  snow: { grey: 0.55, rays: 0.12, clouds: 8 },
  storm: { grey: 0.9, rays: 0, clouds: 11 },
};
function scene() {
  const w = state && state.weather;
  const nowS = Date.now() / 1000;
  const kind = params.get("wx") || wxKind(w && w.code);
  const phase = params.get("phase") || dayPhase(w, nowS);
  const cover = w && w.cloud_cover != null && !params.get("wx") ? w.cloud_cover / 100 : null;
  let arc = 0.5;
  if (w && w.sunrise && w.sunset && !params.get("phase")) {
    const day = w.sunset - w.sunrise;
    arc = phase === "night" ? ((nowS - w.sunset + 86400) % 86400) / (86400 - day) : (nowS - w.sunrise) / day;
  }
  const pal = PHASES[phase] || PHASES.day, k = KINDS[kind] || KINDS.clear;
  // The water in the middle of the tank: what distance fades things toward.
  const haze = mixHex(mixHex(pal.top, pal.grey, k.grey * 0.5), pal.deep, 0.35);
  return { kind, phase, pal, k, cover, haze, precip: w && w.precip_mm, wind: (w && w.wind_mph) || 4,
    arc: Math.min(1, Math.max(0, arc)), moon: moonPhase(nowS * 1000) };
}

// The real moon's phase, 0 = new, 0.5 = full: days since a known new moon
// (2000-01-06 18:14 UTC) over the mean synodic month. The true month runs
// 29.3-29.8 days, so a given new or full moon can land up to about a day
// off - a few percent of the lit fraction, not enough to see. ?moon=0.25
// previews a phase.
const SYNODIC_DAYS = 29.530588853;
const NEW_MOON_MS = Date.UTC(2000, 0, 6, 18, 14);
function moonPhase(nowMs) {
  const preview = parseFloat(params.get("moon"));
  if (!isNaN(preview)) return ((preview % 1) + 1) % 1;
  const days = (nowMs - NEW_MOON_MS) / 86400000;
  return (((days / SYNODIC_DAYS) % 1) + 1) % 1;
}

// The moon as seen from the northern hemisphere: lit from the right while
// waxing, from the left while waning. The lit half-disc, closed by the
// terminator - half an ellipse whose width follows the phase, bulging into
// the lit half for a crescent and away from it for a gibbous moon.
function drawMoon(x, y, r, p) {
  ctx.fillStyle = "rgba(150,160,185,0.18)"; // the unlit part, faintly
  ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill();
  const lit = (1 - Math.cos(2 * Math.PI * p)) / 2;
  if (lit < 0.01) return;
  const waxing = p < 0.5, gibbous = lit > 0.5, rx = r * Math.abs(Math.cos(2 * Math.PI * p));
  const top = -Math.PI / 2, bottom = Math.PI / 2;
  ctx.fillStyle = "#E6E9F0";
  ctx.beginPath();
  if (waxing) {
    ctx.arc(x, y, r, top, bottom); // the right half
    ctx.ellipse(x, y, rx, r, 0, bottom, top, !gibbous); // back up: left for gibbous, right for crescent
  } else {
    ctx.arc(x, y, r, bottom, top + 2 * Math.PI); // the left half
    ctx.ellipse(x, y, rx, r, 0, top, bottom, !gibbous); // back down: right for gibbous, left for crescent
  }
  ctx.fill();
}

const stars = Array.from({ length: 60 }, () => [Math.random(), rand(TOP_BAR, SURF - 0.02), Math.random() * 6]);
const clouds = Array.from({ length: 12 }, () => ({ x: Math.random() * 1.3 - 0.15, y: rand(0.1, SURF - 0.045), w: rand(0.08, 0.16) }));
const drops = [], ripples = [], flakes = [];
let flash = 0, bolt = null, nextBolt = 0, dropCarry = 0;

function drawSky(sc, t, k) {
  const pal = sc.pal, surf = SURF * H;
  const sky = ctx.createLinearGradient(0, 0, 0, surf);
  sky.addColorStop(0, mix(pal.sky[0], pal.grey, sc.k.grey));
  sky.addColorStop(1, mix(pal.sky[1], pal.grey, sc.k.grey));
  ctx.fillStyle = sky; ctx.fillRect(0, 0, W, surf);
  if (sc.phase === "night" && sc.k.grey < 0.5) {
    ctx.fillStyle = "#F1EFE8";
    for (const [x, y, ph] of stars) {
      ctx.globalAlpha = Math.max(0, (0.35 + 0.35 * Math.sin(t / 900 + ph)) * (1 - sc.k.grey * 2));
      ctx.fillRect(x * W, y * H, 2, 2);
    }
    ctx.globalAlpha = 1;
  }
  if (sc.k.grey < 0.5) {
    const bx = (0.08 + sc.arc * 0.84) * W;
    const by = surf - H * 0.04 - Math.sin(sc.arc * Math.PI) * H * 0.025;
    const r = H * 0.028, night = sc.phase === "night";
    // A thin moon glows less: scale the halo by how much of it is lit.
    const glowAlpha = night ? 0.35 * (1 - Math.cos(2 * Math.PI * sc.moon)) / 2 : 0.55;
    const glow = ctx.createRadialGradient(bx, by, r * 0.5, bx, by, r * 4);
    glow.addColorStop(0, night ? `rgba(230,235,245,${glowAlpha.toFixed(3)})` : "rgba(255,240,200,0.55)");
    glow.addColorStop(1, "rgba(255,240,200,0)");
    ctx.fillStyle = glow; ctx.fillRect(bx - r * 4, by - r * 4, r * 8, r * 8);
    if (night) drawMoon(bx, by, r, sc.moon);
    else {
      ctx.fillStyle = sc.phase === "day" ? "#FFF1C4" : "#FFC98A";
      ctx.beginPath(); ctx.arc(bx, by, r, 0, 7); ctx.fill();
    }
  }
  const nClouds = sc.cover != null ? Math.round(sc.cover * 11) : sc.k.clouds;
  ctx.fillStyle = sc.phase === "night" ? "#2A3350" : mix("#F4F3EE", "#7C838C", sc.k.grey);
  const drift = 0.000012 * (1 + sc.wind / 4) * k;
  ctx.globalAlpha = 0.9;
  for (let i = 0; i < nClouds; i++) {
    const c = clouds[i];
    c.x += drift;
    if (c.x > 1.15) c.x = -0.2;
    for (let j = 0; j < 4; j++) {
      const px = (c.x + (j - 1.5) * c.w * 0.28) * W, py = c.y * H + (j % 2 ? -1 : 1) * H * 0.008;
      ctx.beginPath(); ctx.ellipse(px, py, c.w * W * 0.24, H * 0.022 * (j % 2 ? 1.3 : 1), 0, 0, 7); ctx.fill();
    }
  }
  ctx.globalAlpha = 1;
  return nClouds;
}

function drawRays(sc, t, nClouds) {
  // None at night: they were faint anyway (35%), and six translucent
  // polygons down most of the screen are real work for the Pi every frame.
  const rays = sc.k.rays * (sc.phase === "night" ? 0 : sc.phase === "day" ? 1 : 0.7);
  if (rays <= 0.02) return;
  const surf = SURF * H;
  ctx.fillStyle = sc.pal.ray;
  for (let i = 0; i < 6; i++) {
    const bx = (i * 0.19 + Math.sin(t / 5000 + i) * 0.03) * W;
    let shade = 1; // a cloud passing over dims the ray under it
    for (let j = 0; j < nClouds; j++) if (Math.abs(clouds[j].x * W - (bx + W * 0.07)) < clouds[j].w * W * 0.6) shade = 0.4;
    ctx.globalAlpha = 0.1 * rays * shade;
    ctx.beginPath(); ctx.moveTo(bx, surf); ctx.lineTo(bx + W * 0.05, surf); ctx.lineTo(bx + W * 0.17, H * 0.92); ctx.lineTo(bx + W * 0.09, H * 0.92); ctx.fill();
  }
  ctx.globalAlpha = 1;
}

function drawSurface(t) {
  const surf = SURF * H;
  ctx.strokeStyle = "rgba(255,255,255,0.35)"; ctx.lineWidth = 2; ctx.beginPath();
  for (let i = 0; i <= 40; i++) {
    const x = (i / 40) * W, y = surf + Math.sin(i * 0.9 + t / 700) * H * 0.004;
    if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
  }
  ctx.stroke();
}

function drawPrecipitation(sc, t, dt, k) {
  const surf = SURF * H;
  if (sc.kind === "rain" || sc.kind === "drizzle" || sc.kind === "storm") {
    const rate = sc.kind === "drizzle" ? 25 : sc.kind === "storm" ? 150 : Math.min(160, 50 + 25 * (sc.precip || 0));
    dropCarry += rate * dt;
    while (dropCarry >= 1 && drops.length < 400) { dropCarry -= 1; drops.push({ x: Math.random() * W, y: TOP_BAR * H * rand(0.8, 1.2) }); }
  }
  ctx.strokeStyle = "rgba(210,225,240,0.6)"; ctx.lineWidth = 1.2; ctx.beginPath();
  for (let i = drops.length - 1; i >= 0; i--) {
    const d = drops[i];
    d.y += H * 0.02 * k; d.x += sc.wind * 0.05 * k;
    if (d.y >= surf) { if (ripples.length < 120) ripples.push({ x: d.x, r: 1, a: 0.6 }); drops.splice(i, 1); continue; }
    ctx.moveTo(d.x, d.y); ctx.lineTo(d.x - sc.wind * 0.3, d.y - H * 0.018);
  }
  ctx.stroke();
  ctx.strokeStyle = "#DCE8F4"; ctx.lineWidth = 1;
  for (let i = ripples.length - 1; i >= 0; i--) {
    const r = ripples[i];
    r.r += 0.6 * k; r.a -= 0.025 * k;
    if (r.a <= 0) { ripples.splice(i, 1); continue; }
    ctx.globalAlpha = r.a; ctx.beginPath(); ctx.ellipse(r.x, surf + 2, r.r * 2, r.r * 0.5, 0, 0, 7); ctx.stroke();
  }
  ctx.globalAlpha = 1;
  if (sc.kind === "snow") {
    if (flakes.length < 160 && Math.random() < 0.8 * k) flakes.push({ x: Math.random() * W, y: TOP_BAR * H, ph: Math.random() * 6 });
    ctx.fillStyle = "#F4F7FB";
    for (let i = flakes.length - 1; i >= 0; i--) {
      const f = flakes[i], inWater = f.y > surf;
      f.y += H * (inWater ? 0.0006 : 0.0025) * k; f.x += Math.sin(t / 700 + f.ph) * 0.4 * k;
      if (f.y > H * 0.55) { flakes.splice(i, 1); continue; }
      ctx.globalAlpha = inWater ? Math.max(0, 0.7 - (f.y - surf) / (H * 0.35)) : 0.9;
      ctx.beginPath(); ctx.arc(f.x, f.y, 2.2, 0, 7); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  if (sc.kind === "storm") {
    const now = t / 1000;
    if (!nextBolt) nextBolt = now + rand(4, 12);
    if (now > nextBolt) {
      flash = 1; nextBolt = now + rand(5, 14);
      let x = rand(0.1, 0.9) * W, y = TOP_BAR * H;
      bolt = [[x, y]];
      while (y < surf) { y += rand(8, 16); x += rand(-12, 12); bolt.push([x, Math.min(y, surf)]); }
    }
  }
}

function drawLightning(dt) {
  if (flash <= 0) return;
  if (bolt && flash > 0.6) {
    ctx.strokeStyle = "#FFFFFF"; ctx.lineWidth = 2.5; ctx.beginPath();
    bolt.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.stroke();
  }
  ctx.fillStyle = `rgba(235,240,255,${0.45 * flash})`; ctx.fillRect(0, 0, W, H);
  flash -= dt * 3;
}

// ---- Layers ----
// Each layer slides by drift * its parallax: far things barely move, the
// weed in front of the glass moves most. That difference is the depth.

const PAR = { far: 0.15, back: 0.4, floor: 0.6, front: 1.25 };
const drift = (t) => Math.sin(t / 26000) * W * 0.035;

// The far layer - water, ridges and kelp in the haze - only changes with the
// weather, so it is drawn once to its own canvas and copied in every frame.
const far = document.createElement("canvas"), fctx = far.getContext("2d");
let farKey = "";
const farSeed = Array.from({ length: 6 }, () => rand(0, 6));
function paintFar(sc) {
  const pad = W * 0.06;
  far.width = W + pad * 2; far.height = H;
  const surf = SURF * H, pal = sc.pal;
  const water = fctx.createLinearGradient(0, surf, 0, H);
  water.addColorStop(0, mix(pal.top, pal.grey, sc.k.grey * 0.5));
  water.addColorStop(1, pal.deep);
  fctx.fillStyle = water; fctx.fillRect(0, surf, far.width, H - surf);
  // Two ridges of distant rock, the nearer one a little clearer.
  [[0.55, 0.72, 0.1], [0.4, 0.8, 0.07]].forEach(([fade, base, amp], r) => {
    fctx.fillStyle = mix(pal.deep, sc.haze, fade);
    fctx.beginPath(); fctx.moveTo(0, H);
    for (let i = 0; i <= 60; i++) {
      const x = (i / 60) * far.width;
      const y = H * (base - amp * (0.5 + 0.3 * Math.sin(i * 0.23 + farSeed[r]) + 0.2 * Math.sin(i * 0.61 + farSeed[r + 2])));
      fctx.lineTo(x, y);
    }
    fctx.lineTo(far.width, H); fctx.fill();
  });
  // Faint coral heads and fans along the far reef.
  fctx.fillStyle = fctx.strokeStyle = mix(pal.deep, sc.haze, 0.3); fctx.lineCap = "round";
  for (let i = 0; i < 26; i++) {
    const x = (i / 26 + Math.sin(i * 7.3) * 0.015) * far.width, y = H * (0.77 + 0.02 * Math.sin(i * 2.1)), r = H * (0.025 + 0.02 * Math.abs(Math.sin(i * 3.1)));
    fctx.beginPath();
    if (i % 3 === 0) { fctx.moveTo(x, y); fctx.arc(x, y, r * 1.6, Math.PI * 1.2, Math.PI * 1.8); fctx.closePath(); }
    else fctx.ellipse(x, y, r * 1.3, r, 0, Math.PI, 0);
    fctx.fill();
  }
  farKey = sc.phase + sc.kind + W + "x" + H;
}

// Seaweed: tapered blades that sway. Back clumps are hazy, front ones dark.
function makeClumps(n, xs, hMin, hMax) {
  return xs.slice(0, n).map((x) => Array.from({ length: Math.round(rand(4, 7)) }, () => ({
    x: x + rand(-0.025, 0.025), h: rand(hMin, hMax), w: rand(0.006, 0.011), lean: rand(-0.3, 0.3),
    ph: rand(0, 6), kelp: Math.random() < 0.3,
  })));
}
const backWeed = makeClumps(9, [0.03, 0.14, 0.25, 0.38, 0.52, 0.63, 0.74, 0.86, 0.97], 0.14, 0.34);
const frontWeed = makeClumps(3, [-0.02, 0.07, 1.01], 0.2, 0.42);
function drawWeed(clumps, off, base, colours, t) {
  for (const clump of clumps) for (const b of clump) {
    const x = b.x * W + off, h = b.h * H, w = b.w * W;
    const sway = Math.sin(t / 1500 + b.ph) * h * 0.1 + b.lean * h * 0.3;
    ctx.fillStyle = b.kelp ? colours[1] : colours[0];
    ctx.beginPath();
    ctx.moveTo(x - w, base);
    ctx.quadraticCurveTo(x - w * 0.6 + sway * 0.3, base - h * 0.55, x + sway, base - h);
    ctx.quadraticCurveTo(x + w * 0.6 + sway * 0.3, base - h * 0.5, x + w, base);
    ctx.fill();
  }
}

// The reef floor - pale sand, coral, starfish and shells - only changes with
// the weather, so like the far layer it is drawn once and copied in. Only
// the light on the sand and the anemones move, and those are drawn live.
const glints = Array.from({ length: 9 }, () => ({ x: Math.random(), ph: rand(0, 6), w: rand(0.03, 0.07) }));
const reef = document.createElement("canvas"), rctx = reef.getContext("2d");
let reefKey = "";
const REEF_PAD = 0.04; // of the width, either side, for the drift
// Spots along the floor, as fractions of the width, clear of the houses.
const CORALS = [[0.03, "branch"], [0.1, "fan"], [0.155, "brain"], [0.375, "tube"], [0.42, "branch"], [0.53, "brain"],
  [0.585, "tube"], [0.845, "branch"], [0.9, "fan"], [0.965, "brain"]];
const ANEMONES = [[0.2, "#F29BB8"], [0.475, "#B892E8"], [0.8, "#F4B26B"]];
// Where node i of n stands, as a fraction of the width: spread out from the
// middle, 0.44 apart at most (two nodes: 0.28 and 0.72).
function nodeX(i, n) { return 0.5 + (i - (n - 1) / 2) * Math.min(0.44, 0.84 / n); }
function seeded(n) { let x = n * 9301 + 49297; return () => ((x = (x * 9301 + 49297) % 233280) / 233280); }
function floorY(x) { return H * (FLOOR + 0.012 * Math.sin(x / W * 9) + 0.008 * Math.sin(x / W * 23 + 1)); }
function star(c, x, y, r, rot, color) {
  c.fillStyle = color; c.beginPath();
  for (let i = 0; i < 10; i++) {
    const a = rot + (i * Math.PI) / 5, rr = i % 2 ? r * 0.42 : r;
    c.lineTo(x + Math.cos(a) * rr, y + Math.sin(a) * rr);
  }
  c.fill();
}
function paintReef(sc) {
  const pad = W * REEF_PAD, dim = sc.pal.dim + sc.k.grey * 0.2, tint = (hex, extra = 0) => mix(hex, sc.pal.deep, dim + extra);
  reef.width = W + pad * 2; reef.height = H;
  const c = rctx;
  const g = c.createLinearGradient(0, H * FLOOR - H * 0.02, 0, H);
  g.addColorStop(0, tint("#E6D5AE")); g.addColorStop(1, tint("#B79E72", 0.1));
  c.fillStyle = g; c.beginPath(); c.moveTo(0, H);
  for (let i = 0; i <= 50; i++) { const x = (i / 50) * reef.width; c.lineTo(x, floorY(x)); }
  c.lineTo(reef.width, H); c.fill();
  c.lineCap = "round";
  CORALS.forEach(([fx, kind], n) => {
    const x = pad + fx * W, y = floorY(x) + H * 0.015, r = seeded(n + 1), size = H * (0.06 + 0.04 * r());
    if (kind === "branch") {
      const color = tint(["#E8739A", "#F08A4B", "#D9607A"][n % 3]);
      c.strokeStyle = color;
      const grow = (bx, by, len, ang, depth) => {
        const ex = bx + Math.sin(ang) * len, ey = by - Math.cos(ang) * len;
        c.lineWidth = Math.max(2, depth * 2.4); c.beginPath(); c.moveTo(bx, by); c.lineTo(ex, ey); c.stroke();
        if (depth > 0) for (const d of [-1, 1]) grow(ex, ey, len * (0.62 + 0.15 * r()), ang + d * (0.35 + 0.25 * r()), depth - 1);
      };
      grow(x, y, size * 0.45, (r() - 0.5) * 0.3, 3);
    } else if (kind === "brain") {
      const w = size * 0.8, h = size * 0.55;
      c.fillStyle = tint("#C9A94A"); c.beginPath(); c.ellipse(x, y, w, h, 0, Math.PI, 0); c.fill();
      c.strokeStyle = tint("#8E7426"); c.lineWidth = 1.5;
      for (let j = 1; j < 5; j++) {
        c.beginPath();
        for (let a = 0; a <= 20; a++) {
          const t = Math.PI + (a / 20) * Math.PI, k = j / 5;
          c.lineTo(x + Math.cos(t) * w * k + Math.sin(a * 1.9 + j) * 2.5, y + Math.sin(t) * h * k);
        }
        c.stroke();
      }
    } else if (kind === "fan") {
      const R = size * 1.1, color = tint(["#8E5BB5", "#B0487A"][n % 2]);
      c.strokeStyle = color; c.lineWidth = 1.4; c.beginPath();
      for (let j = 0; j <= 14; j++) {
        const a = -1.05 + (j / 14) * 2.1;
        c.moveTo(x, y); c.quadraticCurveTo(x + Math.sin(a) * R * 0.5, y - R * 0.6, x + Math.sin(a) * R, y - Math.cos(a) * R);
      }
      for (let ring = 1; ring <= 3; ring++) c.arc(x, y, R * ring / 3, -Math.PI / 2 - 1.05, -Math.PI / 2 + 1.05);
      c.stroke();
      c.lineWidth = 4; c.beginPath(); c.moveTo(x, y); c.lineTo(x, y - R * 0.3); c.stroke();
    } else { // tube coral: a cluster of open-topped tubes
      for (let j = 0; j < 5; j++) {
        const tx = x + (j - 2) * size * 0.2, th = size * (0.45 + 0.5 * r()), tw = size * 0.16;
        c.fillStyle = tint("#D96A3A"); c.beginPath(); c.roundRect(tx - tw / 2, y - th, tw, th, tw / 2); c.fill();
        c.fillStyle = tint("#F6B27A"); c.beginPath(); c.ellipse(tx, y - th + tw * 0.25, tw * 0.45, tw * 0.22, 0, 0, 7); c.fill();
      }
    }
  });
  // Starfish, shells and pebbles strewn on the sand.
  const r = seeded(99);
  for (let j = 0; j < 14; j++) {
    const x = pad + r() * W, y = H * (0.9 + r() * 0.08);
    if (j % 3 === 0) star(c, x, y, H * (0.01 + r() * 0.006), r() * 6, tint(["#E0703A", "#D94F4F", "#E8A33A"][j % 3]));
    else if (j % 3 === 1) {
      const sr = H * 0.011;
      c.fillStyle = tint("#EEDFC8"); c.beginPath(); c.moveTo(x, y); c.arc(x, y, sr, Math.PI * 1.1, Math.PI * 1.9); c.closePath(); c.fill();
      c.strokeStyle = tint("#C9B79A"); c.lineWidth = 1; c.beginPath();
      for (let q = 0; q < 5; q++) { const a = Math.PI * (1.15 + q * 0.17); c.moveTo(x, y); c.lineTo(x + Math.cos(a) * sr, y + Math.sin(a) * sr); }
      c.stroke();
    } else { c.fillStyle = tint("#9C9486"); c.beginPath(); c.ellipse(x, y, H * 0.007, H * 0.004, 0, 0, 7); c.fill(); }
  }
  reefKey = sc.phase + sc.kind + W + "x" + H;
}
function drawFloor(sc, off, t) {
  if (reefKey !== sc.phase + sc.kind + W + "x" + H) paintReef(sc);
  ctx.drawImage(reef, -W * REEF_PAD + off, 0);
  const light = sc.k.rays * (sc.phase === "day" ? 1 : sc.phase === "night" ? 0 : 0.5);
  if (light > 0.05) {
    ctx.fillStyle = "#FFF6DC";
    for (const gl of glints) {
      ctx.globalAlpha = 0.07 * light * (0.6 + 0.4 * Math.sin(t / 1300 + gl.ph));
      const x = ((gl.x + t / 400000) % 1.1) * W;
      ctx.beginPath(); ctx.ellipse(x, H * 0.93, gl.w * W, H * 0.012, 0, 0, 7); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  // Anemones: a crown of tentacles swaying with the current.
  const dim = sc.pal.dim + sc.k.grey * 0.2;
  ctx.lineCap = "round";
  for (const [fx, color] of ANEMONES) {
    const x = fx * W + off, y = floorY(x - off + W * REEF_PAD) + H * 0.012, L = H * 0.055;
    ctx.fillStyle = mix(mixHex(color, "#000000", 0.25), sc.pal.deep, dim);
    ctx.beginPath(); ctx.ellipse(x, y, L * 0.45, L * 0.3, 0, Math.PI, 0); ctx.fill();
    ctx.strokeStyle = mix(color, sc.pal.deep, dim); ctx.lineWidth = Math.max(2, H * 0.005);
    ctx.beginPath();
    for (let i = 0; i < 16; i++) {
      const a = -1.25 + (i / 15) * 2.5, sway = Math.sin(t / 900 + i * 0.4 + fx * 20) * L * 0.28;
      const bx = x + Math.sin(a) * L * 0.35, by = y - L * 0.25;
      ctx.moveTo(bx, by);
      ctx.quadraticCurveTo(bx + Math.sin(a) * L * 0.5, by - L * 0.5, bx + Math.sin(a) * L * 0.75 + sway, by - Math.cos(a) * L * 0.9);
    }
    ctx.stroke();
  }
}

// One sandcastle per node, flying its own flag. Its windows and gate are
// lit while the node is ready - faintly by day, brightly at dusk and night -
// and when it isn't, the lights go out, the flag hangs grey and limp, and
// the name written in the sand in front turns red and says so.
const FLAGS = ["#E24B4A", "#378ADD", "#EF9F27", "#8E5BB5"];
function drawHouses(sc, off, t) {
  const nodes = state ? state.nodes : [];
  const dim = sc.pal.dim + sc.k.grey * 0.2;
  const glow = { day: 0.3, dawn: 0.65, dusk: 0.7, night: 1 }[sc.phase] * (1 - sc.k.grey * 0.3) + sc.k.grey * 0.3;
  nodes.forEach((n, i) => {
    // FLOOR + 0.015: high enough that the name in the sand clears the 56 px
    // bottom bar (it used to sit under it at FLOOR + 0.04).
    const cx = nodeX(i, nodes.length) * W + off, base = H * (FLOOR + 0.015), hw = W * 0.055, lit = n.ready;
    const sand = mix(lit ? "#E2C98F" : "#A8966E", sc.pal.deep, dim);
    const shade = mix(lit ? "#C4A66A" : "#857554", sc.pal.deep, dim);
    const light = (a) => (lit ? mix("#FFD98A", "#FFB54A", a) : "#1E2228");
    // A bucket-shaped tower: tapered, crenellated, shaded on its right side.
    const tower = (x, w, h) => {
      const top = base - h, tw = w * 0.82;
      ctx.fillStyle = sand;
      ctx.beginPath(); ctx.moveTo(x - w / 2, base); ctx.lineTo(x - tw / 2, top); ctx.lineTo(x + tw / 2, top); ctx.lineTo(x + w / 2, base); ctx.fill();
      for (let m = 0; m < 3; m++) ctx.fillRect(x - tw / 2 + m * tw * 0.4, top - h * 0.1, tw * 0.2, h * 0.1 + 1);
      ctx.fillStyle = shade;
      ctx.beginPath(); ctx.moveTo(x + w * 0.22, base); ctx.lineTo(x + tw * 0.2, top); ctx.lineTo(x + tw / 2, top); ctx.lineTo(x + w / 2, base); ctx.fill();
      return top;
    };
    // The wall joining the towers, with its own crenellations.
    const wallH = H * 0.075, wallTop = base - wallH;
    ctx.fillStyle = sand; ctx.fillRect(cx - hw * 0.8, wallTop, hw * 1.6, wallH);
    for (let m = 0; m < 7; m++) ctx.fillRect(cx - hw * 0.8 + m * hw * 0.25, wallTop - H * 0.012, hw * 0.13, H * 0.012 + 1);
    const sideH = H * 0.12, midH = H * 0.165;
    tower(cx - hw * 0.85, hw * 0.48, sideH);
    tower(cx + hw * 0.85, hw * 0.48, sideH);
    const midTop = tower(cx, hw * 0.55, midH);
    // Lit arched windows and the gate, with their glow.
    const arch = (x, y, w, h) => { ctx.beginPath(); ctx.moveTo(x - w / 2, y); ctx.lineTo(x - w / 2, y - h + w / 2); ctx.arc(x, y - h + w / 2, w / 2, Math.PI, 0); ctx.lineTo(x + w / 2, y); ctx.fill(); };
    const wins = [[cx - hw * 0.85, base - sideH * 0.55], [cx + hw * 0.85, base - sideH * 0.55], [cx, base - midH * 0.62]];
    const ww = hw * 0.12, wh = hw * 0.2;
    if (lit) {
      ctx.globalCompositeOperation = "lighter";
      for (const [wx, wy] of [...wins, [cx, base - H * 0.02]]) {
        const g = ctx.createRadialGradient(wx, wy, ww * 0.5, wx, wy, ww * 6);
        g.addColorStop(0, `rgba(255,190,90,${0.35 * glow})`); g.addColorStop(1, "rgba(255,190,90,0)");
        ctx.fillStyle = g; ctx.fillRect(wx - ww * 6, wy - ww * 6, ww * 12, ww * 12);
      }
      ctx.globalCompositeOperation = "source-over";
    }
    wins.forEach(([wx, wy], j) => { ctx.fillStyle = light(0.5 - 0.5 * Math.sin(t / 400 + j * 2 + i)); arch(wx, wy + wh / 2, ww, wh); });
    ctx.fillStyle = light(0.2); arch(cx, base, hw * 0.3, H * 0.05);
    // Shells and a starfish pressed into the sand.
    ctx.fillStyle = mix("#F4EBDD", sc.pal.deep, dim);
    for (const [sx, sy] of [[-0.55, 0.35], [0.45, 0.55], [-0.2, 0.75]]) {
      ctx.beginPath(); ctx.arc(cx + sx * hw, wallTop + sy * wallH, hw * 0.05, Math.PI, 0); ctx.fill();
    }
    star(ctx, cx + hw * 0.55, wallTop + wallH * 0.3, hw * 0.09, i * 1.3, mix(i % 2 ? "#E8A33A" : "#E0703A", sc.pal.deep, dim));
    // The flag: waving when the node is ready, hanging grey when it isn't.
    const poleTop = midTop - H * 0.1 - midH * 0.1;
    ctx.strokeStyle = mix("#6B5A3E", sc.pal.deep, dim); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx, midTop - midH * 0.1); ctx.lineTo(cx, poleTop); ctx.stroke();
    const fw = hw * 0.42, fh = H * 0.028;
    ctx.fillStyle = mix(lit ? FLAGS[i % FLAGS.length] : "#6E6A64", sc.pal.deep, dim);
    ctx.beginPath(); ctx.moveTo(cx, poleTop);
    if (lit) {
      const wave = Math.sin(t / 350 + i) * fh * 0.25;
      ctx.quadraticCurveTo(cx + fw * 0.5, poleTop + wave, cx + fw, poleTop + fh * 0.5 + wave * 0.5);
      ctx.quadraticCurveTo(cx + fw * 0.5, poleTop + fh - wave, cx, poleTop + fh);
    } else {
      ctx.lineTo(cx + fw * 0.25, poleTop + fh * 1.6); ctx.lineTo(cx, poleTop + fh * 1.3);
    }
    ctx.fill();
    // Sand banked around the foot of the castle.
    ctx.fillStyle = mix("#E6D5AE", sc.pal.deep, dim);
    ctx.beginPath(); ctx.ellipse(cx, base + H * 0.004, hw * 1.3, H * 0.014, 0, Math.PI, 0); ctx.fill();
    // The node's name written in the sand in front, like a finger drew it:
    // a deep brown groove with a lit edge under it - red, and saying so,
    // when the node isn't ready. The groove darkens only half as much as
    // the sand at night, so it still stands out. Fish swim over it.
    ctx.font = `italic 700 ${Math.round(H * 0.028)}px Georgia, "DejaVu Serif", serif`;
    ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
    const sandY = base + H * 0.037, written = lit ? n.name : `${n.name} · not ready`;
    ctx.fillStyle = mix("#FFF4DA", sc.pal.deep, dim); ctx.globalAlpha = 0.7; ctx.fillText(written, cx, sandY + 1.5);
    ctx.globalAlpha = 1;
    ctx.fillStyle = mix(lit ? "#4A3418" : "#A32323", sc.pal.deep, dim * 0.5); ctx.fillText(written, cx, sandY);
  });
}

// Where a pill of this text would sit, kept on screen: {x, y, w, h}, centred.
function pillBox(text, x, y, size) {
  ctx.font = `600 ${size}px system-ui, sans-serif`;
  const w = ctx.measureText(text).width + size * 1.2, h = size * 1.6;
  return { x: Math.min(W - w / 2 - 8, Math.max(w / 2 + 8, x)), y, w, h };
}
function pill(text, x, y, color, bg, size) {
  const b = pillBox(text, x, y, size);
  ctx.fillStyle = bg;
  ctx.beginPath(); ctx.roundRect(b.x - b.w / 2, b.y - b.h / 2, b.w, b.h, b.h / 2); ctx.fill();
  ctx.fillStyle = color; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, b.x, b.y + 1);
  return b;
}

// ---- Fish ----
// Drawn side-on, facing +x, s = half the body length. Each species is one
// function; fins are drawn in a lighter shade of the body colour.

const SPECIES = {
  classic(s, wag, c) {
    fin(c.fin, [[-0.75, 0], [-1.3, -0.5 + wag], [-1.12, 0], [-1.3, 0.5 + wag]]);
    fin(c.fin, [[0.05, -0.36], [0.3, -0.75], [0.55, -0.3]]);
    body(c, () => {
      ctx.moveTo(s * 1.1, 0);
      ctx.quadraticCurveTo(s * 0.35, -s * 0.66, -s * 0.8, -s * 0.07);
      ctx.lineTo(-s * 0.8, s * 0.07);
      ctx.quadraticCurveTo(s * 0.35, s * 0.62, s * 1.1, 0);
    }, s, [[0.5, 0.1], [-0.15, 0.12], [-0.72, 0.06]]);
    fin(c.fin, [[0.2, 0.15], [-0.15, 0.4], [0.05, 0.12]]);
    return [0.62, -0.1];
  },
  angel(s, wag, c) {
    fin(c.fin, [[-0.45, 0], [-0.95, -0.4 + wag * 0.6], [-0.95, 0.4 + wag * 0.6]]);
    fin(c.fin, [[0.35, -0.5], [0, -1.2], [-0.55, -1.0], [-0.5, -0.2]]);
    fin(c.fin, [[0.3, 0.5], [0, 1.1], [-0.55, 0.95], [-0.5, 0.2]]);
    body(c, () => {
      ctx.moveTo(s * 0.8, s * 0.05);
      ctx.quadraticCurveTo(s * 0.45, -s * 0.8, -s * 0.5, -s * 0.12);
      ctx.lineTo(-s * 0.5, s * 0.12);
      ctx.quadraticCurveTo(s * 0.45, s * 0.8, s * 0.8, s * 0.05);
    }, s * 0.8);
    return [0.45, -0.12];
  },
  slender(s, wag, c) {
    fin(c.fin, [[-1.05, 0], [-1.55, -0.45 + wag], [-1.3, 0], [-1.55, 0.45 + wag]]);
    fin(c.fin, [[-0.25, -0.2], [-0.4, -0.45], [-0.65, -0.15]]);
    fin(c.fin, [[-0.3, 0.18], [-0.45, 0.38], [-0.65, 0.14]]);
    body(c, () => {
      ctx.moveTo(s * 1.45, s * 0.02);
      ctx.quadraticCurveTo(s * 0.3, -s * 0.36, -s * 1.1, -s * 0.05);
      ctx.lineTo(-s * 1.1, s * 0.05);
      ctx.quadraticCurveTo(s * 0.3, s * 0.3, s * 1.45, s * 0.02);
    }, s * 0.35);
    return [1.05, -0.06];
  },
  tang(s, wag, c) {
    fin(c.fin, [[-0.7, 0], [-1.15, -0.55 + wag], [-0.95, 0], [-1.15, 0.55 + wag]]);
    fin(c.fin, [[0.45, -0.55], [0.3, -0.78], [-0.6, -0.62], [-0.7, -0.3]]);
    fin(c.fin, [[0.2, 0.55], [0.05, 0.72], [-0.55, 0.6], [-0.7, 0.3]]);
    body(c, () => { ctx.ellipse(0, 0, s * 0.85, s * 0.68, 0, 0, 7); }, s * 0.68);
    return [0.5, -0.2];
  },
  // A ray, seen side-on: a flat body, wings that ripple, a long thin tail.
  ray(s, wag, c) {
    const flap = wag * 2.6;
    ctx.strokeStyle = c.fin; ctx.lineWidth = Math.max(1.2, s * 0.05); ctx.lineCap = "round";
    ctx.beginPath(); ctx.moveTo(-s * 0.75, 0); ctx.quadraticCurveTo(-s * 1.5, s * 0.02 + flap * s * 0.3, -s * 2.2, s * 0.1 + flap * s * 0.5); ctx.stroke();
    fin(c.fin, [[0.6, 0.04], [0.05, 0.2 - flap * 0.8], [-0.55, 0.04]]);
    body(c, () => {
      ctx.moveTo(s * 1.0, s * 0.02);
      ctx.quadraticCurveTo(s * 0.35, -s * 0.24, -s * 0.8, -s * 0.02);
      ctx.lineTo(-s * 0.8, s * 0.05);
      ctx.quadraticCurveTo(s * 0.3, s * 0.13, s * 1.0, s * 0.02);
    }, s * 0.2);
    fin(c.body, [[0.75, -0.04], [0.1, -0.3 + flap], [-0.6, -0.03]]);
    return [0.62, -0.13];
  },
};
let S = 1; // the current fish's half-length, for fin()
function fin(color, pts) {
  ctx.fillStyle = color;
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x * S, y * S) : ctx.moveTo(x * S, y * S)));
  ctx.closePath(); ctx.fill();
}
// The body, then a paler belly and a darker back over it: countershading.
// bands: [x, width] in half-lengths, for a clownfish's white stripes.
function body(c, path, halfHeight, bands) {
  ctx.fillStyle = c.body; ctx.beginPath(); path(); ctx.fill();
  ctx.save(); ctx.clip();
  ctx.fillStyle = c.back; ctx.fillRect(-S * 2, -S * 2, S * 4, S * 2 - halfHeight * 0.45);
  ctx.fillStyle = c.belly; ctx.fillRect(-S * 2, halfHeight * 0.3, S * 4, S * 2);
  if (bands) {
    ctx.fillStyle = c.band;
    for (const [x, w] of bands) { ctx.beginPath(); ctx.ellipse(x * S, 0, w * S, S, 0, 0, 7); ctx.fill(); }
  }
  ctx.restore();
}

function sizeFor(p) {
  if (p.mem_bytes == null) return 0.9;
  const t = Math.min(1, Math.max(0, (Math.log2(p.mem_bytes / MIB) - 3) / 7));
  return 0.6 + t * 1.4;
}
function speedFor(p) {
  if (p.cpu_m == null) return 0.0006;
  return Math.min(0.0045, 0.00035 + 0.00045 * Math.log2(1 + p.cpu_m));
}
function memShare(p) { return p.mem_bytes != null && p.mem_limit ? p.mem_bytes / p.mem_limit : null; }
function isSick(p) { return (p.phase === "Running" && !p.ready) || !!p.reason; }
function shortName(p) {
  // A Deployment's pod ends "-<hash>-<5>"; a DaemonSet's just "-<5>". Strip
  // one or the other, never both: "argocd-redis" must not lose "-redis".
  let name = p.name.replace(/-[a-z0-9]{8,10}-[a-z0-9]{5}$/, "");
  if (name === p.name) name = name.replace(/-[a-z0-9]{5}$/, "");
  return name.startsWith(p.namespace + "-") ? name.slice(p.namespace.length + 1) : name;
}
function attention(f) {
  const p = f.pod;
  if (isSick(p)) return p.reason || "not ready";
  if (p.phase === "Pending") return "pending";
  const share = memShare(p);
  if (share !== null && share >= PUFF_AT) return `${Math.round(share * 100)}% of its memory`;
  if (f.restartedAt && Date.now() - f.restartedAt < 3600e3) return "restarted";
  return null;
}

const fish = new Map(); // pod name -> fish
const restartsSeen = new Map();
let firstSync = true;
function syncFish() {
  const seen = new Set();
  for (const p of state.pods) {
    seen.add(p.name);
    let f = fish.get(p.name);
    if (!f) {
      const fromLeft = Math.random() < 0.5;
      f = { x: firstSync ? rand(0.05, 0.95) : fromLeft ? -0.06 : 1.06, y: rand(SURF + 0.1, 0.75), z: rand(0.2, 0.9),
        vx: fromLeft ? 0.001 : -0.001, vy: 0, face: fromLeft ? 1 : -1, s: sizeFor(p), sp: speedFor(p),
        tx: 0.5, ty: 0.5, tz: 0.5, retarget: 0, ph: rand(0, 6), jitter: rand(0.85, 1.15) };
      fish.set(p.name, f);
    }
    const before = restartsSeen.get(p.name);
    if (before !== undefined && p.restarts > before) f.restartedAt = Date.now();
    restartsSeen.set(p.name, p.restarts);
    f.pod = p;
    f.leaving = false;
  }
  for (const [name, f] of fish) if (!seen.has(name)) f.leaving = true;
  firstSync = false;
}

// Each fish wanders the tank on its own, picking a new spot every few
// seconds; rays keep to the bottom.
function stepFish(t, k) {
  for (const [name, f] of fish) {
    const p = f.pod, sick = isSick(p), pending = p.phase === "Pending";
    f.s += (sizeFor(p) - f.s) * Math.min(1, 0.03 * k);
    f.sp += (speedFor(p) * f.jitter - f.sp) * Math.min(1, 0.05 * k);
    if (f.leaving) {
      f.x += Math.sign(f.vx || 1) * 0.004 * k;
      if (f.x > 1.1 || f.x < -0.1) fish.delete(name);
      continue;
    }
    if (sick) { f.y = Math.max(SURF + 0.04, f.y - 0.0004 * k); f.vx *= 0.95; continue; }
    if (pending) { f.y = Math.min(FLOOR - 0.04, f.y + 0.0003 * k); f.vx *= 0.95; continue; }
    if ((f.retarget -= k / 60) <= 0 || Math.hypot(f.tx - f.x, (f.ty - f.y) * 1.6) < 0.02) {
      const low = NS_SPECIES[p.namespace] === "ray";
      f.tx = rand(0.05, 0.95);
      f.ty = low ? rand(0.74, FLOOR - 0.05) : rand(SURF + 0.07, 0.74);
      f.tz = rand(0, 1);
      f.retarget = rand(4, 9);
    }
    const dx = f.tx - f.x, dy = (f.ty - f.y) * 0.6, d = Math.hypot(dx, dy) || 1;
    const blend = Math.min(1, 0.04 * k);
    f.vx += (dx / d * f.sp - f.vx) * blend;
    f.vy += (dy / d * f.sp * 0.6 - f.vy) * blend;
    f.x += f.vx * k; f.y += f.vy * k;
    f.z += (f.tz - f.z) * Math.min(1, 0.004 * k);
    // Turn around by squashing through edge-on, as a fish seen side-on does.
    if (Math.abs(f.vx) > 0.0001) f.face += (Math.sign(f.vx) - f.face) * Math.min(1, 0.1 * k);
  }
}

// Fish glow at night, like bioluminescence: a soft halo in each fish's own
// colour behind it, added to the water (globalCompositeOperation "lighter").
// One halo image per colour, drawn once and then only stamped - a
// shadowBlur on every fish every frame would cost the Pi far more.
const GLOW = { day: 0, dawn: 0.35, dusk: 0.45, night: 1 };
// Real bioluminescence is a cold blue-green (luciferin, ~480 nm): the glow
// leans that way, keeping a tint of the namespace colour to tell fish
// apart. Warnings (a puffed fish) keep their orange or red.
const BIOLUME = "#3CF2D2";
const halos = new Map();
function halo(hex) {
  let img = halos.get(hex);
  if (!img) {
    img = document.createElement("canvas");
    img.width = img.height = 64;
    const g = img.getContext("2d"), [r, gr, b] = hexRgb(hex);
    const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    // Bright close to the fish, gone quickly: a tight glow, not a haze.
    grad.addColorStop(0, `rgba(${r},${gr},${b},0.75)`);
    grad.addColorStop(0.35, `rgba(${r},${gr},${b},0.4)`);
    grad.addColorStop(0.7, `rgba(${r},${gr},${b},0.08)`);
    grad.addColorStop(1, `rgba(${r},${gr},${b},0)`);
    g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
    // Colours blend through dusk and dawn, so new ones keep coming: start
    // over now and then rather than keep every one.
    if (halos.size > 48) halos.clear();
    halos.set(hex, img);
  }
  return img;
}

// Glowing plankton at night: specks that twinkle on their own and flash
// when a fish swims through them, like a wake in dinoflagellates.
// 120 of them: one additive pass, small dots - cheap even on the Pi.
const plankton = Array.from({ length: 120 }, () => ({ x: Math.random(), y: rand(SURF + 0.05, FLOOR - 0.04), z: Math.random(), ph: rand(0, 6), flash: 0 }));
function drawPlankton(sc, t, dt, off) {
  const night = (GLOW[sc.phase] || 0) * (1 - sc.k.grey * 0.4);
  if (night <= 0.02) return;
  // Batched by brightness: each fill call has a fixed cost, so 120 specks
  // drawn one by one cost more than everything else in the frame. Five
  // brightness levels, one path each - five fills.
  const fishes = [...fish.values()];
  const levels = [[], [], [], [], []];
  for (const p of plankton) {
    p.y -= 0.000015 * dt * 60; // drifting up, very slowly
    if (p.y < SURF + 0.03) { p.y = FLOOR - 0.04; p.x = Math.random(); }
    for (const f of fishes) {
      if (Math.abs(f.x - p.x) < 0.025 && Math.abs(f.y - p.y) < 0.035) { p.flash = 1; break; }
    }
    p.flash = Math.max(0, p.flash - dt * 1.4);
    const twinkle = 0.25 + 0.45 * Math.max(0, Math.sin(t / 900 + p.ph)) ** 4;
    const a = Math.min(1, twinkle + p.flash * 0.85);
    levels[Math.min(4, Math.floor(a * 5))].push(p);
  }
  ctx.globalCompositeOperation = "lighter";
  ctx.fillStyle = "#8FFFE6";
  levels.forEach((ps, i) => {
    if (!ps.length) return;
    ctx.globalAlpha = ((i + 0.5) / 5) * night;
    ctx.beginPath();
    for (const p of ps) {
      const x = p.x * W + off * (0.4 + 0.5 * p.z), y = p.y * H, r = 1 + p.z * 0.8 + p.flash * 1.6;
      ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, 7);
    }
    ctx.fill();
  });
  ctx.globalCompositeOperation = "source-over";
  ctx.globalAlpha = 1;
}

// Where a fish is this frame and how it glows - worked out once, then used
// by three passes in drawTank: every halo, then every body, then every
// light organ. Drawn per fish instead, the canvas switched between normal
// and additive ("lighter") blending ~140 times a frame, and each switch can
// make the Pi's GPU flush its work; in passes it's 4.
function placeFish(f, t, sc, off) {
  const p = f.pod, sick = isSick(p), pending = p.phase === "Pending";
  const share = memShare(p), puffed = !sick && !pending && share !== null && share >= PUFF_AT;
  // Farther fish are a little smaller and fade into the water.
  const depth = 0.8 + 0.2 * f.z;
  const s = f.s * W * 0.021 * depth;
  const X = f.x * W + off * (0.45 + 0.5 * f.z), Y = f.y * H + (sick ? 0 : Math.sin(t / 900 + f.ph) * H * 0.006);
  f.sx = X; f.sy = Y; f.sr = s; // for taps and tags
  const base = sick || pending ? "#B4B2A9" : puffed ? (share >= 0.95 ? "#E24B4A" : "#EF9F27") : nsColor(p.namespace);
  // A sick or pending fish doesn't glow: the healthy ones stand out.
  // Nearer fish (higher z) glow more; far ones fade into the water anyway.
  const glow = (GLOW[sc.phase] || 0) * (1 - sc.k.grey * 0.4) * (0.65 + 0.35 * f.z);
  const face = Math.abs(f.face) < 0.12 ? 0.12 * Math.sign(f.face || 1) : f.face;
  f.look = { sick, pending, puffed, s, X, Y, base, glow, face, lit: glow > 0.02 && !sick && !pending,
    // Neon at night: the glow is the fish's own colour (base is already
    // neon then); by day's edges, a touch of bioluminescent blue-green.
    light: puffed ? base : mixHex(base, BIOLUME, 0.25 * (1 - nightK)),
    // The halo breathes - slowly, each fish on its own beat.
    pulse: 0.72 + 0.28 * Math.sin(t / 1400 + f.ph * 3) };
}

function drawHalos(list) {
  ctx.globalCompositeOperation = "lighter";
  for (const f of list) {
    const g = f.look;
    if (!g.lit) continue;
    const R = g.s * (g.puffed ? 2.2 : 1.8);
    ctx.globalAlpha = g.glow * g.pulse;
    ctx.drawImage(halo(g.light), g.X - R, g.Y - R, R * 2, R * 2);
  }
  ctx.globalCompositeOperation = "source-over";
  ctx.globalAlpha = 1;
}

// Photophores: a row of small light organs along the flank, each pulsing
// a little after the one before, like a lanternfish's. Over the bodies.
function drawLightOrgans(list, t) {
  // Batched like the plankton: one path per colour and brightness level
  // (~20 fills) rather than one fill per dot (~125).
  const groups = new Map();
  for (const f of list) {
    const g = f.look;
    if (!g.lit || g.puffed) continue;
    const color = mixHex(g.light, "#FFFFFF", 0.45), r = Math.max(1, g.s * 0.055);
    for (let i = 0; i < 5; i++) {
      const a = g.glow * (0.35 + 0.65 * Math.max(0, Math.sin(t / 650 + f.ph * 5 - i * 0.8)));
      const key = color + Math.min(3, Math.floor(a * 4));
      if (!groups.has(key)) groups.set(key, { color, a: (Math.min(3, Math.floor(a * 4)) + 0.5) / 4, dots: [] });
      groups.get(key).dots.push([g.X + g.face * (-0.5 + i * 0.22) * g.s, g.Y + 0.12 * g.s, r]);
    }
  }
  ctx.globalCompositeOperation = "lighter";
  for (const { color, a, dots } of groups.values()) {
    ctx.fillStyle = color; ctx.globalAlpha = a;
    ctx.beginPath();
    for (const [x, y, r] of dots) { ctx.moveTo(x + r, y); ctx.arc(x, y, r, 0, 7); }
    ctx.fill();
  }
  ctx.globalCompositeOperation = "source-over";
  ctx.globalAlpha = 1;
}

function drawFish(f, t, sc) {
  const p = f.pod, { sick, pending, puffed, s, X, Y, base, face } = f.look;
  // Glowing things don't fade into the dark the way lit ones fade into
  // water: at night far fish fade half as much, and the deep's dimming
  // doesn't apply to their neon.
  const fade = ((1 - f.z) * 0.5 + sc.pal.dim * 0.25) * (1 - 0.5 * nightK) - sc.pal.dim * 0.25 * nightK * 0.5;
  S = s;
  const own = base; // neon at night (nsColor), the day colour by day
  const c = {
    body: mix(own, sc.haze, fade),
    back: mix(mixHex(own, "#0B1A30", 0.35 - 0.15 * nightK), sc.haze, fade),
    belly: mix(mixHex(own, "#FFFFFF", 0.4), sc.haze, fade),
    fin: mix(mixHex(own, "#FFFFFF", 0.2), sc.haze, fade + 0.1),
    band: mix("#F4F3EE", sc.haze, fade),
  };
  ctx.save();
  ctx.translate(X, Y);
  ctx.globalAlpha = pending ? 0.4 : 1;
  if (sick) ctx.rotate(Math.PI);
  ctx.scale(sick ? 1 : face, 1);
  const wag = sick ? 0 : Math.sin(t / (110 + 400 * (0.0045 - f.sp) / 0.0045) + f.ph) * 0.12;
  let eye;
  if (puffed) {
    const r = s * 0.8;
    ctx.fillStyle = c.fin;
    fin(c.fin, [[-0.7, 0], [-1.1, -0.35 + wag], [-1.1, 0.35 + wag]]);
    ctx.fillStyle = c.body;
    ctx.beginPath();
    for (let a = 0; a < 18; a++) {
      const ang = (a / 18) * Math.PI * 2;
      ctx.lineTo(Math.cos(ang - 0.1) * r, Math.sin(ang - 0.1) * r);
      ctx.lineTo(Math.cos(ang) * r * 1.28, Math.sin(ang) * r * 1.28);
      ctx.lineTo(Math.cos(ang + 0.1) * r, Math.sin(ang + 0.1) * r);
    }
    ctx.fill();
    ctx.fillStyle = c.belly; ctx.beginPath(); ctx.arc(0, r * 0.25, r * 0.7, 0.2, Math.PI - 0.2); ctx.fill();
    eye = [0.45, -0.25];
  } else {
    eye = (SPECIES[NS_SPECIES[p.namespace]] || SPECIES.classic)(s, wag, c);
  }
  const er = Math.max(1.6, s * (puffed ? 0.16 : 0.11));
  ctx.fillStyle = "#F1EFE8"; ctx.beginPath(); ctx.arc(eye[0] * s, eye[1] * s, er, 0, 7); ctx.fill();
  ctx.fillStyle = "#042C53"; ctx.beginPath(); ctx.arc(eye[0] * s + er * 0.25, eye[1] * s, er * 0.5, 0, 7); ctx.fill();
  ctx.restore();
}

// Tags: always on a fish that needs attention; otherwise every fish of one
// namespace at once, a namespace at a time, 10 s each, so you learn who is
// who - the legend highlights which. A tag that would overlap another moves
// up out of its way; warnings are placed first.
let spotNs = null, spotUntil = 0, spotTurn = 0;
function drawTags(t) {
  const placed = [];
  if (t > spotUntil) {
    const present = [...new Set([...fish.values()].filter((f) => !f.leaving).map((f) => f.pod.namespace))].sort();
    spotNs = present.length ? present[spotTurn++ % present.length] : null;
    spotUntil = t + 10000;
  }
  const tags = [];
  for (const f of fish.values()) {
    if (f.leaving || f.sx == null) continue;
    const why = attention(f);
    if (!why && f.pod.namespace !== spotNs) continue;
    tags.push({ why, text: why ? `${shortName(f.pod)} · ${why}` : shortName(f.pod),
      x: f.sx, y: Math.max(H * 0.2, f.sy - f.sr * 1.1 - 16) });
  }
  // Warnings first, then from the bottom up, each nudged up past any overlap.
  tags.sort((a, b) => (!!b.why - !!a.why) || b.y - a.y);
  const hits = (a, b) => Math.abs(a.x - b.x) < (a.w + b.w) / 2 + 4 && Math.abs(a.y - b.y) < (a.h + b.h) / 2 + 3;
  for (const tag of tags) {
    let box = pillBox(tag.text, tag.x, tag.y, 15);
    for (let tries = 0; tries < 12; tries++) {
      const other = placed.find((o) => hits(box, o));
      if (!other) break;
      box = { ...box, y: other.y - (other.h + box.h) / 2 - 3 };
    }
    box.y = Math.max(H * 0.19, box.y);
    placed.push(pill(tag.text, box.x, box.y, tag.why ? "#FAC775" : "#F1EFE8", tag.why ? "rgba(120,60,8,0.85)" : "rgba(4,12,26,0.62)", 15));
  }
}

// Namespaces with their pod counts under the top bar, and what the fish
// mean - over the sky, so shadowed to stay readable on a bright day.
function drawLegend() {
  ctx.shadowColor = "rgba(0,0,0,0.75)"; ctx.shadowBlur = 4;
  ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
  ctx.font = `${Math.round(H * 0.024)}px system-ui, sans-serif`;
  const counts = {};
  for (const p of state.pods) counts[p.namespace] = (counts[p.namespace] || 0) + 1;
  let lx = W * 0.02;
  for (const ns of Object.keys(counts).sort()) {
    ctx.fillStyle = nsColor(ns); ctx.beginPath(); ctx.arc(lx + 6, H * 0.115, 6, 0, 7); ctx.fill();
    const label = `${ns} ${counts[ns]}`;
    if (ns === spotNs) { // the namespace whose fish are named right now
      const w = ctx.measureText(label).width + 30;
      ctx.fillStyle = "rgba(241,239,232,0.2)";
      ctx.beginPath(); ctx.roundRect(lx - 4, H * 0.115 - H * 0.02, w, H * 0.04, H * 0.02); ctx.fill();
    }
    ctx.fillStyle = "#F1EFE8"; ctx.fillText(label, lx + 17, H * 0.123);
    lx += ctx.measureText(label).width + 44;
  }
  ctx.fillStyle = "#E6F1FB";
  ctx.font = `${Math.round(H * 0.021)}px system-ui, sans-serif`;
  ctx.fillText("size = memory  ·  speed = CPU  ·  shape and colour = namespace  ·  puffed = near its memory limit  ·  sandcastles = nodes", W * 0.02, H * 0.152);
  ctx.shadowBlur = 0; ctx.shadowColor = "transparent";
}

// ---- A frame ----

function drawTank(t, dt) {
  const k = dt * 60; // speeds are per 60th of a second
  const sc = scene(), cam = drift(t);
  nightK = (GLOW[sc.phase] || 0) * (1 - sc.k.grey * 0.4);
  if (farKey !== sc.phase + sc.kind + W + "x" + H) paintFar(sc);
  const nClouds = drawSky(sc, t, k);
  ctx.drawImage(far, -W * 0.06 + cam * PAR.far, 0);
  drawRays(sc, t, nClouds);
  drawSurface(t);
  drawPrecipitation(sc, t, dt, k);
  const dim = sc.pal.dim + sc.k.grey * 0.2;
  drawWeed(backWeed, cam * PAR.back, H * (FLOOR + 0.01), [mix(mixHex("#3E7A34", sc.haze, 0.45), sc.pal.deep, dim * 0.5),
    mix(mixHex("#8A7430", sc.haze, 0.45), sc.pal.deep, dim * 0.5)], t);
  drawFloor(sc, cam * PAR.floor, t);
  drawHouses(sc, cam * PAR.floor, t);
  stepFish(t, k);
  const list = [...fish.values()].sort((a, b) => a.z - b.z);
  drawPlankton(sc, t, dt, cam);
  for (const f of list) placeFish(f, t, sc, cam);
  drawHalos(list);
  for (const f of list) drawFish(f, t, sc);
  drawLightOrgans(list, t);
  drawWeed(frontWeed, cam * PAR.front, H * 1.02, [mix("#12301A", sc.pal.deep, dim * 0.6), mix("#2E2A10", sc.pal.deep, dim * 0.6)], t);
  if (sc.kind === "fog") { ctx.fillStyle = "rgba(200,206,212,0.22)"; ctx.fillRect(0, 0, W, H); }
  drawLightning(dt);
  drawTags(t);
  drawLegend();
}


// ---- What display.js calls ----

function describe(p) {
  const mib = (b) => `${Math.round(b / MIB)} MiB`;
  const mem = p.mem_bytes == null ? "memory: no data"
    : `memory ${mib(p.mem_bytes)}` + (p.mem_limit ? ` of ${mib(p.mem_limit)} (${Math.round(memShare(p) * 100)}%)` : ", no limit");
  const cpu = p.cpu_m == null ? "CPU: no data" : `CPU ${p.cpu_m < 10 ? p.cpu_m.toFixed(1) : Math.round(p.cpu_m)}m`;
  return `<b>${p.namespace}/${p.name}</b><br>${p.reason || (p.ready ? "running" : p.phase.toLowerCase())}` +
    ` on ${p.node || "no node"}<br>${mem}<br>${cpu}<br>${p.restarts} restart${p.restarts === 1 ? "" : "s"}`;
}

window.Tank = {
  // A new /api/display: fish join, leave, grow and change speed.
  sync(next) { state = next; syncFish(); },
  draw(t, dt) { if (state) drawTank(t, dt); },
  // Night in the tank: display.js draws it at a lower frame rate then.
  night() { return scene().phase === "night"; },
  resize: sizeCanvas,
  // The details of the fish under a tap (page coordinates), or null.
  detailAt(x, y) {
    const r = canvas.getBoundingClientRect();
    let best = null, bestD = 40;
    for (const f of fish.values()) {
      if (f.sx == null) continue;
      const d = Math.hypot(f.sx - (x - r.left), f.sy - (y - r.top)) - f.sr * 0.6;
      if (d < bestD) { best = f; bestD = d; }
    }
    return best ? describe(best.pod) : null;
  },
};
})();
