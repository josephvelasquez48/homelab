import os

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# MediaMTX on the Mac (apps/cam/mac/). Reached by address, not through a
# Service/Endpoints pair: Argo CD ignores Endpoints, so a changed address
# there syncs "successfully" and changes nothing (kubernetes/ai/inference.yaml
# learned that). Here it's a ConfigMap value, which Argo does apply.
MEDIAMTX_URL = os.environ.get("MEDIAMTX_URL", "http://192.168.1.180:8889")
MEDIAMTX_PATH = os.environ.get("MEDIAMTX_PATH", "cam")
# The viewer login from ~/.config/homelab-cam/viewer-password on the Mac.
# Server-side only, like chat's API key: the browser never needs it, because
# this app does the WebRTC signaling on its behalf.
MEDIAMTX_USER = os.environ.get("MEDIAMTX_USER", "cam-app")
MEDIAMTX_PASSWORD = os.environ.get("MEDIAMTX_PASSWORD", "")

# Where people open the app; invite links are built from it.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://cam.home").rstrip("/")

SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
INVITE_DAYS = int(os.environ.get("INVITE_DAYS", "7"))

# Off only for tests over plain HTTP. In the cluster TLS ends at Traefik,
# and a session cookie must never travel without it.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true") == "true"
