"""Minimal in-cluster Kubernetes API client.

Uses the ServiceAccount token/CA every pod gets mounted automatically
rather than the official `kubernetes` Python client - this app only
needs a handful of read-only GETs, and httpx is already the HTTP client
used throughout this project (apps/api), so this avoids a second HTTP
stack for no real benefit.
"""
import httpx

from app.config import K8S_API, K8S_CA_PATH, K8S_TOKEN_PATH


def _read_token() -> str:
    with open(K8S_TOKEN_PATH) as f:
        return f.read().strip()


def make_client() -> httpx.AsyncClient:
    token = _read_token()
    return httpx.AsyncClient(
        base_url=K8S_API,
        headers={"Authorization": f"Bearer {token}"},
        verify=K8S_CA_PATH,
        timeout=10.0,
    )


async def get_nodes(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get("/api/v1/nodes")
    r.raise_for_status()
    nodes = []
    for item in r.json()["items"]:
        conditions = {c["type"]: c["status"] for c in item["status"].get("conditions", [])}
        nodes.append(
            {
                "name": item["metadata"]["name"],
                "ready": conditions.get("Ready") == "True",
                "schedulable": not item["spec"].get("unschedulable", False),
                "roles": [
                    k.split("node-role.kubernetes.io/")[1]
                    for k in item["metadata"].get("labels", {})
                    if k.startswith("node-role.kubernetes.io/")
                ]
                or ["worker"],
            }
        )
    return nodes


async def get_pods(client: httpx.AsyncClient, namespace: str) -> list[dict]:
    r = await client.get(f"/api/v1/namespaces/{namespace}/pods")
    r.raise_for_status()
    pods = []
    for item in r.json()["items"]:
        statuses = item["status"].get("containerStatuses", [])
        ready = sum(1 for s in statuses if s.get("ready"))
        pods.append(
            {
                "name": item["metadata"]["name"],
                "namespace": namespace,
                "phase": item["status"].get("phase", "Unknown"),
                "ready": ready,
                "total": len(statuses),
                "restarts": sum(s.get("restartCount", 0) for s in statuses),
                "node": item["spec"].get("nodeName"),
            }
        )
    return pods


async def get_argo_applications(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications")
    r.raise_for_status()
    apps = []
    for item in r.json()["items"]:
        status = item.get("status", {})
        apps.append(
            {
                "name": item["metadata"]["name"],
                "sync_status": status.get("sync", {}).get("status", "Unknown"),
                "health_status": status.get("health", {}).get("status", "Unknown"),
            }
        )
    return apps
