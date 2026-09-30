// The Pi's always-on screen: a live map of the homelab, and (tap) an
// aquarium with one fish per pod. Everything drawn comes from /api/display,
// polled every few seconds; see app/display.py for where each value is from.
"use strict";

const POLL_MS = 5000;
const SVG_NS = "http://www.w3.org/2000/svg";
const COLOR = { ok: "#97C459", warn: "#EF9F27", bad: "#E24B4A", unknown: "#5F5E5A" };
const STATUS_COLOR = { up: COLOR.ok, down: COLOR.bad, unknown: COLOR.unknown };

// ---- Map layout (1280 x 720 viewBox, the Pi's screen) ----

const ZONES = [
  { x: 225, y: 110, w: 290, h: 520, label: "Raspberry Pi 5 - host", color: "#1D9E75" },
  { x: 545, y: 212, w: 460, h: 418, label: "K3s cluster", color: "#534AB7", nodes: true },
  { x: 1035, y: 212, w: 225, h: 418, label: "Windows PC", color: "#D85A30" },
];

// id: [x, y, title, subtitle, category color]
const BOXES = {
  lan: [120, 350, "Home network", "PCs, phones, TVs", "#888780"],
  iphone: [120, 530, "iPhone", "Bluetooth", "#888780"],
  coredns: [370, 170, "CoreDNS", "*.home", "#1D9E75"],
  adguard: [370, 290, "AdGuard", "filtering", "#1D9E75"],
  rustdesk: [370, 410, "RustDesk", "ID + relay", "#1D9E75"],
  phone: [370, 530, "Phone bridge", "calls + music", "#1D9E75"],
  internet: [650, 150, "Internet", "upstream DNS", "#444441"],
  traefik: [650, 350, "Traefik", "HTTPS ingress", "#534AB7"],
  apps: [650, 450, "Apps", "Grafana, Argo CD, chat", "#534AB7"],
  api: [880, 300, "FastAPI", "api.home", "#534AB7"],
  postgres: [880, 400, "Postgres", "pgvector", "#BA7517"],
  redis: [880, 480, "Redis", "job queue", "#BA7517"],
  prometheus: [880, 590, "Prometheus", "metrics", "#534AB7"],
  ollama: [1145, 300, "Ollama", "GPU inference", "#D85A30"],
  phone_pc: [1145, 530, "Phone app", "calls, music", "#D85A30"],
};
const BOX_W = 176, BOX_H = 56;

// Lines: [from, to, custom path?]. Rates arrive keyed "from-to".
const EDGES = [
  ["lan", "coredns"], ["coredns", "adguard"], ["adguard", "internet"],
  ["lan", "traefik"], ["traefik", "api"], ["traefik", "apps"],
  ["api", "postgres"], ["api", "redis", "M968 300 C1012 300 1012 480 968 480"],
  ["api", "ollama"], ["lan", "rustdesk"],
  ["iphone", "phone"], ["phone", "phone_pc"],
];

const APPS = ["grafana", "argocd", "chat", "kiwix"];
const APP_NAMES = { grafana: "Grafana", argocd: "Argo CD", chat: "Chat", kiwix: "Wikipedia" };

// ---- State ----

let state = null;
let lastOk = 0;
let view = 0;
const $ = (id) => document.getElementById(id);

// ---- Map ----

const svg = $("arch");
function el(tag, attrs, parent) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  (parent || svg).appendChild(e);
  return e;
}

function anchor(from, to) {
  // Leave from the side facing the other box; stacked boxes use top/bottom.
  const [x1, y1] = BOXES[from], [x2, y2] = BOXES[to];
  if (Math.abs(x1 - x2) < 60) {
    const d = y2 > y1 ? 1 : -1;
    return [x1, y1 + d * BOX_H / 2, x2, y2 - d * BOX_H / 2];
  }
  const d = x2 > x1 ? 1 : -1;
  return [x1 + d * BOX_W / 2, y1, x2 - d * BOX_W / 2, y2];
}

const edges = [];
const boxEls = {};
let nodeLabel;

function buildMap() {
  for (let gx = 20; gx < 1280; gx += 40)
    for (let gy = 80; gy < 680; gy += 40) el("circle", { cx: gx, cy: gy, r: 1, fill: "#232322" });
  for (const z of ZONES) {
    el("rect", { x: z.x, y: z.y, width: z.w, height: z.h, rx: 14, fill: z.color, "fill-opacity": 0.06,
      stroke: z.color, "stroke-opacity": 0.4, "stroke-dasharray": "6 6" });
    el("text", { x: z.x + 14, y: z.y + 24, fill: z.color, "font-size": 15, "font-weight": 600 }).textContent = z.label;
    if (z.nodes) nodeLabel = el("text", { x: z.x + z.w - 14, y: z.y + 24, "text-anchor": "end", "font-size": 14, fill: "#888780" });
  }
  const lines = el("g", { fill: "none", "stroke-width": 2.5, stroke: "#2C2C2A" });
  for (const [from, to, custom] of EDGES) {
    let d = custom;
    if (!d) {
      const [x1, y1, x2, y2] = anchor(from, to);
      d = Math.abs(x1 - x2) < 1 ? `M${x1} ${y1} L${x2} ${y2}` : `M${x1} ${y1} C${(x1 + x2) / 2} ${y1} ${(x1 + x2) / 2} ${y2} ${x2} ${y2}`;
    }
    const path = el("path", { d }, lines);
    edges.push({ id: `${from}-${to}`, path, len: path.getTotalLength(), color: BOXES[to][4], carry: 0 });
  }
  dotLayer = el("g", {});
  for (const [id, [x, y, title, sub, color]] of Object.entries(BOXES)) {
    const g = el("g", { "data-id": id });
    const rect = el("rect", { x: x - BOX_W / 2, y: y - BOX_H / 2, width: BOX_W, height: BOX_H, rx: 10,
      fill: "#141413", stroke: color, "stroke-width": 2 }, g);
    el("rect", { x: x - BOX_W / 2, y: y - BOX_H / 2, width: 5, height: BOX_H, rx: 2, fill: color }, g);
    el("text", { x: x - BOX_W / 2 + 16, y: y - 4, fill: "#F1EFE8", "font-size": 17, "font-weight": 600 }, g).textContent = title;
    const subEl = el("text", { x: x - BOX_W / 2 + 16, y: y + 17, fill: "#888780", "font-size": 13 }, g);
    subEl.textContent = sub;
    const led = el("circle", { cx: x + BOX_W / 2 - 14, cy: y - BOX_H / 2 + 14, r: 5, fill: COLOR.unknown }, g);
    boxEls[id] = { rect, sub: subEl, led, color };
  }
  buildStats();
}

let dotLayer;
const statEls = {};
function buildStats() {
  const items = [["dns", "DNS", "#5DCAA5"], ["blocked", "Blocked today", "#F0997B"], ["pods", "Pods", "#AFA9EC"],
    ["api", "API", "#AFA9EC"], ["temp", "Pi temp", "#EF9F27"], ["cpu", "Pi CPU", "#EF9F27"], ["mem", "Pi memory", "#EF9F27"]];
  items.forEach(([id, label, color], i) => {
    const x = 30 + i * 175;
    el("text", { x, y: 655, fill: "#888780", "font-size": 13 }).textContent = label;
    statEls[id] = el("text", { x, y: 678, fill: color, "font-size": 21, "font-weight": 600 });
    statEls[id].textContent = "-";
  });
}

function serviceStatus(id) {
  if (!state) return "unknown";
  if (id === "lan") return "up";
  if (id === "apps") {
    const s = APPS.map((a) => state.services[a]);
    if (s.includes("down")) return "down";
    return s.every((x) => x === "up") ? "up" : "unknown";
  }
  return state.services[id] || "unknown";
}

function renderMap() {
  for (const id of Object.keys(BOXES)) {
    const s = serviceStatus(id), b = boxEls[id];
    b.led.setAttribute("fill", STATUS_COLOR[s]);
    b.rect.setAttribute("stroke", s === "down" ? COLOR.bad : b.color);
    b.rect.setAttribute("fill", s === "down" ? "#2a1212" : "#141413");
  }
  const st = state.stats;
  const fmt = (v, f) => (v === null || v === undefined ? "-" : f(v));
  statEls.dns.textContent = fmt(st.dns_per_min, (v) => `${Math.round(v)}/min`);
  statEls.blocked.textContent = fmt(st.blocked_pct, (v) => `${Math.round(v)}%`);
  statEls.pods.textContent = `${st.pods_ready}/${st.pods_total}`;
  statEls.api.textContent = fmt(st.api_rps, (v) => `${v < 10 ? v.toFixed(1) : Math.round(v)} req/s`);
  statEls.temp.textContent = fmt(st.pi_temp_c, (v) => `${Math.round(v)}°C`);
  statEls.cpu.textContent = fmt(st.pi_cpu, (v) => `${Math.round(v * 100)}%`);
  statEls.mem.textContent = fmt(st.pi_mem, (v) => `${Math.round(v * 100)}%`);
  const podsUp = (ns, prefix) => state.pods.filter((p) => p.namespace === ns && p.name.startsWith(prefix));
  const api = podsUp("backend", "api-");
  boxEls.api.sub.textContent = api.length ? `api.home - ${api.filter((p) => p.ready).length}/${api.length} pods` : "api.home";
  nodeLabel.textContent = state.nodes.map((n) => `${n.name} ${n.ready ? "ready" : "not ready"}`).join("  ·  ");
  nodeLabel.setAttribute("fill", state.nodes.every((n) => n.ready) ? "#888780" : COLOR.warn);
}

// Dots per second for a request rate: visible at a trickle, capped when busy.
function dotRate(rps) {
  if (!rps || rps <= 0) return 0;
  return Math.min(7, 0.5 + 2.2 * Math.log10(1 + rps * 10));
}

const dots = [];
const SPEED = 230; // px per second
function stepDots(dt) {
  if (!state) return;
  for (const e of edges) {
    e.carry += dotRate(state.rates[e.id]) * dt;
    while (e.carry >= 1) {
      e.carry -= 1;
      // Blocked DNS: red, and turned back before it reaches the internet.
      const blocked = e.id === "adguard-internet" && Math.random() < state.blocked_share;
      const c = el("circle", { r: blocked ? 5 : 4, fill: blocked ? COLOR.bad : e.color }, dotLayer);
      dots.push({ c, e, t: 0, stop: blocked ? 0.35 : 1 });
    }
  }
  for (let i = dots.length - 1; i >= 0; i--) {
    const d = dots[i];
    d.t += (SPEED * dt) / d.e.len;
    if (d.t >= d.stop) { d.c.remove(); dots.splice(i, 1); continue; }
    const p = d.e.path.getPointAtLength(d.t * d.e.len);
    d.c.setAttribute("cx", p.x);
    d.c.setAttribute("cy", p.y);
    if (d.stop < 1) d.c.setAttribute("opacity", Math.min(1, (d.stop - d.t) / 0.12));
  }
}

// ---- Aquarium ----

const NS_COLOR = { backend: "#5DCAA5", data: "#EF9F27", monitoring: "#AFA9EC", argocd: "#F0997B", chat: "#97C459",
  kiwix: "#85B7EB", ai: "#ED93B1", dashboard: "#FAC775", "kube-system": "#E6F1FB" };
const canvas = $("tank"), ctx = canvas.getContext("2d");
let W = 0, H = 0;
function sizeCanvas() {
  const r = canvas.getBoundingClientRect();
  W = canvas.width = Math.round(r.width);
  H = canvas.height = Math.round(r.height);
}

const fish = new Map(); // pod name -> fish
let firstSync = true;
const restartsSeen = new Map();
const rand = (a, b) => a + Math.random() * (b - a);
const bubbles = Array.from({ length: 40 }, () => ({ x: Math.random(), y: Math.random(), r: rand(0.6, 2.4), v: rand(0.0006, 0.002) }));
const weeds = Array.from({ length: 16 }, (_, i) => ({ x: 0.02 + i * 0.064 + rand(0, 0.02), h: rand(0.1, 0.26), c: Math.random() < 0.5 ? "#27500A" : "#3B6D11" }));
const sand = Array.from({ length: 160 }, () => [Math.random(), rand(0.9, 1), Math.random()]);

// What a fish's look means (metrics from /api/display, via metrics-server):
// size is memory in use, on a log scale (8 MiB small, 1 GiB big); speed is
// CPU; a pod near its memory limit puffs up - the warning before an OOM kill.
const MIB = 2 ** 20;
const PUFF_AT = 0.85;
function sizeFor(p) {
  if (p.mem_bytes == null) return 0.9;
  const t = Math.min(1, Math.max(0, (Math.log2(p.mem_bytes / MIB) - 3) / 7));
  return 0.6 + t * 1.3;
}
function speedFor(p) {
  if (p.cpu_m == null) return 0.0006;
  return Math.min(0.0045, 0.00025 + 0.00045 * Math.log2(1 + p.cpu_m));
}
function memShare(p) {
  return p.mem_bytes != null && p.mem_limit ? p.mem_bytes / p.mem_limit : null;
}

function syncFish() {
  const seen = new Set();
  for (const p of state.pods) {
    seen.add(p.name);
    let f = fish.get(p.name);
    if (!f) {
      // Already in the tank on the first load; new pods swim in from a side.
      const fromLeft = Math.random() < 0.5;
      f = { x: firstSync ? rand(0.05, 0.95) : fromLeft ? -0.05 : 1.05, y: rand(0.31, 0.75), dir: fromLeft ? 1 : -1,
        s: sizeFor(p), sp: speedFor(p), jitter: rand(0.85, 1.15), ph: rand(0, 6), z: Math.random(), leaving: false };
      fish.set(p.name, f);
    }
    const before = restartsSeen.get(p.name);
    if (before !== undefined && p.restarts > before) f.restartedAt = Date.now();
    restartsSeen.set(p.name, p.restarts);
    f.pod = p;
    f.targetS = sizeFor(p);
    f.targetSp = speedFor(p) * f.jitter;
    f.leaving = false;
  }
  for (const [name, f] of fish) if (!seen.has(name)) f.leaving = true;
  firstSync = false;
}

function drawFish(f, t) {
  const p = f.pod, sick = p.phase === "Running" && !p.ready || p.reason, pending = p.phase === "Pending";
  const s = f.s * W * 0.017;
  const share = memShare(p), puffed = !sick && !pending && share !== null && share >= PUFF_AT;
  const X = f.x * W, Y = f.y * H + (sick ? 0 : Math.sin(t / 800 + f.ph) * H * 0.01);
  ctx.save();
  ctx.translate(X, Y);
  ctx.globalAlpha = pending ? 0.35 : 0.55 + f.z * 0.45;
  if (sick) ctx.rotate(Math.PI);
  ctx.scale(f.dir, 1);
  ctx.fillStyle = sick || pending ? "#B4B2A9" : puffed ? (share >= 0.95 ? "#E24B4A" : "#EF9F27") : NS_COLOR[p.namespace] || "#D3D1C7";
  if (puffed) {
    // A pufferfish: round, with spikes.
    const r = s * 0.8;
    ctx.beginPath(); ctx.arc(0, 0, r, 0, 7); ctx.fill();
    ctx.beginPath();
    for (let a = 0; a < 16; a++) {
      const ang = (a / 16) * Math.PI * 2;
      ctx.moveTo(Math.cos(ang - 0.12) * r, Math.sin(ang - 0.12) * r);
      ctx.lineTo(Math.cos(ang) * r * 1.3, Math.sin(ang) * r * 1.3);
      ctx.lineTo(Math.cos(ang + 0.12) * r, Math.sin(ang + 0.12) * r);
    }
    ctx.fill();
  } else {
    ctx.beginPath();
    ctx.moveTo(s * 1.1, 0);
    ctx.quadraticCurveTo(s * 0.4, -s * 0.62, -s * 0.8, -s * 0.08);
    ctx.quadraticCurveTo(s * 0.4, s * 0.62, s * 1.1, 0);
    ctx.fill();
  }
  const wag = sick ? 0 : Math.sin(t / 140 + f.ph) * s * 0.2;
  ctx.beginPath();
  ctx.moveTo(-s * 0.7, 0); ctx.lineTo(-s * 1.35, -s * 0.45 + wag); ctx.lineTo(-s * 1.2, 0); ctx.lineTo(-s * 1.35, s * 0.45 + wag);
  ctx.closePath(); ctx.fill();
  ctx.globalAlpha *= 0.5;
  ctx.beginPath(); ctx.moveTo(0, -s * 0.35); ctx.lineTo(s * 0.2, -s * 0.7); ctx.lineTo(s * 0.45, -s * 0.3); ctx.fill();
  ctx.globalAlpha = pending ? 0.35 : 1;
  ctx.fillStyle = "#F1EFE8"; ctx.beginPath(); ctx.arc(s * 0.62, -s * 0.1, s * 0.11, 0, 7); ctx.fill();
  ctx.fillStyle = "#042C53"; ctx.beginPath(); ctx.arc(s * 0.65, -s * 0.1, s * 0.055, 0, 7); ctx.fill();
  ctx.restore();
  const recent = f.restartedAt && Date.now() - f.restartedAt < 3600e3;
  const label = p.reason || (pending ? "pending" : sick ? "not ready"
    : puffed ? `${Math.round(share * 100)}% of memory limit` : recent ? "restarted" : null);
  if (label) {
    ctx.fillStyle = sick || (puffed && share >= 0.95) ? "#F09595" : "#FAC775";
    ctx.font = `${Math.round(H * 0.024)}px system-ui, sans-serif`;
    ctx.textAlign = "center";
    ctx.fillText(label, X, Y - s * (puffed ? 1.25 : 0.95));
  }
}

// ---- Tank weather ----
// The sky above the water, the water's colour and the light in it follow the
// real weather and time of day (Open-Meteo via /api/display: sunrise and
// sunset, weather code, cloud cover, rain, wind). Add ?wx=rain&phase=night
// (or clear/partly/overcast/fog/drizzle/snow/storm, dawn/day/dusk) to the
// page's address to preview any of them.
const SURF = 0.245; // the water's surface, as a fraction of the height
const TOP_BAR = 0.078;
const params = new URLSearchParams(location.search);

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

const PHASES = {
  day: { sky: ["#5E9AD6", "#A9CFEF"], top: "#1C6FB3", deep: "#042C53", ray: "#CFE8FF", grey: "#8A929B" },
  dawn: { sky: ["#5E5588", "#F2A07B"], top: "#3C5D8F", deep: "#06203F", ray: "#FFD2A8", grey: "#77737D" },
  dusk: { sky: ["#35336A", "#E8845A"], top: "#34507F", deep: "#051A35", ray: "#FFC08A", grey: "#5E5A66" },
  night: { sky: ["#040A1A", "#13224A"], top: "#0B2447", deep: "#020A18", ray: "#B5C8E8", grey: "#1C222C" },
};
// How grey the sky goes, how strong the light rays are, and how many clouds.
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

function hexRgb(h) { return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)); }
function mix(a, b, t) {
  const x = hexRgb(a), y = hexRgb(b);
  return `rgb(${x.map((v, i) => Math.round(v + (y[i] - v) * t)).join(",")})`;
}

function scene() {
  const w = state && state.weather;
  const nowS = Date.now() / 1000;
  const kind = params.get("wx") || wxKind(w && w.code);
  const phase = params.get("phase") || dayPhase(w, nowS);
  const cover = w && w.cloud_cover != null && !params.get("wx") ? w.cloud_cover / 100 : null;
  // Sun or moon across the sky: sunrise to sunset, then sunset to sunrise.
  let arc = 0.5;
  if (w && w.sunrise && w.sunset && !params.get("phase")) {
    const day = w.sunset - w.sunrise;
    arc = phase === "night" ? ((nowS - w.sunset + 86400) % 86400) / (86400 - day) : (nowS - w.sunrise) / day;
  }
  return { kind, phase, pal: PHASES[phase] || PHASES.day, k: KINDS[kind] || KINDS.clear, cover,
    precip: w && w.precip_mm, wind: (w && w.wind_mph) || 4, arc: Math.min(1, Math.max(0, arc)) };
}

const stars = Array.from({ length: 60 }, () => [Math.random(), rand(TOP_BAR, SURF - 0.02), Math.random() * 6]);
const clouds = Array.from({ length: 12 }, () => ({ x: Math.random() * 1.3 - 0.15, y: rand(0.1, SURF - 0.045), w: rand(0.08, 0.16) }));
const drops = [], ripples = [], flakes = [];
let flash = 0, bolt = null, nextBolt = 0, dropCarry = 0;

function drawSkyAndWater(t, dt, k) {
  const sc = scene(), pal = sc.pal, surf = SURF * H;
  // Sky, greyed by the weather.
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
    // Sun by day, moon by night, crossing the sky - kept below the legend
    // rows, a little higher at midday.
    const bx = (0.08 + sc.arc * 0.84) * W;
    const by = surf - H * 0.04 - Math.sin(sc.arc * Math.PI) * H * 0.025;
    const r = H * 0.028;
    const night = sc.phase === "night";
    const glow = ctx.createRadialGradient(bx, by, r * 0.5, bx, by, r * 4);
    glow.addColorStop(0, night ? "rgba(230,235,245,0.35)" : "rgba(255,240,200,0.55)");
    glow.addColorStop(1, "rgba(255,240,200,0)");
    ctx.fillStyle = glow; ctx.fillRect(bx - r * 4, by - r * 4, r * 8, r * 8);
    ctx.fillStyle = night ? "#E6E9F0" : sc.phase === "day" ? "#FFF1C4" : "#FFC98A";
    ctx.beginPath(); ctx.arc(bx, by, r, 0, 7); ctx.fill();
  }
  // Clouds drift with the wind; how many from the real cloud cover.
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
  // Water, darker under a grey sky or at night.
  const water = ctx.createLinearGradient(0, surf, 0, H);
  water.addColorStop(0, mix(pal.top, pal.grey, sc.k.grey * 0.5));
  water.addColorStop(1, pal.deep);
  ctx.fillStyle = water; ctx.fillRect(0, surf, W, H - surf);
  // Light rays from the surface: strong on a clear day, faint moonlight at night.
  const rays = sc.k.rays * (sc.phase === "night" ? 0.35 : sc.phase === "day" ? 1 : 0.7);
  if (rays > 0.02) {
    ctx.fillStyle = pal.ray;
    for (let i = 0; i < 6; i++) {
      const bx = (i * 0.19 + Math.sin(t / 5000 + i) * 0.03) * W;
      // A cloud passing over dims the ray under it.
      let shade = 1;
      for (let j = 0; j < nClouds; j++) if (Math.abs(clouds[j].x * W - (bx + W * 0.07)) < clouds[j].w * W * 0.6) shade = 0.4;
      ctx.globalAlpha = 0.12 * rays * shade;
      ctx.beginPath(); ctx.moveTo(bx, surf); ctx.lineTo(bx + W * 0.05, surf); ctx.lineTo(bx + W * 0.17, H * 0.92); ctx.lineTo(bx + W * 0.09, H * 0.92); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  // The surface: a gently moving line.
  ctx.strokeStyle = "rgba(255,255,255,0.35)"; ctx.lineWidth = 2; ctx.beginPath();
  for (let i = 0; i <= 40; i++) {
    const x = (i / 40) * W, y = surf + Math.sin(i * 0.9 + t / 700) * H * 0.004;
    if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
  }
  ctx.stroke();
  drawPrecipitation(sc, t, dt, k, surf);
  if (sc.kind === "fog") { ctx.fillStyle = "rgba(200,206,212,0.22)"; ctx.fillRect(0, 0, W, H); }
}

function drawPrecipitation(sc, t, dt, k, surf) {
  const raining = sc.kind === "rain" || sc.kind === "drizzle" || sc.kind === "storm";
  if (raining) {
    // Drops per second: a drizzle, a rain that follows the real mm/h, a storm.
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
      const f = flakes[i];
      const inWater = f.y > surf;
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

// Over everything, fish included: a lightning flash lights the whole tank.
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

function drawTank(t, dt) {
  const k = dt * 60; // speeds below are per 60th of a second
  drawSkyAndWater(t, dt, k);
  ctx.fillStyle = "#633806"; ctx.beginPath(); ctx.moveTo(0, H);
  for (let i = 0; i <= 30; i++) ctx.lineTo((i / 30) * W, H * 0.88 + Math.sin(i * 1.3) * H * 0.015);
  ctx.lineTo(W, H); ctx.fill();
  ctx.fillStyle = "#854F0B"; for (const [a, b, r] of sand) ctx.fillRect(a * W, b * H, 1 + r * 3, 1 + r * 3);
  // One rock per node, named, grey when the node isn't ready.
  const nodes = state ? state.nodes : [];
  nodes.forEach((n, i) => {
    const rx = (i + 1) / (nodes.length + 1);
    ctx.fillStyle = n.ready ? "#5F5E5A" : "#2C2C2A";
    ctx.beginPath(); ctx.ellipse(rx * W, H * 0.91, W * 0.08, H * 0.09, 0, Math.PI, 0); ctx.fill();
    ctx.fillStyle = "#444441";
    ctx.beginPath(); ctx.ellipse(rx * W + W * 0.055, H * 0.92, W * 0.035, H * 0.05, 0, Math.PI, 0); ctx.fill();
  });
  ctx.lineCap = "round";
  for (const w of weeds) {
    ctx.strokeStyle = w.c; ctx.lineWidth = W * 0.005; ctx.beginPath(); ctx.moveTo(w.x * W, H * 0.95);
    for (let k = 1; k <= 7; k++) ctx.lineTo(w.x * W + Math.sin(t / 1000 + k * 0.7 + w.x * 11) * k * W * 0.0028, H * 0.95 - (k / 7) * w.h * H);
    ctx.stroke();
  }
  // Rock names over the weeds.
  ctx.font = `600 ${Math.round(H * 0.026)}px system-ui, sans-serif`; ctx.textAlign = "center";
  nodes.forEach((n, i) => {
    ctx.fillStyle = n.ready ? "#F1EFE8" : "#F09595";
    ctx.fillText(n.ready ? n.name : `${n.name} - not ready`, ((i + 1) / (nodes.length + 1)) * W, H * 0.885);
  });
  ctx.fillStyle = "#B5D4F4";
  for (const b of bubbles) {
    b.y -= b.v * k; b.x += Math.sin(t / 600 + b.r * 9) * 0.0003 * k;
    if (b.y < SURF + 0.01) { b.y = 0.93; b.x = rand(0.05, 0.95); }
    ctx.globalAlpha = 0.4; ctx.beginPath(); ctx.arc(b.x * W, b.y * H, b.r * 2, 0, 7); ctx.fill();
  }
  ctx.globalAlpha = 1;
  const list = [...fish.entries()].sort((a, b) => a[1].z - b[1].z);
  for (const [name, f] of list) {
    const p = f.pod, sick = (p.phase === "Running" && !p.ready) || p.reason;
    // Ease toward the latest size and speed, so a change reads as growth.
    f.s += (f.targetS - f.s) * Math.min(1, 0.03 * k);
    f.sp += (f.targetSp - f.sp) * Math.min(1, 0.05 * k);
    if (f.leaving) { f.x += f.dir * 0.004 * k; if (f.x > 1.1 || f.x < -0.1) { fish.delete(name); continue; } }
    else if (sick) f.y = Math.max(SURF + 0.03, f.y - 0.0004 * k);
    else if (p.phase === "Pending") f.y = Math.min(0.8, f.y + 0.0003 * k);
    else {
      f.x += f.dir * f.sp * k;
      if ((f.x > 0.97 && f.dir > 0) || (f.x < 0.03 && f.dir < 0)) f.dir *= -1;
    }
    drawFish(f, t);
  }
  drawLightning(dt);
  // Namespace legend, under the top bar - over the sky, so shadowed to stay
  // readable against a bright day.
  ctx.shadowColor = "rgba(0,0,0,0.75)"; ctx.shadowBlur = 4;
  let lx = W * 0.02;
  ctx.font = `${Math.round(H * 0.024)}px system-ui, sans-serif`; ctx.textAlign = "left";
  const present = [...new Set(state ? state.pods.map((p) => p.namespace) : [])].sort();
  for (const ns of present) {
    ctx.fillStyle = NS_COLOR[ns] || "#D3D1C7"; ctx.beginPath(); ctx.arc(lx + 6, H * 0.115, 6, 0, 7); ctx.fill();
    ctx.fillStyle = "#F1EFE8"; ctx.fillText(ns, lx + 17, H * 0.123);
    lx += ctx.measureText(ns).width + 44;
  }
  ctx.fillStyle = "#E6F1FB";
  ctx.font = `${Math.round(H * 0.021)}px system-ui, sans-serif`;
  ctx.fillText("size = memory  ·  speed = CPU  ·  puffed = near its memory limit", W * 0.02, H * 0.152);
  ctx.shadowBlur = 0; ctx.shadowColor = "transparent";
}

// ---- Top and bottom bars ----

const WEATHER = [[0, "clear"], [3, "partly cloudy"], [48, "fog"], [57, "drizzle"], [67, "rain"], [77, "snow"], [82, "showers"], [86, "snow showers"], [99, "thunderstorms"]];
function weatherText(w) {
  if (!w) return "";
  const kind = (WEATHER.find(([max]) => w.code <= max) || [0, ""])[1];
  return `${w.temp_f}° ${kind === "clear" && !w.is_day ? "clear night" : kind}`;
}

function renderBars() {
  $("weather").textContent = weatherText(state.weather);
  const all = Object.keys(BOXES).filter((id) => id !== "lan").map(serviceStatus);
  const down = all.filter((s) => s === "down").length;
  const known = all.filter((s) => s !== "unknown").length;
  const alerts = (state.alerts || []).length;
  const health = $("health");
  if (down || alerts) {
    health.className = "pill bad";
    health.textContent = [down && `${down} down`, alerts && `${alerts} alert${alerts > 1 ? "s" : ""}`].filter(Boolean).join(", ");
  } else {
    health.className = known === all.length ? "pill ok" : "pill warn";
    health.textContent = known === all.length ? `All ${all.length} services healthy` : `${known} of ${all.length} reporting`;
  }
  const ph = state.phone;
  const [color, text] = ph.connected === null ? [COLOR.unknown, "Phone: no data"]
    : ph.in_call ? ["#D4537E", "On a call"]
    : ph.connected ? [COLOR.ok, "iPhone connected"] : [COLOR.unknown, "iPhone away"];
  $("phone").innerHTML = `<span class="dot" style="background:${color}"></span>${text}`;
}

let tickIndex = 0;
function nextTick() {
  const events = state ? state.events : [];
  if (!events.length) return;
  const e = events[tickIndex++ % events.length];
  const tk = $("ticker");
  tk.style.opacity = 0;
  setTimeout(() => {
    tk.textContent = e.text;
    $("ticker-dot").style.background = { ok: COLOR.ok, info: "#85B7EB", warn: COLOR.warn, bad: COLOR.bad }[e.level];
    tk.style.opacity = 1;
  }, 400);
}

function renderClock() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("date").textContent = now.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  // Pick up new deploys: reload once a night.
  if (now.getHours() === 4 && now.getMinutes() === 0 && performance.now() > 120e3) location.reload();
}

// ---- Data ----

async function poll() {
  try {
    const r = await fetch("/api/display", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    state = await r.json();
    lastOk = Date.now();
    renderMap();
    renderBars();
    syncFish();
  } catch (err) {
    // Keep drawing the last good state; say so once it's stale.
  }
  $("offline").style.display = Date.now() - lastOk > 3 * POLL_MS ? "block" : "none";
}

// ---- Interaction: tap to switch views; hold on something for details ----

function showDetail(text, x, y) {
  const d = $("detail");
  d.innerHTML = text;
  d.style.display = "block";
  d.style.left = Math.min(x + 12, innerWidth - 340) + "px";
  d.style.top = Math.max(y - 60, 64) + "px";
  clearTimeout(showDetail.timer);
  showDetail.timer = setTimeout(() => (d.style.display = "none"), 4000);
}

function boxDetail(id) {
  if (!state) return null;
  const [, , title] = BOXES[id];
  if (id === "apps") return `<b>Apps</b><br>` + APPS.map((a) => `${APP_NAMES[a]}: ${state.services[a]}`).join("<br>");
  return `<b>${title}</b><br>${serviceStatus(id)}`;
}

function fishAt(x, y) {
  let best = null, bestD = 0.05;
  for (const f of fish.values()) {
    const d = Math.hypot(f.x - x, (f.y - y) * 0.6);
    if (d < bestD) { best = f; bestD = d; }
  }
  return best;
}

document.addEventListener("contextmenu", (e) => e.preventDefault());
$("screen").addEventListener("click", (e) => {
  if (view === 0) {
    const g = e.target.closest && e.target.closest("g[data-id]");
    if (g) return showDetail(boxDetail(g.dataset.id), e.clientX, e.clientY);
  } else {
    const r = canvas.getBoundingClientRect();
    const f = fishAt((e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height);
    if (f) {
      const p = f.pod;
      const mib = (b) => `${Math.round(b / MIB)} MiB`;
      const mem = p.mem_bytes == null ? "memory: no data"
        : `memory ${mib(p.mem_bytes)}` + (p.mem_limit ? ` of ${mib(p.mem_limit)} (${Math.round(memShare(p) * 100)}%)` : ", no limit");
      const cpu = p.cpu_m == null ? "CPU: no data" : `CPU ${p.cpu_m < 10 ? p.cpu_m.toFixed(1) : Math.round(p.cpu_m)}m`;
      return showDetail(`<b>${p.namespace}/${p.name}</b><br>${p.reason || (p.ready ? "running" : p.phase.toLowerCase())}` +
        ` on ${p.node || "no node"}<br>${mem}<br>${cpu}<br>${p.restarts} restart${p.restarts === 1 ? "" : "s"}`, e.clientX, e.clientY);
    }
  }
  view = 1 - view;
  $("arch-view").classList.toggle("hidden", view !== 0);
  $("tank-view").classList.toggle("hidden", view !== 1);
  $("title").textContent = view ? "Cluster aquarium" : "Homelab";
  $("pager").children[0].classList.toggle("on", view === 0);
  $("pager").children[1].classList.toggle("on", view === 1);
  $("detail").style.display = "none";
  if (view === 1) sizeCanvas();
});

// ---- Loop ----

// 24 fps, not the screen's 60: smooth enough for dots and fish, and at 60
// the Pi's Chromium spent about a core and a half redrawing.
const FRAME_MS = 1000 / 24;
let prev = performance.now();
function frame(t) {
  requestAnimationFrame(frame);
  if (t - prev < FRAME_MS - 2) return;
  const dt = Math.min((t - prev) / 1000, 0.1);
  prev = t;
  if (view === 0) stepDots(dt);
  else if (state) drawTank(t, dt);
}

buildMap();
sizeCanvas();
addEventListener("resize", sizeCanvas);
renderClock();
setInterval(renderClock, 1000);
poll();
setInterval(poll, POLL_MS);
setInterval(nextTick, 5000);
setTimeout(nextTick, 1500);
requestAnimationFrame(frame);
