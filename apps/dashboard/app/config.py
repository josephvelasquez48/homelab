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

# Ollama, reached through the same in-cluster Service the api uses. The
# dashboard evicts resident models from here to free VRAM before gaming -
# see app/gpu.py for why that replaced an SSH-and-PowerShell path.
INFERENCE_URL = os.environ.get("INFERENCE_URL", "http://inference.ai.svc.cluster.local:11434")

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
# DASHBOARD_PASSWORD unset means nobody can authenticate, so /api/gpu/release
# is unreachable rather than open: the failure mode of a missing Secret is
# "the GPU button is broken", never "the GPU button is public".
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# A generated fallback keeps the pod starting without the Secret. The cost
# is that sessions do not survive a restart, which is the right way round
# for a single-replica dashboard - and far better than shipping a default
# signing key that would let anyone forge a session cookie.
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)

SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(12 * 60 * 60)))
