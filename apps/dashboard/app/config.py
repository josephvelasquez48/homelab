import os

# In-cluster K8s API access - no kubeconfig needed, every pod gets a
# ServiceAccount token + CA cert mounted automatically.
K8S_API = "https://kubernetes.default.svc"
K8S_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

# Same in-cluster Prometheus the Grafana datasource points at
# (kubernetes/monitoring/grafana.yaml) - queried directly here rather than
# through Grafana, since the display only needs a handful of instant values.
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.monitoring.svc.cluster.local:9090")

# Alertmanager, for the list of what is currently firing. Read-only, and
# read directly rather than through Prometheus: Prometheus knows which
# rules are firing, but Alertmanager knows what survived grouping,
# inhibition and silences - which is what someone actually wants to see.
ALERTMANAGER_URL = os.environ.get(
    "ALERTMANAGER_URL", "http://alertmanager.monitoring.svc.cluster.local:9093"
)

# The Pi's LAN address, where the host services the display probes live
# (CoreDNS on :53, RustDesk on :21116) - see app/display.py.
HOST_IP = os.environ.get("HOST_IP", "192.168.1.253")

# Where the display's weather is for (Open-Meteo, no key). Unset hides it.
WEATHER_LAT = os.environ.get("WEATHER_LAT", "")
WEATHER_LON = os.environ.get("WEATHER_LON", "")
