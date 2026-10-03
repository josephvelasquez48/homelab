import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from app import component, display, main

STATIC = Path(__file__).parent.parent / "app" / "static"


def _pod(name, ns="backend", restarts=0, last=None, ready=True):
    status = {"name": "app", "ready": ready, "restartCount": restarts,
              "state": {"running": {"startedAt": "2026-10-03T05:00:00Z"}}}
    if last:
        status["lastState"] = {"terminated": last}
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {"nodeName": "joe", "containers": [{"name": "app", "image": "ghcr.io/x/api:1",
                                                     "resources": {"limits": {"memory": "256Mi"}}}]},
        "status": {"phase": "Running", "podIP": "10.42.0.9", "startTime": "2026-10-03T05:00:00Z",
                   "containerStatuses": [status], "conditions": [{"type": "Ready", "status": "True"}]},
    }


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code, self._body, self.text = status_code, body, text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class FakeK8s:
    """Answers the handful of GETs the debug pages make."""

    def __init__(self, pods, logs=None, forbidden=()):
        self.pods, self.logs, self.forbidden, self.calls = pods, logs or {}, set(forbidden), []

    async def aclose(self):
        pass

    async def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/apis/metrics.k8s.io/v1beta1/pods":
            return FakeResponse(body={"items": []})
        m = re.fullmatch(r"/api/v1/namespaces/([^/]+)/(pods|events)(?:/([^/]+))?(/log)?", path)
        ns, kind, name, log = m.groups()
        if kind == "events":
            return FakeResponse(body={"items": [{"involvedObject": {"kind": "Pod", "name": "api-1"}, "type": "Warning",
                                                 "reason": "BackOff", "message": "Back-off restarting", "count": 3,
                                                 "lastTimestamp": "2026-10-03T05:10:00Z"}]})
        if log:
            if ns in self.forbidden:
                return FakeResponse(403, {"message": "forbidden"})
            which = "previous" if (params or {}).get("previous") else "current"
            return FakeResponse(text=self.logs.get((name, which), ""))
        if name:
            match = [p for p in self.pods if p["metadata"]["name"] == name and p["metadata"]["namespace"] == ns]
            return FakeResponse(body=match[0]) if match else FakeResponse(404, {})
        return FakeResponse(body={"items": [p for p in self.pods if p["metadata"]["namespace"] == ns]})


STATE = {
    "services": {"api": "up", "postgres": "up", "redis": "down", "traefik": "up", "ollama": "unknown",
                 "grafana": "up", "argocd": "up", "chat": "up", "kiwix": "up"},
    "links": {"api-postgres": "active", "api-redis": "down", "traefik-api": "active"},
    "rates": {"api-postgres": 2.5, "traefik-api": 3.0},
    "alerts": [],
}


def _setup(client, monkeypatch, k8s):
    client.app.state.k8s = k8s
    monkeypatch.setattr(display, "gather", AsyncMock(return_value=STATE))
    monkeypatch.setattr(display, "_query", AsyncMock(return_value=1.0))


def test_redact_masks_credential_shaped_text():
    cases = {
        "Authorization: Bearer abcdefghijklmnop": "[redacted]",
        "connecting to postgresql://app:hunter2secret@postgres:5432/db": "postgresql://app:[redacted]@postgres",
        'password="hunter2" user=bob': 'password="[redacted]',
        "GET /invite/AbCdEfGhIjKlMnOp HTTP/1.1": "/invite/[redacted]",
        "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N": "token=[redacted]",
    }
    for line, expected in cases.items():
        out = component.redact(line)
        assert expected in out, (line, out)
        assert "hunter2" not in out and "AbCdEfGhIjKlMnOp" not in out
    assert component.redact("GET /health 200 OK") == "GET /health 200 OK"


def test_summarize_pod_reports_why_it_last_ended():
    pod = component.summarize_pod(_pod("api-1", restarts=4, last={"reason": "OOMKilled", "exitCode": 137,
                                                                   "finishedAt": "2026-10-03T04:59:00Z"}))
    c = pod["containers"][0]
    assert pod["restarts"] == 4 and pod["ready"] and pod["node"] == "joe"
    assert c["last"]["reason"] == "OOMKilled" and c["last"]["exit_code"] == 137
    assert c["state"]["kind"] == "running" and c["mem_limit"] == "256Mi"


def test_component_page_shows_pods_events_logs_and_connections(client, monkeypatch):
    k8s = FakeK8s(
        [_pod("api-1", restarts=1, last={"reason": "Error", "exitCode": 1, "finishedAt": "2026-10-03T04:59:00Z"}),
         _pod("api-2"), _pod("redis-0")],
        logs={("api-1", "current"): "2026-10-03T05:00:01Z started\n2026-10-03T05:00:02Z GET /v1/chat 200",
              ("api-1", "previous"): "Traceback: password=hunter2 boom"})
    _setup(client, monkeypatch, k8s)
    r = client.get("/api/component/api")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "up" and d["title"] == "FastAPI"
    assert [p["name"] for p in d["pods"]] == ["api-1", "api-2"]  # not redis
    previous = [l for l in d["logs"] if l["which"] == "previous"]
    assert len(previous) == 1 and "hunter2" not in previous[0]["lines"][0]
    current = next(l for l in d["logs"] if l["pod"] == "api-1" and l["which"] == "current")
    assert len(current["lines"]) == 2
    log_calls = [p for path, p in k8s.calls if path.endswith("/log")]
    assert all(p["tailLines"] <= component.LOG_LINES and p["limitBytes"] == component.LOG_BYTES for p in log_calls)
    assert d["events"][0]["reason"] == "BackOff"
    conns = {c["edge"]: c for c in d["connections"]}
    assert conns["api-postgres"]["state"] == "active" and conns["api-postgres"]["direction"] == "out"
    assert conns["api-redis"]["state"] == "down" and conns["api-redis"]["other_status"] == "down"
    assert conns["traefik-api"]["direction"] == "in" and conns["traefik-api"]["rate"] == 3.0


def test_logs_the_dashboard_may_not_read_say_so(client, monkeypatch):
    _setup(client, monkeypatch, FakeK8s([_pod("api-1")], forbidden={"backend"}))
    logs = client.get("/api/component/api").json()["logs"]
    assert "isn't allowed" in logs[0]["error"]


def test_host_component_has_numbers_and_commands_but_no_pods(client, monkeypatch):
    _setup(client, monkeypatch, FakeK8s([]))
    d = client.get("/api/component/phone").json()
    assert "pods" not in d and d["metrics"] and any("journalctl" in c for c in d["commands"])
    assert d["metrics"][0]["good"] is True  # 1 s since the last update


def test_unknown_component_is_404(client):
    assert client.get("/api/component/nope").status_code == 404
    assert client.get("/component/nope").status_code == 404


def test_fish_page_and_its_box(client, monkeypatch):
    _setup(client, monkeypatch, FakeK8s([_pod("api-1")]))
    d = client.get("/api/pod/backend/api-1").json()
    assert d["title"] == "api-1" and d["box"]["id"] == "api" and d["connections"]
    assert client.get("/api/pod/backend/gone-1").status_code == 404


def test_pages_are_served_and_rechecked(client):
    for url in ("/component/api", "/component/pod/backend/api-1"):
        r = client.get(url)
        assert r.status_code == 200 and "component.js" in r.text
        assert r.headers["cache-control"] == "no-cache"


def test_every_box_and_line_on_the_map_has_a_page_entry():
    js = (STATIC / "display.js").read_text()
    boxes = re.findall(r"^\s+(\w+): \[\d+, \d+, \"", js, re.M)
    assert boxes and set(boxes) == set(component.COMPONENTS)
    block = js[js.index("const EDGES = ["):js.index("];", js.index("const EDGES = ["))]
    edges = {f"{a}-{b}" for a, b in re.findall(r'\["(\w+)", "(\w+)"', block)}
    assert edges and edges == set(component.EDGE_INFO)


def test_every_box_has_a_guide_and_its_docs_exist():
    repo = Path(__file__).resolve().parents[3]
    assert set(component.GUIDES) == set(component.COMPONENTS)
    for cid, g in component.GUIDES.items():
        assert g["how"] and g["fixes"] and g["docs"], cid
        assert all(len(f) == 2 and all(f) for f in g["fixes"]), cid
        for doc in g["docs"]:
            assert (repo / doc).is_file(), f"{cid}: {doc}"


def test_pages_carry_their_guide(client, monkeypatch):
    _setup(client, monkeypatch, FakeK8s([_pod("api-1")]))
    as_json = lambda g: json.loads(json.dumps(g))  # noqa: E731 - (symptom, fix) tuples arrive as lists
    assert client.get("/api/component/phone").json()["guide"] == as_json(component.GUIDES["phone"])
    assert client.get("/api/pod/backend/api-1").json()["guide"] == as_json(component.GUIDES["api"])  # a fish: its box's
