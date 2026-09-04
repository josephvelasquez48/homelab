import os

# In-cluster K8s API access - no kubeconfig needed, every pod gets a
# ServiceAccount token + CA cert mounted automatically.
K8S_API = "https://kubernetes.default.svc"
K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

# Namespaces worth summarizing on the status page. Deliberately excludes
# argocd/kube-system - noisy, and not what someone glancing at "is my
# homelab healthy" cares about.
WATCHED_NAMESPACES = ["backend", "data", "ai", "monitoring", "dashboard"]

GAMING_NODE_NAME = os.environ.get("GAMING_NODE_NAME", "desktop-j1grrmu")

# Reaching the desktop over SSH to run gaming-mode/{pregame,postgame}.ps1
# - see docs/dashboard.md for the key setup (dedicated key, LAN-only,
# key-only auth) and docs/gaming-mode.md for what the scripts do.
GAMING_SSH_HOST = os.environ.get("GAMING_SSH_HOST", "192.168.1.131")
GAMING_SSH_USER = os.environ.get("GAMING_SSH_USER", "josep")
GAMING_SSH_KEY_PATH = os.environ.get("GAMING_SSH_KEY_PATH", "/secrets/ssh/id_ed25519")
GAMING_SSH_KNOWN_HOSTS_PATH = os.environ.get(
    "GAMING_SSH_KNOWN_HOSTS_PATH", "/config/known_hosts"
)
GAMING_SCRIPT_DIR = os.environ.get("GAMING_SCRIPT_DIR", "D:\\homelab\\gaming-mode")

API_HEALTH_URL = os.environ.get("API_HEALTH_URL", "http://api.backend.svc.cluster.local:8000/health")

# Same in-cluster Prometheus the Grafana datasource points at
# (kubernetes/monitoring/grafana.yaml) - queried directly here rather than
# through Grafana, since this page only needs a handful of instant values.
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.monitoring.svc.cluster.local:9090")
