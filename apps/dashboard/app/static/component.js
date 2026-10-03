// A debug page behind the display: tap a box on the map (/component/<id>)
// or a fish in the aquarium (/component/pod/<ns>/<name>). Shows how the
// component connects to the rest (drawn), its numbers, pods, events and
// recent logs - see app/component.py for where each comes from.
//
// Logs and events are other programs' output: they only ever go into the
// page as text (textContent), never as markup.
"use strict";

const REFRESH_MS = 15000;
// Opened from the Pi's display (?from=display): go back to it by itself
// after a while untouched, so the always-on screen never stays on a log.
const IDLE_RETURN_MS = 3 * 60 * 1000;
const FROM_DISPLAY = new URLSearchParams(location.search).get("from") === "display";
const SVG_NS = "http://www.w3.org/2000/svg";
const COLOR = { ok: "#97C459", warn: "#EF9F27", bad: "#E24B4A", unknown: "#5F5E5A", idle: "#6E6D67" };
const STATUS_COLOR = { up: COLOR.ok, down: COLOR.bad, unknown: COLOR.unknown };
const $ = (id) => document.getElementById(id);

const path = location.pathname.replace(/\/+$/, "");
const api = path.startsWith("/component/pod/") ? "/api/pod/" + path.slice("/component/pod/".length)
  : "/api/component/" + path.slice("/component/".length);

let data = null;
let loadedAt = 0;

// ---- building DOM without innerHTML ----

function h(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) e.append(c.nodeType ? c : String(c));
  return e;
}
function s(tag, attrs, parent) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

function ago(iso) {
  if (!iso) return "-";
  const secs = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (secs < 90) return `${Math.round(secs)} s ago`;
  if (secs < 5400) return `${Math.round(secs / 60)} min ago`;
  if (secs < 129600) return `${Math.round(secs / 3600)} h ago`;
  return `${Math.round(secs / 86400)} d ago`;
}
function bytes(n) {
  if (n === null || n === undefined) return "-";
  return n >= 2 ** 30 ? `${(n / 2 ** 30).toFixed(1)} GiB` : `${Math.round(n / 2 ** 20)} MiB`;
}
function num(v, unit) {
  if (v === null || v === undefined) return "no data";
  const abs = Math.abs(v);
  const text = abs >= 1000 ? Math.round(v).toLocaleString() : abs >= 10 ? v.toFixed(0) : abs >= 1 ? v.toFixed(1) : v === 0 ? "0" : v.toFixed(2);
  return unit ? `${text} ${unit}` : text;
}
function link(to) {
  return to + (FROM_DISPLAY ? "?from=display" : "");
}

// ---- the connection drawing ----
// This box in the middle; what sends to it on the left, what it sends to
// on the right. Each line is drawn the way it is right now: green and
// solid while traffic flows, grey dashes while both ends are up and quiet,
// red when an end is down, faint dots without data. Static - nothing on
// this page animates, so it costs the Pi nothing while it's open.

const LINE = {
  active: { color: COLOR.ok, dash: "", width: 3, text: "traffic flowing" },
  idle: { color: COLOR.idle, dash: "7 6", width: 2.5, text: "up, quiet" },
  down: { color: COLOR.bad, dash: "", width: 3, text: "an end is down" },
  unknown: { color: "#3A3A37", dash: "2 6", width: 2.5, text: "no data" },
};

function diagram(conns, centerTitle, centerStatus) {
  const ins = conns.filter((c) => c.direction === "in");
  const outs = conns.filter((c) => c.direction === "out");
  const rows = Math.max(ins.length, outs.length, 1);
  const W = 1000, ROW = 86, TOP = 26, H = TOP * 2 + rows * ROW;
  const BW = 168, BH = 54, CX = W / 2, CY = H / 2, CW = 190, CH = 66;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `How ${centerTitle} connects` });
  const defs = s("defs", {}, svg);
  for (const [state, st] of Object.entries(LINE)) {
    const m = s("marker", { id: `arrow-${state}`, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, defs);
    s("path", { d: "M0 0 L10 5 L0 10 z", fill: st.color }, m);
  }

  function side(list, left) {
    const x = left ? 20 + BW / 2 : W - 20 - BW / 2;
    list.forEach((c, i) => {
      const y = TOP + ROW * (i + 0.5) + (rows - list.length) * ROW / 2;
      const st = LINE[c.state] || LINE.unknown;
      // Neighbour edge to the centre box's side, arrow showing the flow.
      const nx = left ? x + BW / 2 : x - BW / 2;
      const cx = left ? CX - CW / 2 : CX + CW / 2;
      const cy = CY + (i - (list.length - 1) / 2) * Math.min(16, CH / Math.max(list.length, 1));
      const mid = (nx + cx) / 2;
      const d = `M${nx} ${y} C${mid} ${y} ${mid} ${cy} ${cx} ${cy}`;
      const attrs = { d, fill: "none", stroke: st.color, "stroke-width": st.width };
      if (st.dash) attrs["stroke-dasharray"] = st.dash;
      // Arrow at the receiving end: into the centre for "in", into the neighbour for "out".
      if (c.direction === "in") attrs["marker-end"] = `url(#arrow-${c.state in LINE ? c.state : "unknown"})`;
      else attrs["marker-start"] = `url(#arrow-${c.state in LINE ? c.state : "unknown"})`;
      s("path", attrs, svg);

      // What flows: its name over the line, then the port and the rate
      // under it, by the neighbour (where the line is still level).
      const [main, extra] = c.label.split(" · ");
      const tx = left ? nx + 12 : nx - 12, anchor = left ? "start" : "end";
      s("text", { x: tx, y: y - 9, "text-anchor": anchor, fill: "#D3D1C7", "font-size": 14.5 }, svg).textContent = main;
      const under = s("text", { x: tx, y: y + 21, "text-anchor": anchor, "font-size": 13 }, svg);
      if (extra) s("tspan", { fill: COLOR.unknown }, under).textContent = extra;
      if (c.rate !== null && c.rate !== undefined && c.unit) {
        if (extra) s("tspan", { fill: COLOR.unknown }, under).textContent = " · ";
        s("tspan", { fill: st.color, "font-weight": 600 }, under).textContent = c.rate > 0.001 ? num(c.rate, c.unit) : "quiet now";
      }

      // The neighbour, tappable: its own page.
      const a = s("a", { href: link(`/component/${c.other}`) }, svg);
      s("rect", { x: x - BW / 2, y: y - BH / 2, width: BW, height: BH, rx: 10, fill: "#141413",
        stroke: c.other_status === "down" ? COLOR.bad : "#3A3A37", "stroke-width": 2 }, a);
      s("circle", { cx: x + BW / 2 - 14, cy: y - BH / 2 + 14, r: 5, fill: STATUS_COLOR[c.other_status] || COLOR.unknown }, a);
      const nt = s("text", { x: x - BW / 2 + 14, y: y + 6, fill: "#F1EFE8", "font-size": 16, "font-weight": 600 }, a);
      nt.textContent = c.other_title;
    });
  }
  side(ins, true);
  side(outs, false);

  s("rect", { x: CX - CW / 2, y: CY - CH / 2, width: CW, height: CH, rx: 12, fill: "#1c1c1a",
    stroke: STATUS_COLOR[centerStatus] || COLOR.unknown, "stroke-width": 3 }, svg);
  const ct = s("text", { x: CX, y: CY + 7, "text-anchor": "middle", fill: "#F1EFE8", "font-size": 19, "font-weight": 700 }, svg);
  ct.textContent = centerTitle;
  if (!ins.length) s("text", { x: 20, y: CY + 5, fill: COLOR.unknown, "font-size": 13 }, svg).textContent = "nothing sends to it";
  if (!outs.length) s("text", { x: W - 20, y: CY + 5, "text-anchor": "end", fill: COLOR.unknown, "font-size": 13 }, svg).textContent = "it sends to nothing on the map";

  const legend = h("div", { class: "legend" }, Object.entries(LINE).map(([, st]) => {
    const sw = s("svg", { width: 34, height: 10 });
    const l = { x1: 0, y1: 5, x2: 34, y2: 5, stroke: st.color, "stroke-width": st.width };
    if (st.dash) l["stroke-dasharray"] = st.dash;
    s("line", l, sw);
    return h("span", {}, sw, st.text);
  }), h("span", {}, "Tap a box to open it"));
  return h("div", { class: "panel", id: "diagram" }, svg, legend);
}

// ---- sections ----

function metricsSection(metrics) {
  if (!metrics || !metrics.length) return null;
  return [h("h2", {}, "Numbers"), h("div", { class: "grid" }, metrics.map((m) =>
    h("div", { class: `metric ${m.good === true ? "good" : m.good === false ? "bad" : ""}`, title: m.query },
      h("div", { class: "label" }, m.label), h("div", { class: "value" }, num(m.value, m.unit)))))];
}

function alertsSection(alerts) {
  if (alerts === null) return [h("h2", {}, "Alerts"), h("p", { class: "empty" }, "Couldn't reach Alertmanager.")];
  if (!alerts || !alerts.length) return null;
  return [h("h2", {}, `Firing alerts (${alerts.length}, all of them)`), h("div", { class: "panel" },
    alerts.map((a) => h("div", { class: "badline" }, `${a.name}${a.severity ? ` (${a.severity})` : ""}: ${a.summary || ""}`)))];
}

function stateLine(st) {
  if (!st) return "";
  if (st.kind === "running") return h("span", {}, "running since ", ago(st.since));
  const bits = [st.kind, st.reason, st.exit_code !== null && st.exit_code !== undefined ? `exit ${st.exit_code}` : null].filter(Boolean).join(", ");
  return h("span", { class: "badline" }, bits, st.message ? ` - ${st.message}` : "");
}

function podsSection(d) {
  if (d.pods_error) return [h("h2", {}, "Pods"), h("p", { class: "badline" }, d.pods_error)];
  if (!d.pods) return null;
  if (!d.pods.length) return [h("h2", {}, "Pods"), h("p", { class: "empty" }, "No pods found - is it deployed?")];
  return [h("h2", {}, `Pods (${d.pods.length})`), d.pods.map((p) => {
    const memPct = p.mem_limit && p.mem_bytes ? Math.min(100, (100 * p.mem_bytes) / p.mem_limit) : null;
    return h("div", { class: `pod ${p.ready ? "" : "notready"}` },
      h("div", { class: "head" },
        h("span", { class: `pill ${p.ready ? "up" : "down"}` }, p.ready ? "ready" : p.phase === "Running" ? "not ready" : p.phase),
        h("a", { class: "name", href: link(`/component/pod/${p.namespace}/${p.name}`), style: "color: inherit; text-decoration: none" }, p.name),
        h("span", { class: "kv" }, "node ", h("b", {}, p.node || "-")),
        h("span", { class: "kv" }, "started ", h("b", {}, ago(p.started))),
        h("span", { class: p.restarts ? "kv warnline" : "kv" }, "restarts ", h("b", {}, p.restarts)),
        h("span", { class: "kv" }, "CPU ", h("b", {}, p.cpu_m === null ? "-" : `${Math.round(p.cpu_m)}m`)),
        h("span", { class: "kv" }, "memory ", h("b", {}, bytes(p.mem_bytes)), p.mem_limit ? ` of ${bytes(p.mem_limit)}` : " (no limit)",
          memPct !== null ? h("span", { class: "bar" }, h("i", { style: `width: ${memPct}%; background: ${memPct > 95 ? COLOR.bad : memPct > 85 ? COLOR.warn : COLOR.ok}` })) : null)),
      p.conditions.map((c) => h("div", { class: "warnline", style: "font-size: 14px; margin-top: 6px" }, `${c.type}: ${c.reason || "false"}${c.message ? ` - ${c.message}` : ""}`)),
      p.containers.map((c) => h("div", { class: "container" },
        h("span", { class: "cname" }, c.name), " ", stateLine(c.state),
        c.last ? h("div", { class: c.last.reason === "OOMKilled" ? "badline" : "warnline" },
          `Last ended ${ago(c.last.since)}: ${c.last.reason || "terminated"}${c.last.exit_code !== null ? `, exit ${c.last.exit_code}` : ""}`,
          c.last.reason === "OOMKilled" ? " - it ran out of memory (its limit is " + (c.mem_limit || "?") + ")" : "") : null,
        h("div", { class: "img" }, c.image))));
  })];
}

function eventsSection(events) {
  if (!events) return null;
  if (!events.length) return [h("h2", {}, "Kubernetes events"), h("p", { class: "empty" }, "None recently - Kubernetes keeps them for an hour.")];
  return [h("h2", {}, "Kubernetes events"), h("div", { class: "panel" }, h("table", {},
    h("tr", {}, h("th", {}, "When"), h("th", {}, "Pod"), h("th", {}, "What"), h("th", {}, "")),
    events.map((e) => h("tr", {}, h("td", {}, ago(e.last)), h("td", {}, e.pod), h("td", { class: e.type }, e.reason, e.count > 1 ? ` ×${e.count}` : ""), h("td", {}, e.message)))))];
}

// Colour obvious errors and warnings; the timestamp Kubernetes adds goes grey.
const ERR = /\b(error|err|fatal|panic|exception|traceback|failed|critical|refused|denied|oomkilled)\b/i;
const WRN = /\b(warn|warning|timeout|timed out|retry|retrying|unavailable)\b/i;
function logLines(lines) {
  const pre = h("pre", {});
  for (const line of lines) {
    const m = /^(\d{4}-\d\d-\d\dT[\d:.]+Z) (.*)$/.exec(line);
    const text = m ? m[2] : line;
    const row = h("span", { class: ERR.test(text) ? "err" : WRN.test(text) ? "wrn" : "" });
    if (m) row.append(h("span", { class: "ts" }, m[1].slice(5, 19).replace("T", " ") + " "));
    row.append(text, "\n");
    pre.append(row);
  }
  return pre;
}

function logsSection(logs, openState) {
  if (!logs) return null;
  if (!logs.length) return null;
  return [h("h2", {}, "Recent logs"), logs.map((l, i) => {
    const key = `${l.pod}/${l.container}/${l.which}`;
    const prev = l.which === "previous";
    const count = l.lines ? `${l.lines.length} lines` : "";
    const errs = l.lines ? l.lines.filter((x) => ERR.test(x)).length : 0;
    const open = key in openState ? openState[key] : i === 0 || prev;
    const det = h("details", { class: prev ? "previous" : "", "data-key": key, open: open ? "" : null },
      h("summary", {}, prev ? "Before the last restart: " : "", `${l.pod} › ${l.container}`,
        h("span", { class: "kv" }, count, errs ? h("span", { class: "badline" }, ` · ${errs} error-looking`) : "")),
      l.error ? h("pre", { class: "empty" }, l.error) : l.lines.length ? logLines(l.lines) : h("pre", { class: "empty" }, "(nothing logged)"));
    return det;
  })];
}

function argoSection(argo) {
  if (!argo || !argo.length) return null;
  return [h("h2", {}, "Argo CD"), h("div", { class: "panel" }, argo.map((a) => {
    const ok = a.sync_status === "Synced" && a.health_status === "Healthy";
    return h("div", { class: ok ? "" : "warnline" }, h("b", {}, a.name), `: ${a.sync_status}, ${a.health_status}`);
  }))];
}

function targetsSection(targets) {
  if (targets === undefined) return null;
  if (targets === null) return [h("h2", {}, "Scrape targets"), h("p", { class: "empty" }, "Couldn't ask Prometheus.")];
  if (!targets.length) return [h("h2", {}, "Scrape targets"), h("p", { class: "empty" }, "Every target is up.")];
  return [h("h2", {}, "Scrape targets that are down"), h("div", { class: "panel" },
    targets.map((t) => h("div", { class: "badline" }, `${t.job} ${t.url}: ${t.error || "down"}`)))];
}

function nodesSection(nodes) {
  if (!nodes || !nodes.length) return null;
  return [h("h2", {}, "Cluster node"), h("div", { class: "panel" }, nodes.map((n) =>
    h("div", { class: n.ready ? "" : "badline" }, h("b", {}, n.name), n.ready ? ": Ready" : ": not ready - the VM or the Mac may be off")))];
}

function commandsSection(commands) {
  if (!commands || !commands.length) return null;
  return [h("h2", {}, "Look further (from a terminal)"), h("div", {}, commands.map((c) => {
    const btn = h("button", { type: "button" }, "Copy");
    btn.addEventListener("click", async () => {
      const cmd = c.split("   #")[0].trim();
      try { await navigator.clipboard.writeText(cmd); btn.textContent = "Copied"; } catch { btn.textContent = "Select it"; }
      setTimeout(() => (btn.textContent = "Copy"), 1500);
    });
    return h("div", { class: "cmd" }, h("code", {}, c), btn);
  }))];
}

// What the box is, what's gone wrong with it before and what fixed it, and
// where it's written up (app/guide.py). A fish's page shows its box's.
function howSection(g) {
  if (!g || !g.how || !g.how.length) return null;
  return [h("h2", {}, "How it works"), h("div", { class: "panel" }, h("ul", { class: "how" }, g.how.map((t) => h("li", {}, t))))];
}

function fixesSection(g) {
  if (!g || !g.fixes || !g.fixes.length) return null;
  return [h("h2", {}, "Known problems and fixes"), h("div", { class: "panel" },
    g.fixes.map(([symptom, fix]) => h("div", { class: "fix" }, h("b", {}, symptom), h("span", {}, fix))))];
}

function docsSection(g) {
  if (!g || !g.docs || !g.docs.length) return null;
  return [h("h2", {}, "Read more (in the repo)"), h("div", { class: "docs" }, g.docs.map((d) => h("code", {}, d)))];
}

// ---- render, keeping what's open and where each log is scrolled ----

function render() {
  const d = data;
  document.title = `${d.title} - Homelab`;
  $("title").textContent = d.title;
  $("status").className = `pill ${d.status}`;
  $("status").textContent = { up: "healthy", down: "down", unknown: "no data" }[d.status] || d.status;
  $("about").textContent = d.about;

  const openState = {}, scroll = {};
  document.querySelectorAll("details[data-key]").forEach((det) => {
    openState[det.dataset.key] = det.open;
    const pre = det.querySelector("pre");
    if (pre) scroll[det.dataset.key] = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 20 ? "bottom" : pre.scrollTop;
  });

  const out = $("sections");
  out.replaceChildren(...[
    d.connections && d.connections.length ? [h("h2", {}, d.box ? `Connections (as part of ${d.box.title})` : "Connections"),
      diagram(d.connections, d.box ? d.box.title : d.title, d.box ? "unknown" : d.status)] : null,
    howSection(d.guide),
    alertsSection(d.alerts),
    metricsSection(d.metrics),
    fixesSection(d.guide),
    nodesSection(d.nodes),
    targetsSection(d.down_targets),
    podsSection(d),
    eventsSection(d.events),
    logsSection(d.logs, openState),
    argoSection(d.argo),
    commandsSection(d.commands),
    docsSection(d.guide),
  ].flat(2).filter(Boolean));

  // Logs open at their newest line, or where they were left.
  document.querySelectorAll("details[data-key]").forEach((det) => {
    const pre = det.querySelector("pre");
    if (!pre) return;
    const where = scroll[det.dataset.key];
    const toBottom = () => { pre.scrollTop = where === undefined || where === "bottom" ? pre.scrollHeight : where; };
    toBottom();
    det.addEventListener("toggle", toBottom, { once: true });
  });
}

async function load() {
  try {
    const r = await fetch(api, { cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `The dashboard answered ${r.status}`);
    data = body;
    loadedAt = Date.now();
    $("error").style.display = "none";
    render();
  } catch (err) {
    $("error").textContent = err.message;
    $("error").style.display = "inline-block";
  }
  showUpdated();
}

function showUpdated() {
  if (!loadedAt) return;
  const secs = Math.round((Date.now() - loadedAt) / 1000);
  $("updated").textContent = secs < 3 ? "updated just now" : `updated ${secs} s ago`;
}

// ---- controls ----

$("back").addEventListener("click", () => {
  if (FROM_DISPLAY || history.length < 2) location.href = "/display";
  else history.back();
});
$("refresh").addEventListener("click", load);

let lastTouch = Date.now();
for (const ev of ["pointerdown", "scroll", "keydown", "wheel"]) addEventListener(ev, () => (lastTouch = Date.now()), { passive: true });
setInterval(() => {
  if (FROM_DISPLAY && Date.now() - lastTouch > IDLE_RETURN_MS) location.href = "/display";
}, 10000);

load();
setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
setInterval(showUpdated, 1000);
