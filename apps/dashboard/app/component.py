"""Debug pages behind the display: tap a box on the map (or a fish) to get
what's useful when it's red.

For a component that runs in the cluster, its pods: state, restarts and why
the last one ended (OOMKilled, exit code), live CPU and memory against the
limit, recent Kubernetes events, and the tail of each container's log - plus
the log from before the last restart, which is usually where a crash says
why. For one that runs elsewhere (the Pi's Docker and systemd, the Mac, the
desktop), the numbers Prometheus has for it and the commands that show its
logs: the dashboard pod can't read those itself, and giving it a way in
(the Docker socket, the host's journal) would hand a page with no login far
more than it needs.

Logs are only readable in the namespaces kubernetes/dashboard/dashboard.yaml
grants (pods/log, events) - not cam, whose logs carry other people's
addresses and invite links. Lines that look like credentials are masked
anyway (redact()); this page is open to anyone on the LAN.
"""
import asyncio
import re
import time
from datetime import datetime, timezone

import httpx

from app import display

LOG_LINES = 150
PREVIOUS_LOG_LINES = 60
# Per container. Keeps a page of several pods well inside the dashboard's
# 128Mi limit, whatever a pod logs.
LOG_BYTES = 48 * 1024

PI = "ssh joe@192.168.1.253"
MAC = "ssh -o HostKeyAlias=192.168.1.180 josephvelasquez@192.168.1.219"


def _m(label: str, query: str, unit: str = "", good=None) -> dict:
    """A number shown on a page: label, PromQL, unit, and optionally a
    check (value -> True good / False bad) that colours it."""
    return {"label": label, "query": query, "unit": unit, "good": good}


ok_if_1 = lambda v: v >= 1  # noqa: E731
ok_if_0 = lambda v: v == 0  # noqa: E731

# Every box on the map (static/display.js BOXES). pods: (namespace, name
# prefix) pairs whose pods, events and logs the page shows. argo: Argo CD
# Applications to show. metrics: numbers from Prometheus. commands: where
# to look next, for what this page can't show.
COMPONENTS: dict[str, dict] = {
    "lan": {
        "title": "Home network",
        "about": "Every device on the LAN. Its DNS goes to the Pi (CoreDNS, then AdGuard); *.home pages go to Traefik.",
        "metrics": [
            _m("DNS queries", "sum(rate(adguard_queries[5m])) * 60", "/min"),
            _m("Web requests (Traefik)", 'sum(rate(traefik_service_requests_total{service!~"dashboard-.*"}[5m]))', "req/s"),
        ],
        "commands": [
            "dig @192.168.1.253 dashboard.home      # DNS from any machine",
            "curl -sI https://dashboard.home        # HTTPS through Traefik",
        ],
    },
    "internet": {
        "title": "Internet",
        "about": "What AdGuard forwards to (1.1.1.1, 8.8.8.8), and what Argo CD pulls from GitHub.",
        "metrics": [
            _m("AdGuard upstream response", "max(adguard_top_upstreams_avg_response_time_seconds) * 1000", "ms"),
            _m("AdGuard answering", "min(adguard_up)", "", ok_if_1),
        ],
        "commands": [
            f"{PI} dig @1.1.1.1 example.com        # upstream DNS, past AdGuard",
            f"{PI} docker logs --since 1h adguardhome 2>&1 | grep -i timeout",
        ],
    },
    "coredns": {
        "title": "CoreDNS",
        "about": "The LAN's DNS server, in Docker on the Pi (docker/dns): answers *.home itself, hands everything else to AdGuard.",
        "metrics": [
            _m("Queries", "sum(rate(adguard_queries[5m])) * 60", "/min"),
        ],
        "commands": [
            f"{PI} docker logs --tail 100 coredns",
            "dig @192.168.1.253 dashboard.home",
            f"{PI} docker restart coredns",
        ],
    },
    "adguard": {
        "title": "AdGuard",
        "about": "Filtering and upstream DNS, in Docker on the Pi. Its numbers reach Prometheus through adguard-exporter (logs below).",
        "pods": [("monitoring", "adguard-exporter-")],
        "argo": ["monitoring"],
        "metrics": [
            _m("Answering", "min(adguard_up)", "", ok_if_1),
            _m("Protection on", "min(adguard_protection_enabled)", "", ok_if_1),
            _m("Queries today", "sum(adguard_queries)"),
            _m("Blocked today", "sum(adguard_queries_blocked)"),
            _m("Average processing", "avg(adguard_avg_processing_time_seconds) * 1000", "ms"),
            _m("Exporter scrape errors (1h)", "sum(increase(adguard_scrape_errors_total[1h]))", "", ok_if_0),
        ],
        "commands": [
            f"{PI} docker logs --tail 100 adguardhome",
            "http://192.168.1.253:3000               # AdGuard's own UI and query log",
        ],
    },
    "rustdesk": {
        "title": "RustDesk",
        "about": "Remote desktop ID and relay servers (hbbs, hbbr) in Docker on the Pi, plus the Pi's own RustDesk client.",
        "metrics": [
            _m("Screen out", "sum(rate(rustdesk_sent_bytes_total[1m])) / 1000", "kB/s"),
            _m("Input in", "sum(rate(rustdesk_received_bytes_total[1m])) / 1000", "kB/s"),
        ],
        "commands": [
            f"{PI} docker logs --tail 50 rustdesk-hbbs",
            f"{PI} docker logs --tail 50 rustdesk-hbbr",
            f"{PI} ps -C rustdesk -o pid,etime,args   # a --cm process = a session is open",
        ],
    },
    "phone": {
        "title": "Phone bridge",
        "about": "Calls and music from the iPhone over the Pi's Bluetooth to the PC (apps/phone). A systemd user service on the Pi.",
        "metrics": [
            _m("Last update", "time() - phone_bridge_last_update_timestamp_seconds", "s ago", lambda v: v < 120),
            _m("Call audio being bridged", "phone_bridge_running"),  # 1 only during a call
            _m("iPhone connected", "phone_connected", "", ok_if_1),
            _m("PC app present", "phone_pc_present", "", ok_if_1),
            _m("On a call", "phone_call_active"),
            _m("Music to the PC", "sum(rate(phone_media_sent_bytes_total[1m])) / 1000", "kB/s"),
            _m("Pi CPU", '1 - avg(rate(node_cpu_seconds_total{job="node-pi",mode="idle"}[2m]))', "", lambda v: v < 0.7),
        ],
        "commands": [
            f"{PI} journalctl --user -u phone-bridge -n 100 --no-pager",
            f"{PI} systemctl --user restart phone-bridge",
            "%APPDATA%\\phone-bridge\\agent.log      # on the PC: 'dropped N late chunks' = the Pi is too busy",
        ],
    },
    "phone_pc": {
        "title": "Phone app (PC)",
        "about": "The PC's agent: plays calls and music, sends the mic back, checks in with the bridge every second.",
        "metrics": [
            _m("Present", "phone_pc_present", "", ok_if_1),
            _m("Check-ins", "sum(rate(phone_pc_checkins_total[1m]))", "/s"),
        ],
        "commands": [
            "%APPDATA%\\phone-bridge\\agent.log      # on the PC",
        ],
    },
    "iphone": {
        "title": "iPhone",
        "about": "Paired with the Pi over Bluetooth (calls: HFP, music: A2DP).",
        "metrics": [
            _m("Connected", "phone_connected", "", ok_if_1),
            _m("On a call", "phone_call_active"),
        ],
        "commands": [
            f"{PI} bluetoothctl info                 # connected? which profiles?",
            f"{PI} journalctl --user -u phone-bridge -n 50 --no-pager | grep -i connect",
        ],
    },
    "backup": {
        "title": "Backup",
        "about": "Nightly at 03:00 (or within 15 min of the Mac waking): the Pi's snapshot, pulled by the Mac into restic (backup/).",
        "metrics": [
            _m("Last snapshot", "(time() - homelab_backup_last_snapshot_timestamp_seconds) / 3600", "h ago", lambda v: v < 26),
            _m("Last run exit code", "homelab_backup_last_exit_code", "", ok_if_0),
            _m("Snapshots", "homelab_backup_snapshot_count"),
            _m("Repository readable", "homelab_backup_repository_readable", "", ok_if_1),
            _m("Agent loaded", "homelab_backup_agent_loaded", "", ok_if_1),
            _m("Last report from the Mac", "(time() - homelab_backup_report_timestamp_seconds) / 60", "min ago", lambda v: v < 60),
        ],
        "commands": [
            f"{MAC} tail -50 ~/.config/homelab-backup/backup.log",
            f"{MAC} tail -20 ~/.config/homelab-backup/backup-error.log",
            f"{MAC} /opt/homebrew/bin/python3 ~/.config/homelab-backup/mac-backup.py   # back up now",
        ],
    },
    "mac": {
        "title": "Mac",
        "about": "The MacBook: holds the backups, runs the m1-node VM (the cluster's worker) and the camera (MediaMTX).",
        "nodes": ["m1-node"],
        "metrics": [
            _m("Backup repository readable", "homelab_backup_repository_readable", "", ok_if_1),
            _m("Last report", "(time() - homelab_backup_report_timestamp_seconds) / 60", "min ago", lambda v: v < 60),
        ],
        "commands": [
            f"{MAC} /usr/local/bin/multipass list",
            f"{MAC} launchctl list | grep homelab",
            f"{MAC} tail -20 ~/.config/homelab-cam/mediamtx.log",
        ],
    },
    "traefik": {
        "title": "Traefik",
        "about": "HTTPS ingress for every *.home page, with the homelab CA's certificate.",
        "pods": [("kube-system", "traefik-"), ("kube-system", "svclb-traefik-")],
        "metrics": [
            _m("Requests", "sum(rate(traefik_service_requests_total[5m]))", "req/s"),
            _m("5xx responses", 'sum(rate(traefik_service_requests_total{code=~"5.."}[5m])) or vector(0)', "req/s", lambda v: v < 0.01),
            _m("4xx responses", 'sum(rate(traefik_service_requests_total{code=~"4.."}[5m])) or vector(0)', "req/s"),
        ],
    },
    "api": {
        "title": "FastAPI",
        "about": "api.home and ai.home: the backend, its Redis job worker, and the routes that call Ollama.",
        "pods": [("backend", "api-"), ("backend", "worker-")],
        "argo": ["backend"],
        "metrics": [
            _m("Requests", f'sum(rate(http_requests_total{{handler!~"{display.PROBE_HANDLERS}"}}[5m])) or vector(0)', "req/s"),
            _m("5xx responses", 'sum(rate(http_requests_total{status="5xx"}[5m])) or vector(0)', "req/s", lambda v: v < 0.01),
            _m("p95 latency (1h)", f'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{handler!~"{display.PROBE_HANDLERS}"}}[1h]))) * 1000', "ms"),
        ],
    },
    "postgres": {
        "title": "Postgres",
        "about": "Postgres with pgvector, pinned to the Pi (5Gi local-path volume).",
        "pods": [("data", "postgres-")],
        "argo": ["data"],
    },
    "redis": {
        "title": "Redis",
        "about": "The api's job queue and cache, pinned to the Pi.",
        "pods": [("backend", "redis-")],
        "argo": ["backend"],
    },
    "prometheus": {
        "title": "Prometheus",
        "about": "Metrics for everything on this map, and Alertmanager behind it.",
        "pods": [("monitoring", "prometheus-"), ("monitoring", "alertmanager-")],
        "argo": ["monitoring"],
        "targets": True,
        "metrics": [
            _m("Targets up", "sum(up)"),
            _m("Targets", "count(up)"),
        ],
    },
    "apps": {
        "title": "Apps",
        "about": "Grafana, Argo CD, the chat assistant and offline Wikipedia.",
        "pods": [("monitoring", "grafana-"), ("argocd", "argocd-server-"), ("argocd", "argocd-application-controller-"),
                 ("argocd", "argocd-repo-server-"), ("chat", "chat-"), ("kiwix", "kiwix-")],
        "argo": ["monitoring", "chat", "kiwix", "root"],
        "all_argo": True,
    },
    "ollama": {
        "title": "Ollama",
        "about": "GPU inference on the Windows desktop (192.168.1.131:11434), reached from the cluster as the ai/inference Service.",
        "metrics": [
            _m("Reachable from the cluster", "min(homelab_inference_reachable)", "", ok_if_1),
            _m("api requests that use it", f'sum(rate(http_requests_total{{handler=~"{display.OLLAMA_HANDLERS}"}}[5m])) or vector(0)', "req/s"),
        ],
        "commands": [
            "curl http://192.168.1.131:11434/api/ps     # loaded models",
            "curl http://192.168.1.131:11434/api/version",
            "%LOCALAPPDATA%\\Ollama\\server.log           # on the desktop",
        ],
    },
}

# What flows along each line on the map (static/display.js EDGES), for the
# connection drawing on each page. unit: what the map's rate for it is in.
EDGE_INFO: dict[str, tuple[str, str]] = {
    "lan-coredns": ("DNS queries · UDP/TCP 53", "q/s"),
    "coredns-adguard": ("other queries · not *.home, to 127.0.0.1:5335", "q/s"),
    "adguard-internet": ("upstream DNS · 1.1.1.1, 8.8.8.8 (not blocked ones)", "q/s"),
    "lan-traefik": ("HTTPS to *.home · 443", "req/s"),
    "traefik-api": ("api.home, ai.home", "req/s"),
    "traefik-apps": ("the apps' pages · grafana, argocd, chat, wikipedia", "req/s"),
    "api-postgres": ("SQL + pgvector · 5432", "req/s"),
    "api-redis": ("job queue · 6379", "req/s"),
    "api-ollama": ("chat and embeddings · 192.168.1.131:11434", "req/s"),
    "lan-rustdesk": ("remote input · 21115-21119", "kB/s"),
    "rustdesk-lan": ("the Pi's screen to the viewer", "kB/s"),
    "iphone-phone": ("calls and music · Bluetooth HFP and A2DP", "kB/s"),
    "phone-phone_pc": ("call audio and music", "kB/s"),
    "phone_pc-phone": ("mic and check-ins · one check-in a second", "kB/s"),
    "phone-iphone": ("your voice to the caller", "kB/s"),
    "apps-api": ("chat conversations", "req/s"),
    "apps-internet": ("Argo CD pulls the repo · from GitHub", ""),
    "traefik-prometheus": ("metrics · scraped by Prometheus", ""),
    "api-prometheus": ("metrics · /metrics, scraped", ""),
    "adguard-prometheus": ("AdGuard's numbers · via adguard-exporter", ""),
    "phone-prometheus": ("phone, RustDesk, backups · the Pi's node_exporter", ""),
    "backup-mac": ("nightly snapshot · tar over SSH, 03:00", ""),
}


def connections(cid: str, links: dict, rates: dict, services: dict, status_of) -> list[dict]:
    """The lines on the map that touch this box, with what flows along them
    and how they look right now (display.edge_states: active, idle, down,
    unknown)."""
    out = []
    for edge, (label, unit) in EDGE_INFO.items():
        a, b = edge.split("-", 1)
        if cid not in (a, b):
            continue
        other = b if a == cid else a
        out.append({
            "edge": edge,
            "direction": "out" if a == cid else "in",
            "other": other,
            "other_title": COMPONENTS[other]["title"] if other in COMPONENTS else other,
            "other_status": status_of(services, other),
            "label": label,
            "state": links.get(edge, "unknown"),
            "rate": rates.get(edge),
            "unit": unit,
        })
    return out


def component_of_pod(ns: str, name: str) -> str | None:
    """The map box a pod belongs to, if any - for a fish's connections."""
    for cid, spec in COMPONENTS.items():
        if any(ns == n and name.startswith(p) for n, p in spec.get("pods", [])):
            return cid
    return None


# Masked before a log line leaves the dashboard. Defence in depth: the
# namespaces that can be read were chosen to keep secrets out, but a
# library can always log a connection string or a header.
REDACTIONS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie|x-api-key)(\"?\s*[:=]\s*)\S.*"), r"\1\2[redacted]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)(\"?\s*[:=]\s*\"?)[^\s\"'&,;]+"), r"\1\2[redacted]"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s/]+@"), r"\1[redacted]@"),  # scheme://user:pass@
    (re.compile(r"(/invite/)[A-Za-z0-9_-]{8,}"), r"\1[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[redacted jwt]"),
]


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _age(iso: str | None, now: float | None = None) -> float | None:
    """Seconds since a Kubernetes timestamp."""
    if not iso:
        return None
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    return max((now or time.time()) - t, 0.0)


def _state(state: dict) -> dict:
    """A container state ({running|waiting|terminated: {...}}) as one flat dict."""
    for kind in ("running", "waiting", "terminated"):
        if kind in (state or {}):
            s = state[kind]
            return {
                "kind": kind,
                "reason": s.get("reason"),
                "message": redact(s.get("message") or "")[:500] or None,
                "exit_code": s.get("exitCode"),
                "since": s.get("startedAt") or s.get("finishedAt"),
            }
    return {"kind": "unknown"}


def summarize_pod(item: dict, usage: dict | None = None) -> dict:
    """What the page shows for one pod, from its API object."""
    meta, spec, status = item["metadata"], item["spec"], item.get("status", {})
    statuses = {s["name"]: s for s in status.get("containerStatuses", [])}
    containers = []
    for c in spec.get("containers", []):
        s = statuses.get(c["name"], {})
        last = _state(s.get("lastState", {}))
        containers.append({
            "name": c["name"],
            "image": c.get("image"),
            "ready": bool(s.get("ready")),
            "restarts": s.get("restartCount", 0),
            "state": _state(s.get("state", {})),
            "last": last if last["kind"] == "terminated" else None,
            "mem_limit": c.get("resources", {}).get("limits", {}).get("memory"),
            "mem_request": c.get("resources", {}).get("requests", {}).get("memory"),
            "cpu_limit": c.get("resources", {}).get("limits", {}).get("cpu"),
        })
    conditions = [
        {"type": c["type"], "reason": c.get("reason"), "message": c.get("message")}
        for c in status.get("conditions", []) if c.get("status") != "True"
    ]
    return {
        "namespace": meta["namespace"],
        "name": meta["name"],
        "node": spec.get("nodeName"),
        "ip": status.get("podIP"),
        "phase": status.get("phase", "Unknown"),
        "started": status.get("startTime"),
        "ready": bool(containers) and all(c["ready"] for c in containers),
        "restarts": sum(c["restarts"] for c in containers),
        "containers": containers,
        "conditions": conditions,
        "cpu_m": (usage or {}).get("cpu_m"),
        "mem_bytes": (usage or {}).get("mem_bytes"),
        "mem_limit": display.memory_limit(spec),
    }


async def _logs(k8s: httpx.AsyncClient, ns: str, pod: str, container: str, previous: bool) -> dict:
    params = {"container": container, "tailLines": PREVIOUS_LOG_LINES if previous else LOG_LINES,
              "limitBytes": LOG_BYTES, "timestamps": "true"}
    if previous:
        params["previous"] = "true"
    try:
        r = await k8s.get(f"/api/v1/namespaces/{ns}/pods/{pod}/log", params=params)
    except httpx.HTTPError as e:
        return {"error": f"Couldn't reach the Kubernetes API: {type(e).__name__}"}
    if r.status_code == 403:
        return {"error": f"The dashboard isn't allowed to read logs in {ns} (kubernetes/dashboard/dashboard.yaml)."}
    if r.status_code != 200:
        try:
            message = r.json().get("message", "")
        except ValueError:
            message = ""
        return {"error": message or f"Kubernetes answered {r.status_code}"}
    lines = r.text.splitlines()
    return {"lines": [redact(line) for line in lines]}


async def _events(k8s: httpx.AsyncClient, ns: str, pods: list[str]) -> list[dict]:
    """Recent events for these pods, newest first. [] when not allowed."""
    try:
        r = await k8s.get(f"/api/v1/namespaces/{ns}/events")
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
    except (httpx.HTTPError, ValueError):
        return []
    wanted = set(pods)
    out = []
    for e in items:
        obj = e.get("involvedObject", {})
        if obj.get("kind") != "Pod" or obj.get("name") not in wanted:
            continue
        out.append({
            "pod": obj.get("name"),
            "type": e.get("type"),
            "reason": e.get("reason"),
            "message": redact(e.get("message") or ""),
            "count": e.get("count") or 1,
            "last": e.get("lastTimestamp") or e.get("eventTime") or e.get("metadata", {}).get("creationTimestamp"),
        })
    out.sort(key=lambda e: e["last"] or "", reverse=True)
    return out[:30]


async def pod_details(k8s: httpx.AsyncClient, items: list[dict], usage: dict) -> dict:
    """Pods, their events and logs (and logs from before a restart)."""
    pods = [summarize_pod(i, usage.get((i["metadata"]["namespace"], i["metadata"]["name"]))) for i in items]
    log_jobs, keys = [], []
    for p in pods:
        for c in p["containers"]:
            log_jobs.append(_logs(k8s, p["namespace"], p["name"], c["name"], False))
            keys.append((p["name"], c["name"], "current"))
            if c["last"]:
                log_jobs.append(_logs(k8s, p["namespace"], p["name"], c["name"], True))
                keys.append((p["name"], c["name"], "previous"))
    by_ns: dict[str, list[str]] = {}
    for p in pods:
        by_ns.setdefault(p["namespace"], []).append(p["name"])
    results = await asyncio.gather(*log_jobs, *(_events(k8s, ns, names) for ns, names in by_ns.items()))
    logs = [
        {"pod": pod, "container": container, "which": which, **result}
        for (pod, container, which), result in zip(keys, results[: len(keys)])
    ]
    events = sorted((e for batch in results[len(keys):] for e in batch), key=lambda e: e["last"] or "", reverse=True)
    return {"pods": pods, "logs": logs, "events": events}


async def _pods_matching(k8s: httpx.AsyncClient, selectors: list[tuple[str, str]]) -> list[dict]:
    items = []
    for ns in dict.fromkeys(ns for ns, _ in selectors):
        r = await k8s.get(f"/api/v1/namespaces/{ns}/pods")
        r.raise_for_status()
        prefixes = [p for n, p in selectors if n == ns]
        items += [
            i for i in r.json()["items"]
            if i.get("status", {}).get("phase") != "Succeeded" and any(i["metadata"]["name"].startswith(p) for p in prefixes)
        ]
    return items


async def _metrics(http: httpx.AsyncClient, metrics: list[dict]) -> list[dict]:
    values = await asyncio.gather(*(display._query(http, m["query"]) for m in metrics))
    out = []
    for m, v in zip(metrics, values):
        good = None if v is None or m["good"] is None else bool(m["good"](v))
        out.append({"label": m["label"], "value": v, "unit": m["unit"], "good": good, "query": m["query"]})
    return out


async def _down_targets(http: httpx.AsyncClient) -> list[dict] | None:
    try:
        r = await http.get(f"{display.PROMETHEUS_URL}/api/v1/targets", params={"state": "active"})
        r.raise_for_status()
        return [
            {"job": t["labels"].get("job"), "url": t.get("scrapeUrl"), "error": t.get("lastError")}
            for t in r.json()["data"]["activeTargets"] if t.get("health") != "up"
        ]
    except Exception:
        return None


async def component(k8s: httpx.AsyncClient, http: httpx.AsyncClient, cid: str,
                    services: dict, argo_task, alerts_task, nodes_task) -> dict:
    spec = COMPONENTS[cid]
    usage_task = display.pod_usage(k8s)
    pods_task = _pods_matching(k8s, spec.get("pods", [])) if spec.get("pods") else asyncio.sleep(0, [])
    targets_task = _down_targets(http) if spec.get("targets") else asyncio.sleep(0, None)
    items, usage, metrics, targets, argo, alerts, nodes = await asyncio.gather(
        pods_task, usage_task, _metrics(http, spec.get("metrics", [])), targets_task,
        argo_task, alerts_task, nodes_task, return_exceptions=True)
    out = {
        "id": cid,
        "title": spec["title"],
        "about": spec["about"],
        "status": services.get(cid, "unknown"),
        "metrics": [] if isinstance(metrics, BaseException) else metrics,
        "commands": spec.get("commands", []),
        "alerts": None if isinstance(alerts, BaseException) else alerts,
        "time": time.time(),
    }
    if spec.get("pods"):
        if isinstance(items, BaseException):
            out["pods_error"] = f"Couldn't list pods: {type(items).__name__}"
        else:
            out.update(await pod_details(k8s, items, {} if isinstance(usage, BaseException) else usage))
    if spec.get("argo") and not isinstance(argo, BaseException):
        wanted = None if spec.get("all_argo") else set(spec["argo"])
        out["argo"] = [a for a in argo if wanted is None or a["name"] in wanted]
    if spec.get("targets"):
        out["down_targets"] = None if isinstance(targets, BaseException) else targets
    if spec.get("nodes") and not isinstance(nodes, BaseException):
        out["nodes"] = [{"name": n["name"], "ready": n["ready"]} for n in nodes if n["name"] in spec["nodes"]]
    return out


async def single_pod(k8s: httpx.AsyncClient, ns: str, name: str, alerts_task) -> dict | None:
    """A fish's page: one pod. None if it's gone."""
    r = await k8s.get(f"/api/v1/namespaces/{ns}/pods/{name}")
    if r.status_code == 404:
        alerts_task.close()  # never needed: there's no page to put them on
        return None
    r.raise_for_status()
    usage, alerts = await asyncio.gather(display.pod_usage(k8s), alerts_task, return_exceptions=True)
    details = await pod_details(k8s, [r.json()], {} if isinstance(usage, BaseException) else usage)
    pod = details["pods"][0]
    return {
        "id": f"pod/{ns}/{name}",
        "title": name,
        "about": f"Pod in {ns}, on {pod['node'] or 'no node yet'}.",
        "status": "up" if pod["ready"] else "down",
        "metrics": [],
        "commands": [f"kubectl -n {ns} describe pod {name}", f"kubectl -n {ns} logs {name} --all-containers --tail 200"],
        "alerts": None if isinstance(alerts, BaseException) else alerts,
        "time": time.time(),
        **details,
    }
