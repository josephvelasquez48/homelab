"""State for the Pi's always-on screen (/display): the architecture map and
the pod aquarium.

One call gathers everything the page draws, so the page only ever makes one
request every few seconds:

- **services** - one status per box on the map: "up", "down" or "unknown".
  In-cluster ones come from pod readiness, host ones from a probe or a
  Prometheus gauge. No data is "unknown", never "up".
- **rates** - requests per second along each line on the map, from
  Prometheus. Zero means nothing is flowing, and the page draws no dots.
- **pods** - every pod, for the aquarium: one fish each, with its live CPU
  and memory (metrics-server) and memory limit - a fish's speed, size, and
  whether it puffs up near its limit.
- **events** - short lines for the ticker, built from the same data.

Host services (CoreDNS, RustDesk) are probed at the Pi's own address. That
works because this pod is pinned to the Pi: same-node pod traffic to the
node's IP isn't filtered by ufw (see kubernetes/monitoring/adguard-exporter.yaml).
"""
import asyncio
import math
import socket
import struct
import time

import httpx

from app.config import HOST_IP, PROMETHEUS_URL, WEATHER_LAT, WEATHER_LON

# The api's routes that call Ollama (apps/api/app/routers).
OLLAMA_HANDLERS = "/v1/chat|/v1/embed|/v1/rag/query|/v1/documents|/v1/conversations/.+/messages"
PROBE_HANDLERS = "/metrics|/ready|/health"

RATE_QUERIES = {
    "dns": "sum(clamp_min(rate(adguard_queries[5m]), 0))",
    "dns_blocked": "sum(clamp_min(rate(adguard_queries_blocked[5m]), 0))",
    # The display's own polling goes through Traefik too; leave it out, or
    # the screen would mostly show itself.
    "web": 'sum(rate(traefik_service_requests_total{service!~"dashboard-.*"}[5m])) or vector(0)',
    "web_api": 'sum(rate(traefik_service_requests_total{service=~"backend-api-.*"}[5m])) or vector(0)',
    "web_apps": 'sum(rate(traefik_service_requests_total{service=~"(monitoring|argocd|chat|kiwix)-.*"}[5m])) or vector(0)',
    # Counters only appear once a route has been hit; no series is no traffic.
    "api": f'sum(rate(http_requests_total{{handler!~"{PROBE_HANDLERS}"}}[5m])) or vector(0)',
    "ollama": f'sum(rate(http_requests_total{{handler=~"{OLLAMA_HANDLERS}"}}[5m])) or vector(0)',
    # The phone bridge's traffic (apps/phone, counters in its textfile). A
    # 1m window, not 5m: a call or a song should show up within a minute
    # and stop soon after it ends. Audio is in kB/s; check-ins per second.
    "phone_call_rx_kbps": "sum(rate(phone_call_rx_bytes_total[1m])) / 1000 or vector(0)",
    "phone_call_tx_kbps": "sum(rate(phone_call_tx_bytes_total[1m])) / 1000 or vector(0)",
    "phone_media_kbps": "sum(rate(phone_media_sent_bytes_total[1m])) / 1000 or vector(0)",
    "phone_checkins": "sum(rate(phone_pc_checkins_total[1m])) or vector(0)",
    # RustDesk on the Pi (apps/rustdesk-traffic): out is mostly the Pi's
    # screen to a viewer, in is input. kB/s, like the phone's audio.
    "rustdesk_out_kbps": "sum(rate(rustdesk_sent_bytes_total[1m])) / 1000 or vector(0)",
    "rustdesk_in_kbps": "sum(rate(rustdesk_received_bytes_total[1m])) / 1000 or vector(0)",
    # The chat app's conversations through the api (Apps -> FastAPI).
    "chat_api": 'sum(rate(http_requests_total{handler=~"/v1/conversations.*"}[5m])) or vector(0)',
}

VALUE_QUERIES = {
    "dns_today": "sum(adguard_queries)",
    "blocked_today": "sum(adguard_queries_blocked)",
    "adguard_up": "min(adguard_up)",
    "adguard_protection": "min(adguard_protection_enabled)",
    "pi_temp_c": 'node_hwmon_temp_celsius{job="node-pi",chip="thermal_thermal_zone0",sensor="temp0"}',
    "pi_cpu": '1 - avg(rate(node_cpu_seconds_total{job="node-pi",mode="idle"}[2m]))',
    "pi_mem": '1 - node_memory_MemAvailable_bytes{job="node-pi"} / node_memory_MemTotal_bytes{job="node-pi"}',
    "api_p95_s": f'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{{handler!~"{PROBE_HANDLERS}"}}[1h])))',
    # The phone service writes its gauges every few seconds; stale means it's down.
    "phone_age_s": "time() - phone_bridge_last_update_timestamp_seconds",
    "phone_connected": "phone_connected",
    "phone_call": "phone_call_active",
    "phone_pc": "phone_pc_present",
    "inference": "min(homelab_inference_reachable)",
    "backup_age_h": "(time() - homelab_backup_last_snapshot_timestamp_seconds) / 3600",
    "backup_exit": "homelab_backup_last_exit_code",
    "targets_up": "sum(up)",
    "targets": "count(up)",
    # Prometheus's scrapes, one per line into the Prometheus box. node-pi is
    # the Pi's node_exporter, which also carries the phone bridge's,
    # RustDesk's and the backups' textfiles - the line from the phone bridge.
    "scrape_traefik": 'min(up{job="kubernetes-pods",pod=~"traefik-.*"})',
    "scrape_api": 'min(up{job="kubernetes-pods",pod=~"api-.*"})',
    "scrape_adguard": 'min(up{job="kubernetes-pods",pod=~"adguard-exporter-.*"})',
    "scrape_pi": 'min(up{job="node-pi"})',
    "backup_readable": "min(homelab_backup_repository_readable)",
    # 1 while the Pi's call screen covers the display: the page pauses.
    "phone_screen": "phone_screen_shown",
    # The Pi's Bluetooth adapter, for the display's Bluetooth button.
    "phone_bluetooth": "phone_bluetooth_powered",
}

# Map boxes that are pods: box -> (namespace, pod name prefix).
POD_SERVICES = {
    "traefik": ("kube-system", "traefik-"),
    "api": ("backend", "api-"),
    "postgres": ("data", "postgres-"),
    "redis": ("backend", "redis-"),
    "prometheus": ("monitoring", "prometheus-"),
    "argocd": ("argocd", "argocd-server-"),
    "grafana": ("monitoring", "grafana-"),
    "chat": ("chat", "chat-"),
    "kiwix": ("kiwix", "kiwix-"),
}

WEATHER_TTL_S = 600
# "at" is time.monotonic(), which counts from boot: starting it at 0 made a
# freshly booted machine (the Pi after a reboot, a CI runner) treat the empty
# cache as fresh for its first 10 minutes. None means never fetched.
_weather: dict = {"at": None, "value": None}


async def _query(client: httpx.AsyncClient, expr: str) -> float | None:
    try:
        r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": expr})
        r.raise_for_status()
        result = r.json()["data"]["result"]
        value = float(result[0]["value"][1]) if result else None
        # NaN is Prometheus's answer to, say, a latency quantile with no
        # requests in the window. It's "no data", and it can't be sent as
        # JSON - the api's debug page answered 500 on it (2026-10-03).
        return value if value is None or math.isfinite(value) else None
    except Exception:
        return None


async def _queries(client: httpx.AsyncClient, queries: dict[str, str]) -> dict[str, float | None]:
    values = await asyncio.gather(*(_query(client, q) for q in queries.values()))
    return dict(zip(queries, values))


async def _top_blocked(client: httpx.AsyncClient) -> str | None:
    try:
        r = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query", params={"query": "topk(1, adguard_top_blocked_domains)"}
        )
        r.raise_for_status()
        result = r.json()["data"]["result"]
        return result[0]["metric"].get("domain") if result else None
    except Exception:
        return None


UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
CPU_UNITS = {"n": 1e-6, "u": 1e-3, "m": 1.0}


def memory_bytes(q: str) -> float:
    """A Kubernetes memory quantity ("177664Ki", "256Mi", "1G", "1024") in bytes."""
    for suffix in sorted(UNITS, key=len, reverse=True):
        if q.endswith(suffix):
            return float(q[: -len(suffix)]) * UNITS[suffix]
    return float(q)


def cpu_millicores(q: str) -> float:
    """A CPU quantity ("3284811n", "250m", "2") in millicores."""
    if q and q[-1] in CPU_UNITS:
        return float(q[:-1]) * CPU_UNITS[q[-1]]
    return float(q) * 1000


def memory_limit(spec: dict) -> float | None:
    """The pod's memory limit: the sum over its containers, or None if any has none."""
    limits = [c.get("resources", {}).get("limits", {}).get("memory") for c in spec.get("containers", [])]
    if not limits or None in limits:
        return None
    return sum(memory_bytes(q) for q in limits)


async def pod_usage(k8s: httpx.AsyncClient) -> dict[tuple[str, str], dict]:
    """(namespace, name) -> live cpu_m / mem_bytes from metrics-server; {} if it's unavailable."""
    try:
        r = await k8s.get("/apis/metrics.k8s.io/v1beta1/pods")
        r.raise_for_status()
    except Exception:
        return {}
    usage = {}
    for item in r.json().get("items", []):
        containers = item.get("containers", [])
        usage[(item["metadata"]["namespace"], item["metadata"]["name"])] = {
            "cpu_m": sum(cpu_millicores(c["usage"]["cpu"]) for c in containers),
            "mem_bytes": sum(memory_bytes(c["usage"]["memory"]) for c in containers),
        }
    return usage


async def list_pods(k8s: httpx.AsyncClient) -> list[dict]:
    """Every pod in the cluster, with why it isn't running when it isn't."""
    r = await k8s.get("/api/v1/pods")
    r.raise_for_status()
    pods = []
    for item in r.json()["items"]:
        phase = item["status"].get("phase", "Unknown")
        if phase == "Succeeded":  # finished Jobs, not residents of the tank
            continue
        statuses = item["status"].get("containerStatuses", [])
        reason = next(
            (s["state"]["waiting"].get("reason") for s in statuses if "waiting" in s.get("state", {})),
            None,
        )
        pods.append(
            {
                "name": item["metadata"]["name"],
                "namespace": item["metadata"]["namespace"],
                "node": item["spec"].get("nodeName"),
                "phase": phase,
                "ready": bool(statuses) and all(s.get("ready") for s in statuses),
                "restarts": sum(s.get("restartCount", 0) for s in statuses),
                "reason": reason,
                "mem_limit": memory_limit(item["spec"]),
            }
        )
    return pods


def pod_status(pods: list[dict], namespace: str, prefix: str) -> str:
    """up if any matching pod is ready, down if some exist and none are."""
    matching = [p for p in pods if p["namespace"] == namespace and p["name"].startswith(prefix)]
    if not matching:
        return "unknown"
    return "up" if any(p["ready"] for p in matching) else "down"


def dns_query(name: str, qid: int = 0x5A5A) -> bytes:
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)  # recursion desired, one question
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split("."))
    return header + labels + b"\x00" + struct.pack(">HH", 1, 1)  # A, IN


def dns_answered(response: bytes, qid: int = 0x5A5A) -> bool:
    """Same ID, a response, rcode NOERROR and at least one answer."""
    if len(response) < 12:
        return False
    rid, flags, _, answers = struct.unpack(">HHHH", response[:8])
    return rid == qid and flags & 0x8000 and flags & 0x000F == 0 and answers > 0


def _dns_probe_blocking(host: str, name: str, timeout: float) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(dns_query(name), (host, 53))
        return "up" if dns_answered(sock.recv(512)) else "down"
    except OSError:
        return "down"
    finally:
        sock.close()


async def probe_dns(host: str, name: str = "dashboard.home", timeout: float = 2.0) -> str:
    """Does the Pi's resolver answer? A plain socket with its own timeout, in a thread.

    This used to be loop.create_datagram_endpoint() with the reply awaited
    under wait_for. Under uvicorn's uvloop that endpoint stopped coming
    back after the first poll (2026-10-02, after a network blip on the Pi):
    the await sat outside the timeout, gather() waited on it forever, and
    every /api/display after the first hung - the display froze with its
    last state, dead fish and blank stats. A blocking socket's timeout
    can't be skipped like that.
    """
    try:
        return await asyncio.wait_for(asyncio.to_thread(_dns_probe_blocking, host, name, timeout), timeout + 1)
    except Exception:
        return "down"


async def probe_tcp(host: str, port: int, timeout: float = 2.0) -> str:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        return "up"
    except Exception:
        return "down"


async def weather(client: httpx.AsyncClient) -> dict | None:
    """Current conditions from Open-Meteo (no key), cached; None when no location is set."""
    if not (WEATHER_LAT and WEATHER_LON):
        return None
    if _weather["at"] is not None and time.monotonic() - _weather["at"] < WEATHER_TTL_S:
        return _weather["value"]
    try:
        r = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": WEATHER_LAT,
                "longitude": WEATHER_LON,
                # The aquarium draws the weather above its water: time of
                # day from sunrise/sunset, clouds, rain and wind.
                "current": "temperature_2m,weather_code,is_day,cloud_cover,precipitation,wind_speed_10m",
                "daily": "sunrise,sunset",
                "forecast_days": 1,
                "timezone": "auto",
                "timeformat": "unixtime",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
            },
        )
        r.raise_for_status()
        body = r.json()
        cur, daily = body["current"], body.get("daily", {})
        _weather["value"] = {
            "temp_f": round(cur["temperature_2m"]),
            "code": cur["weather_code"],
            "is_day": bool(cur.get("is_day", 1)),
            "cloud_cover": cur.get("cloud_cover"),
            "precip_mm": cur.get("precipitation"),
            "wind_mph": cur.get("wind_speed_10m"),
            "sunrise": (daily.get("sunrise") or [None])[0],
            "sunset": (daily.get("sunset") or [None])[0],
        }
    except Exception:
        pass  # keep the last good value
    _weather["at"] = time.monotonic()
    return _weather["value"]


def flag(value: float | None) -> bool | None:
    return None if value is None else value >= 1


def services(pods: list[dict], v: dict, dns: str, rustdesk: str) -> dict[str, str]:
    out = {box: pod_status(pods, ns, prefix) for box, (ns, prefix) in POD_SERVICES.items()}
    out["coredns"] = dns
    if v["adguard_up"] is None:
        out["adguard"] = "unknown"
    else:  # filtering switched off counts as down: DNS works, but unprotected
        out["adguard"] = "up" if flag(v["adguard_up"]) and flag(v["adguard_protection"]) is not False else "down"
    out["phone"] = "unknown" if v["phone_age_s"] is None else ("up" if v["phone_age_s"] < 120 else "down")
    out["phone_pc"] = {True: "up", False: "down", None: "unknown"}[flag(v["phone_pc"])]
    out["iphone"] = {True: "up", False: "down", None: "unknown"}[flag(v["phone_connected"])]
    out["ollama"] = {True: "up", False: "down", None: "unknown"}[flag(v["inference"])]
    out["rustdesk"] = rustdesk
    out["internet"] = "up" if v["adguard_up"] and (v["dns_today"] or 0) > (v["blocked_today"] or 0) else "unknown"
    # The nightly backup on the Pi (backup/): a snapshot in the last 26 h
    # and a clean last run. The Mac holds the repository it writes to.
    age, code = v["backup_age_h"], v["backup_exit"]
    out["backup"] = "unknown" if age is None or code is None else ("up" if age < 26 and code == 0 else "down")
    out["mac"] = {True: "up", False: "down", None: "unknown"}[flag(v["backup_readable"])]
    return out


# Lines whose ends' health isn't simply their boxes': the Apps box is four
# apps, so a line from it uses the one app it's about; a scrape line is
# healthy when Prometheus can scrape that target.
EDGE_ENDS = {
    "apps-api": ("chat", "api"),
    "apps-internet": ("argocd", "internet"),
}
EDGE_SCRAPE = {
    "traefik-prometheus": "scrape_traefik",
    "api-prometheus": "scrape_api",
    "adguard-prometheus": "scrape_adguard",
    "phone-prometheus": "scrape_pi",
}


def edge_states(rates: dict[str, float], svc: dict[str, str], v: dict) -> dict[str, str]:
    """One of active / idle / down / unknown per line - the page draws a
    fixed pattern for each, not a dot per request:

    - down: either end down (or a scrape failing): red dots that stop short
    - unknown: no data for an end: no dots
    - active: traffic measured on it now: a steady stream
    - idle: both ends up, nothing measured (or nothing to measure - the
      scrapes, Argo CD's checks of GitHub, the backups): a slow trickle
    """
    def status(box: str) -> str:
        if box == "lan":
            return "up"
        if box == "apps":  # four apps in one box: as the page colours it
            apps = [svc.get(a, "unknown") for a in ("grafana", "argocd", "chat", "kiwix")]
            return "down" if "down" in apps else "up" if all(a == "up" for a in apps) else "unknown"
        return svc.get(box, "unknown")

    out = {}
    for edge in [*rates, "apps-internet", "backup-mac", *EDGE_SCRAPE]:
        a, b = EDGE_ENDS.get(edge) or edge.split("-", 1)
        ends = [status(a), status(b)]
        if edge in EDGE_SCRAPE:
            ends.append({True: "up", False: "down", None: "unknown"}[flag(v[EDGE_SCRAPE[edge]])])
        if "down" in ends:
            out[edge] = "down"
        elif "unknown" in ends:
            out[edge] = "unknown"
        else:
            out[edge] = "active" if (rates.get(edge) or 0) > 0.001 else "idle"
    return out


def edge_rates(r: dict, v: dict) -> dict[str, float]:
    z = lambda x: max(x or 0.0, 0.0)  # noqa: E731
    # The phone lines carry real traffic both ways: the caller's voice and
    # music (only while audible) from the iPhone through the bridge to the
    # PC, and the PC's mic plus its agent's once-a-second check-ins back.
    # Audio rates are kB/s (a call ~32, music ~190), so streaming reads busy
    # and check-ins a trickle; the page's log scale caps both.
    down = z(r["phone_call_rx_kbps"]) + z(r["phone_media_kbps"])
    mic = z(r["phone_call_tx_kbps"])
    return {
        "lan-coredns": z(r["dns"]),
        "coredns-adguard": z(r["dns"]),
        "adguard-internet": z(r["dns"]),  # blocked share drawn as red dots that stop short
        "lan-traefik": z(r["web"]),
        "traefik-api": z(r["web_api"]),
        "traefik-apps": z(r["web_apps"]),
        "api-postgres": z(r["api"]),
        "api-redis": z(r["api"]),
        "api-ollama": z(r["ollama"]),
        "iphone-phone": down,
        "phone-phone_pc": down,
        "phone_pc-phone": mic + z(r["phone_checkins"]),
        "phone-iphone": mic,
        "lan-rustdesk": z(r["rustdesk_in_kbps"]),
        "rustdesk-lan": z(r["rustdesk_out_kbps"]),
        "apps-api": z(r["chat_api"]),
    }


def events(v: dict, argo: list[dict], alerts: list[dict] | None, nodes: list[dict], top_blocked: str | None) -> list[dict]:
    """Ticker lines, most important first. level: ok, info, warn, bad."""
    out: list[dict] = []
    for a in alerts or []:
        out.append({"level": "bad", "text": f"Alert: {a.get('summary') or a.get('name')}"})
    for n in nodes:
        if not n["ready"]:
            out.append({"level": "warn", "text": f"Node {n['name']} is not ready"})
    unsynced = [a["name"] for a in argo if a["sync_status"] != "Synced" or a["health_status"] != "Healthy"]
    if argo:
        out.append(
            {"level": "warn", "text": f"Argo CD: {', '.join(unsynced)} not synced and healthy"}
            if unsynced
            else {"level": "ok", "text": f"Argo CD: all {len(argo)} apps synced and healthy"}
        )
    age = v["backup_age_h"]
    if age is not None:
        bad = age > 30 or (v["backup_exit"] or 0) != 0
        out.append({"level": "bad" if bad else "ok", "text": f"Last backup {age:.0f} h ago" + (" - check it" if bad else "")})
    if v["blocked_today"] is not None and v["dns_today"]:
        pct = 100 * v["blocked_today"] / v["dns_today"]
        out.append({"level": "info", "text": f"AdGuard blocked {v['blocked_today']:,.0f} of {v['dns_today']:,.0f} queries today ({pct:.0f}%)"})
    if top_blocked:
        out.append({"level": "info", "text": f"Most blocked: {top_blocked}"})
    if v["phone_connected"] is not None:
        if flag(v["phone_call"]):
            out.append({"level": "info", "text": "On a call"})
        else:
            out.append({"level": "ok" if flag(v["phone_connected"]) else "warn",
                        "text": "iPhone connected" if flag(v["phone_connected"]) else "iPhone not connected"})
    if v["api_p95_s"] is not None:
        out.append({"level": "info", "text": f"API p95 {v['api_p95_s'] * 1000:.0f} ms over the last hour"})
    if v["targets"]:
        out.append({"level": "ok" if v["targets_up"] == v["targets"] else "warn",
                    "text": f"Prometheus: {v['targets_up']:.0f} of {v['targets']:.0f} targets up"})
    return out


# Longest any one source may take. Each already has its own timeout; this is
# the backstop, so a source that hangs anyway costs the display that one
# reading instead of freezing it (see probe_dns).
SOURCE_DEADLINE_S = 8.0


async def _bounded(aw):
    return await asyncio.wait_for(aw, SOURCE_DEADLINE_S)


async def gather(k8s_client: httpx.AsyncClient, http: httpx.AsyncClient, argo_task, alerts_task, nodes_task) -> dict:
    (pods, usage, rates, values, top_blocked, dns, rustdesk, wx, argo, alerts, nodes) = await asyncio.gather(
        *(_bounded(aw) for aw in (
            list_pods(k8s_client),
            pod_usage(k8s_client),
            _queries(http, RATE_QUERIES),
            _queries(http, VALUE_QUERIES),
            _top_blocked(http),
            probe_dns(HOST_IP),
            probe_tcp(HOST_IP, 21116),
            weather(http),
            argo_task,
            alerts_task,
            nodes_task,
        )),
        return_exceptions=True,
    )
    pods = [] if isinstance(pods, BaseException) else pods
    usage = {} if isinstance(usage, BaseException) else usage
    # Unknown, not zero, if Prometheus didn't answer in time.
    rates = {k: None for k in RATE_QUERIES} if isinstance(rates, BaseException) else rates
    values = {k: None for k in VALUE_QUERIES} if isinstance(values, BaseException) else values
    for p in pods:  # no metrics yet (just started, or metrics-server down): None
        u = usage.get((p["namespace"], p["name"]), {})
        p["cpu_m"], p["mem_bytes"] = u.get("cpu_m"), u.get("mem_bytes")
    argo = [] if isinstance(argo, BaseException) else argo
    nodes = [] if isinstance(nodes, BaseException) else nodes
    alerts = None if isinstance(alerts, BaseException) else alerts
    wx = None if isinstance(wx, BaseException) else wx
    dns = "unknown" if isinstance(dns, BaseException) else dns
    rustdesk = "unknown" if isinstance(rustdesk, BaseException) else rustdesk
    top_blocked = None if isinstance(top_blocked, BaseException) else top_blocked

    svc = services(pods, values, dns, rustdesk)
    running = [p for p in pods if p["ready"]]
    blocked_share = (
        rates["dns_blocked"] / rates["dns"] if rates["dns"] and rates["dns_blocked"] is not None else 0.0
    )
    edge = edge_rates(rates, values)
    return {
        "services": svc,
        "rates": edge,
        "links": edge_states(edge, svc, values),
        "blocked_share": min(max(blocked_share, 0.0), 1.0),
        "stats": {
            "dns_per_min": None if rates["dns"] is None else rates["dns"] * 60,
            "blocked_pct": None if not values["dns_today"] else 100 * (values["blocked_today"] or 0) / values["dns_today"],
            "pods_ready": len(running),
            "pods_total": len(pods),
            "api_rps": rates["api"],
            "pi_temp_c": values["pi_temp_c"],
            "pi_cpu": values["pi_cpu"],
            "pi_mem": values["pi_mem"],
        },
        "phone": {
            "connected": flag(values["phone_connected"]),
            "in_call": flag(values["phone_call"]),
            "pc_present": flag(values["phone_pc"]),
            # The Pi's call screen is covering the display: nothing to animate.
            "screen_shown": bool(flag(values["phone_screen"])),
            # The Pi's Bluetooth is on; None when the phone service hasn't said.
            "bluetooth": flag(values["phone_bluetooth"]),
        },
        "nodes": [{"name": n["name"], "ready": n["ready"]} for n in nodes],
        "pods": pods,
        "alerts": alerts,
        "events": events(values, argo, alerts, nodes, top_blocked),
        "weather": wx,
        "time": time.time(),
    }
