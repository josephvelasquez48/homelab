import os
import secrets

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
# Optional override. Normally empty, and the address is read from the
# node's InternalIP at call time instead - neither host has a DHCP
# reservation, so a hardcoded address here goes stale the next time a
# lease moves. Set it to pin a specific address (local runs, or if the
# K8s API is unavailable and gaming mode still needs to work).
GAMING_SSH_HOST = os.environ.get("GAMING_SSH_HOST", "")

# Host key verification is keyed to this alias rather than to an address,
# so a lease change does not turn into a host key mismatch. ssh is passed
# -o HostKeyAlias, and the known_hosts entry uses the alias in place of
# the IP (kubernetes/dashboard/dashboard.yaml).
GAMING_SSH_HOST_KEY_ALIAS = os.environ.get("GAMING_SSH_HOST_KEY_ALIAS", "homelab-desktop")
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

# Session auth for the state-changing gaming-mode endpoints. Both values
# come from the dashboard-auth Secret
# (kubernetes/secrets/dashboard-auth.enc.yaml, SOPS-encrypted and applied
# out-of-band like the others - see docs/secrets.md).
#
# DASHBOARD_PASSWORD unset means nobody can authenticate, so /api/gaming/*
# is unreachable rather than open: the failure mode of a missing Secret is
# "gaming mode is broken", never "gaming mode is public".
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# A generated fallback keeps the pod starting without the Secret. The cost
# is that sessions do not survive a restart, which is the right way round
# for a single-replica dashboard - and far better than shipping a default
# signing key that would let anyone forge a session cookie.
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)

SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(12 * 60 * 60)))
