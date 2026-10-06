// The Pi's always-on screen: a live map of the homelab, and (tap) an
// aquarium with one fish per pod (drawn by tank.js). Everything drawn comes
// from /api/display, polled every few seconds; see app/display.py for where
// each value is from.
"use strict";

const POLL_MS = 5000;
const SVG_NS = "http://www.w3.org/2000/svg";
// Green, amber and red mean health and nothing else. Everything else on
// the map takes its zone's colour (ZONE): where a thing runs, not how it is.
const COLOR = { ok: "#97C459", warn: "#EF9F27", bad: "#E24B4A", unknown: "#5F5E5A" };
const ZONE = { pi: "#1D9E75", cluster: "#534AB7", pc: "#378ADD", outside: "#888780", internet: "#444441" };
// Light versions of the zone colours, for text on the dark background.
const ZONE_TEXT = { pi: "#5DCAA5", cluster: "#AFA9EC", pc: "#85B7EB", outside: "#B4B2A9" };
const STATUS_COLOR = { up: COLOR.ok, down: COLOR.bad, unknown: COLOR.unknown };

// ---- Map layout (1280 x 720 viewBox, the Pi's screen) ----

const ZONES = [
  { x: 225, y: 110, w: 290, h: 520, label: "Raspberry Pi 5 - host", color: ZONE.pi },
  { x: 545, y: 212, w: 460, h: 418, label: "K3s cluster", color: ZONE.cluster, nodes: true },
  // Blue, not the orange it was: orange read as a warning.
  { x: 1035, y: 212, w: 225, h: 418, label: "Windows PC", color: ZONE.pc },
];

// id: [x, y, title, subtitle, category color]
const BOXES = {
  lan: [120, 350, "Home network", "PCs, phones, TVs", ZONE.outside],
  iphone: [120, 530, "iPhone", "Bluetooth", ZONE.outside],
  mac: [120, 600, "Mac", "backups, m1-node", ZONE.outside],
  coredns: [370, 170, "CoreDNS", "*.home", ZONE.pi],
  adguard: [370, 290, "AdGuard", "filtering", ZONE.pi],
  rustdesk: [370, 410, "RustDesk", "ID + relay", ZONE.pi],
  phone: [370, 530, "Phone bridge", "calls + music", ZONE.pi],
  backup: [370, 600, "Backup", "nightly, encrypted", ZONE.pi],
  internet: [650, 150, "Internet", "upstream DNS", ZONE.internet],
  traefik: [650, 350, "Traefik", "HTTPS ingress", ZONE.cluster],
  apps: [650, 450, "Apps", "Grafana, Argo CD, chat", ZONE.cluster],
  api: [880, 300, "FastAPI", "api.home", ZONE.cluster],
  postgres: [880, 400, "Postgres", "pgvector", ZONE.cluster],
  redis: [880, 480, "Redis", "job queue", ZONE.cluster],
  prometheus: [880, 590, "Prometheus", "metrics", ZONE.cluster],
  ollama: [1145, 300, "Ollama", "GPU inference", ZONE.pc],
  phone_pc: [1145, 530, "Phone app", "calls, music", ZONE.pc],
};
const BOX_W = 176, BOX_H = 56;

// Lines: [from, to, custom path?]. Rates arrive keyed "from-to".
const EDGES = [
  ["lan", "coredns"], ["coredns", "adguard"], ["adguard", "internet"],
  ["lan", "traefik"], ["traefik", "api"], ["traefik", "apps"],
  ["api", "postgres"], ["api", "redis", "M968 300 C1012 300 1012 480 968 480"],
  ["api", "ollama"], ["lan", "rustdesk"], ["rustdesk", "lan"], // input in, the Pi's screen out
  ["iphone", "phone"], ["phone", "phone_pc"],
  // The way back along the same lines: the PC's mic and its agent's
  // check-ins (to the bridge), and the mic on to the iPhone.
  ["phone_pc", "phone"], ["phone", "iphone"],
  // Chat (in Apps) to the api, and Argo CD (in Apps) checking GitHub.
  ["apps", "api"], ["apps", "internet", "M738 438 C786 438 786 150 738 150"],
  // Prometheus scraping, the data flowing into it. The Pi's node_exporter
  // (with the phone bridge's, RustDesk's and backups' numbers) is drawn
  // from the phone bridge; two routes run down the gap beside the Pi.
  ["traefik", "prometheus"],
  ["api", "prometheus", "M968 316 C1026 316 1026 590 968 590"],
  ["adguard", "prometheus", "M458 302 C530 302 530 302 530 340 L530 600 Q530 612 545 612 L792 612"],
  ["phone", "prometheus", "M458 544 C520 544 520 544 520 560 L520 588 Q520 600 535 600 L792 600"],
  // The nightly backup from the Pi to the Mac.
  ["backup", "mac"],
];

const APPS = ["grafana", "argocd", "chat", "kiwix"];

// ---- State ----

let state = null;
let lastOk = 0;
let pageVersion = null; // the page_version this page was loaded with, from its first poll
let view = 0;
const $ = (id) => document.getElementById(id);

// ---- Map ----

const svg = $("arch");
// The stats row sits under the top bar; the map (zones, boxes, lines and
// dots, all in this group) is drawn below it. Its coordinates are the
// map's own - the group moves it down MAP_TOP px.
const MAP_TOP = 26;
const mapLayer = document.createElementNS(SVG_NS, "g");
mapLayer.setAttribute("transform", `translate(0 ${MAP_TOP})`);
svg.appendChild(mapLayer);
function el(tag, attrs, parent) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  (parent || mapLayer).appendChild(e);
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
    // Sampled once, every ~3 px: dots look their position up here rather
    // than asking the browser to measure the curve for each dot each frame.
    const len = path.getTotalLength(), n = Math.max(2, Math.ceil(len / 3) + 1), pts = new Float32Array(n * 2);
    for (let k = 0; k < n; k++) {
      const p = path.getPointAtLength((len * k) / (n - 1));
      pts[k * 2] = p.x; pts[k * 2 + 1] = p.y;
    }
    edges.push({ id: `${from}-${to}`, len, pts, n, color: BOXES[to][4], carry: 0 });
  }
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

const statEls = {};
const STAT_TEXT = "#F1EFE8";
// When a vital turns amber, then red: [warn, bad].
const STAT_LIMITS = {
  piTemp: [70, 80], // °C; the Pi 5 throttles at 85
  macTemp: [85, 95],
  pcCpuTemp: [80, 90],
  pcGpuTemp: [80, 87],
  cpu: [0.8, 0.95],
  mem: [0.85, 0.95],
};
function level(v, limits) {
  if (v === null || v === undefined || !limits) return null;
  return v >= limits[1] ? "bad" : v >= limits[0] ? "warn" : null;
}
// A stat as parts, each coloured on its own: "59° · 18% · 46%" with only
// the part that's too high in amber or red.
function setParts(textEl, parts) {
  textEl.replaceChildren();
  parts.forEach(([text, lvl], i) => {
    if (i) el("tspan", { fill: "#5F5E5A" }, textEl).textContent = "  ·  ";
    el("tspan", { fill: lvl ? COLOR[lvl] : STAT_TEXT }, textEl).textContent = text;
  });
}
function buildStats() {
  // Each machine is one cell (temp · CPU · memory): seven separate cells
  // per machine wouldn't fit across 1280.
  // The label says where (its zone's colour); the number is plain white
  // unless it's past a threshold (STAT_LIMITS), then amber or red.
  const items = [["dns", "DNS", ZONE_TEXT.pi], ["blocked", "Blocked today", ZONE_TEXT.pi], ["pods", "Pods", ZONE_TEXT.cluster],
    ["api", "API", ZONE_TEXT.cluster], ["pi", "Pi  temp · CPU · mem", ZONE_TEXT.pi], ["mac", "Mac  temp · CPU · mem", ZONE_TEXT.outside],
    ["pc", "PC  CPU° · GPU° · CPU · mem", ZONE_TEXT.pc]];
  // A machine's cell is ~150-200 wide in practice ("57° · 25% · 47%"; the PC's,
  // with its GPU, "55° · 44° · 4% · 38%"); the PC's ends well inside 1280.
  const xs = [30, 160, 300, 390, 520, 765, 1000];
  items.forEach(([id, label, color], i) => {
    const x = xs[i];
    // Right under the top bar, outside the map's group.
    el("text", { x, y: 78, fill: color, "font-size": 13 }, svg).textContent = label;
    statEls[id] = el("text", { x, y: 101, fill: STAT_TEXT, "font-size": 21, "font-weight": 600 }, svg);
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
  setParts(statEls.pods, [[`${st.pods_ready}/${st.pods_total}`, st.pods_ready < st.pods_total ? "warn" : null]]);
  statEls.api.textContent = fmt(st.api_rps, (v) => `${v < 10 ? v.toFixed(1) : Math.round(v)} req/s`);
  const deg = (v) => fmt(v, (x) => `${Math.round(x)}°`), pct = (v) => fmt(v, (x) => `${Math.round(x * 100)}%`);
  const L = STAT_LIMITS;
  setParts(statEls.pi, [[deg(st.pi_temp_c), level(st.pi_temp_c, L.piTemp)], [pct(st.pi_cpu), level(st.pi_cpu, L.cpu)],
    [pct(st.pi_mem), level(st.pi_mem, L.mem)]]);
  setParts(statEls.mac, [[deg(st.mac_temp_c), level(st.mac_temp_c, L.macTemp)], [pct(st.mac_cpu), level(st.mac_cpu, L.cpu)],
    [pct(st.mac_mem), level(st.mac_mem, L.mem)]]);
  setParts(statEls.pc, [[deg(st.pc_temp_c), level(st.pc_temp_c, L.pcCpuTemp)], [deg(st.pc_gpu_temp_c), level(st.pc_gpu_temp_c, L.pcGpuTemp)],
    [pct(st.pc_cpu), level(st.pc_cpu, L.cpu)], [pct(st.pc_mem), level(st.pc_mem, L.mem)]]);
  const podsUp = (ns, prefix) => state.pods.filter((p) => p.namespace === ns && p.name.startsWith(prefix));
  const api = podsUp("backend", "api-");
  boxEls.api.sub.textContent = api.length ? `api.home - ${api.filter((p) => p.ready).length}/${api.length} pods` : "api.home";
  nodeLabel.textContent = state.nodes.map((n) => `${n.name} ${n.ready ? "ready" : "not ready"}`).join("  ·  ");
  nodeLabel.setAttribute("fill", state.nodes.every((n) => n.ready) ? "#888780" : COLOR.warn);
}

// A fixed pattern per line state (app/display.py, edge_states), not a dot
// per request: a steady stream while traffic flows, a slow trickle while
// both ends are up and quiet, a few red dots that stop short when one is
// down, nothing without data. Dots per second.
const PATTERN = { active: 2, idle: 0.33, down: 0.6, unknown: 0 };

// The dots are drawn on a canvas over the map, not as SVG circles: moving
// SVG elements every frame made Chromium repaint the whole map - boxes,
// lines and text - 16 times a second. Now the SVG only changes when a
// status does (every few seconds), and a frame is one small canvas redraw.
const dots = [];
const SPEED = 230; // px per second
const dotCanvas = $("arch-dots"), dctx = dotCanvas.getContext("2d");
let dScale = 1, dOx = 0, dOy = 0, dotsDrawn = false;
function sizeDots() {
  // The SVG's viewBox is 1280 x 720, scaled to fit (xMidYMid meet).
  const r = dotCanvas.getBoundingClientRect(), dpr = devicePixelRatio || 1;
  dotCanvas.width = Math.round(r.width * dpr);
  dotCanvas.height = Math.round(r.height * dpr);
  const s = Math.min(r.width / 1280, r.height / 720);
  dScale = s * dpr;
  dOx = ((r.width - 1280 * s) / 2) * dpr;
  dOy = ((r.height - 720 * s) / 2) * dpr;
}
function stepDots(dt) {
  if (!state) return;
  for (const e of edges) {
    const how = (state.links && state.links[e.id]) || "unknown";
    e.carry += PATTERN[how] * dt;
    while (e.carry >= 1) {
      e.carry -= 1;
      // Down: red, and stopping short. Blocked DNS too: the blocked share
      // of the stream to the internet, turned back before it gets there.
      const red = how === "down" || (e.id === "adguard-internet" && Math.random() < state.blocked_share);
      dots.push({ e, t: 0, stop: red ? 0.35 : 1, r: red ? 5 : 4, color: red ? COLOR.bad : e.color });
    }
  }
  if (!dots.length && !dotsDrawn) return; // nothing moving, nothing to clear
  dctx.clearRect(0, 0, dotCanvas.width, dotCanvas.height);
  for (let i = dots.length - 1; i >= 0; i--) {
    const d = dots[i], e = d.e;
    d.t += (SPEED * dt) / e.len;
    if (d.t >= d.stop) { dots.splice(i, 1); continue; }
    const f = d.t * (e.n - 1), k = Math.min(Math.floor(f), e.n - 2), w = f - k;
    const x = e.pts[k * 2] + (e.pts[k * 2 + 2] - e.pts[k * 2]) * w;
    const y = e.pts[k * 2 + 1] + (e.pts[k * 2 + 3] - e.pts[k * 2 + 1]) * w + MAP_TOP;
    dctx.globalAlpha = d.stop < 1 ? Math.min(1, (d.stop - d.t) / 0.12) : 1;
    dctx.fillStyle = d.color;
    dctx.beginPath(); dctx.arc(dOx + x * dScale, dOy + y * dScale, d.r * dScale, 0, 7); dctx.fill();
  }
  dctx.globalAlpha = 1;
  dotsDrawn = dots.length > 0;
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
  // Only seen while a call is up and its screen was sent home: it covers
  // this page otherwise.
  $("to-call").hidden = !ph.in_call;
  const bt = $("to-bt"), btOn = bluetoothShown();
  bt.hidden = btOn === null; // no data: no button to guess with
  bt.classList.toggle("off", btOn === false);
  bt.textContent = btOn === false ? "Bluetooth off" : "Bluetooth on";
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
  // And once a night regardless - a fresh start (poll() picks up deploys).
  if (now.getHours() === 4 && now.getMinutes() === 0 && performance.now() > 120e3) location.reload();
}

// ---- Data ----

async function poll() {
  try {
    const r = await fetch("/api/display", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    state = await r.json();
    lastOk = Date.now();
    // A deploy changed this page: load the new one. The dashboard sends
    // Cache-Control: no-cache, so the reload fetches the new files.
    if (pageVersion && state.page_version && state.page_version !== pageVersion) location.reload();
    pageVersion = pageVersion || state.page_version;
    renderMap();
    renderBars();
    Tank.sync(state);
  } catch (err) {
    // Keep drawing the last good state; say so once it's stale.
  }
  $("offline").style.display = Date.now() - lastOk > 3 * POLL_MS ? "block" : "none";
}

// ---- Interaction: tap to switch views; tap a box or fish for its page ----

document.addEventListener("contextmenu", (e) => e.preventDefault());
$("screen").addEventListener("click", (e) => {
  // A box or a fish opens its page: connections, numbers, pods, events and
  // recent logs (static/component.js). It comes back here by itself.
  if (view === 0) {
    const g = e.target.closest && e.target.closest("g[data-id]");
    if (g) return (location.href = `/component/${g.dataset.id}?from=display`);
  } else {
    const pod = Tank.podAt(e.clientX, e.clientY);
    if (pod) return (location.href = `/component/pod/${encodeURIComponent(pod.namespace)}/${encodeURIComponent(pod.name)}?from=display`);
  }
  view = 1 - view;
  $("arch-view").classList.toggle("hidden", view !== 0);
  $("tank-view").classList.toggle("hidden", view !== 1);
  $("title").textContent = view ? "Cluster aquarium" : "Homelab";
  $("pager").children[0].classList.toggle("on", view === 0);
  $("pager").children[1].classList.toggle("on", view === 1);
  if (view === 1) Tank.resize();
});

// ---- Asking first ----
// One dialog for the buttons that ask before acting: Go follows the link.
// Taps here don't switch views.
const confirmBox = $("confirm");
let confirmHref = null;
function closeConfirm() { confirmBox.hidden = true; clearTimeout(closeConfirm.timer); }
function ask(title, html, go, href) {
  $("confirm-title").textContent = title;
  $("confirm-text").innerHTML = html;
  $("confirm-go").textContent = go;
  confirmHref = href;
  confirmBox.hidden = false;
  clearTimeout(closeConfirm.timer);
  closeConfirm.timer = setTimeout(closeConfirm, 15000); // unanswered: back to the display
}
confirmBox.addEventListener("click", (e) => {
  e.stopPropagation();
  if (e.target === confirmBox || e.target.id === "confirm-cancel") closeConfirm();
});
$("confirm-go").addEventListener("click", () => {
  closeConfirm();
  location.href = confirmHref;
});

// ---- The Pi's desktop ----
// The Desktop button, once confirmed, follows a homelab-desktop:// link.
// Chromium hands it to apps/pi-display's handler, which closes this display
// (pi-display.service) so the desktop shows; its "Homelab display" icon
// brings it back. install.sh pre-approves the link for this page, so there
// is no "open this application?" prompt.
$("to-desktop").addEventListener("click", (e) => {
  e.stopPropagation();
  ask("Show the Pi desktop?",
    "The display closes. To bring it back, tap <b>Homelab display</b> on the desktop.",
    "Show desktop", "homelab-desktop://show");
});
// ---- Back to a call ----
// The Pi's call screen has a Home button that sends it away for the rest
// of the call; Call brings it back. Like Desktop, a page can't reach the
// phone service, so it follows a homelab-call:// link that apps/pi-display's
// handler turns into a loopback request to the service.
$("to-call").addEventListener("click", (e) => {
  e.stopPropagation();
  location.href = "homelab-call://show";
});
// ---- The Pi's Bluetooth ----
// Same trick: homelab-bluetooth://on or ://off, and apps/pi-display's
// handler powers the adapter with bluetoothctl. A tap toggles it, without
// asking (asked to drop the confirmation, 2026-10-03). The button shows the adapter as the phone service reports it, which is also
// what the phone page's switch shows, so each follows the other (the report
// takes up to ~10 s to get here). After a tap here it shows what was asked
// for straight away, until the report agrees or a minute passes.
let btWanted = null, btWantedUntil = 0;
function wantBluetooth(on) {
  btWanted = on;
  btWantedUntil = Date.now() + 60000;
  location.href = on ? "homelab-bluetooth://on" : "homelab-bluetooth://off";
  if (state) renderBars();
}
function bluetoothShown() {
  const reported = state.phone.bluetooth;
  if (btWanted === null || reported === btWanted || Date.now() > btWantedUntil) btWanted = null;
  return btWanted === null ? reported : btWanted;
}
$("to-bt").addEventListener("click", (e) => {
  e.stopPropagation();
  wantBluetooth(bluetoothShown() === false);
});

// ---- Loop ----

// 16 fps, not the screen's 60: at 60 the Pi's Chromium spent about a core
// and a half redrawing, and 24 still cost about half a core on the map
// alone. Dots and fish move per second, not per frame, so they keep their
// speed at any rate - only the steps get a little bigger. Frames come on
// the screen's refresh (every 16.7 ms), so skipping any too soon after the
// last would round the rate down; keep a schedule instead: draw at the
// first refresh past each 16th of a second.
// The aquarium at night: 12 fps. It's calm and dark then.
const FRAME_MS = 1000 / 16;
const NIGHT_FRAME_MS = 1000 / 12;
let prev = performance.now(), next = prev;
function frame(t) {
  requestAnimationFrame(frame);
  if (t < next - 1) return;
  // The Pi's call screen covers this page during a call (unless it was sent
  // home): draw nothing until it's gone. Chromium on the Pi's X11 can't tell
  // it's covered, so it would otherwise keep animating behind it.
  if (state && state.phone && state.phone.screen_shown) { next = t + FRAME_MS; prev = t; return; }
  const step = view === 1 && Tank.night() ? NIGHT_FRAME_MS : FRAME_MS;
  next = t - next > step ? t + step : next + step; // fell behind: don't try to catch up
  const dt = Math.min((t - prev) / 1000, 0.1);
  prev = t;
  if (view === 0) stepDots(dt);
  else Tank.draw(t, dt);
}

buildMap();
Tank.resize();
sizeDots();
addEventListener("resize", () => { Tank.resize(); sizeDots(); });
renderClock();
setInterval(renderClock, 1000);
poll();
setInterval(poll, POLL_MS);
setInterval(nextTick, 5000);
setTimeout(nextTick, 1500);
requestAnimationFrame(frame);
