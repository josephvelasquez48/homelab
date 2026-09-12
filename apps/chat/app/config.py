import os

# The real conversation API. Reached in-cluster rather than through
# api.home, so this never leaves the pod network and does not depend on
# Traefik or DNS being healthy to work.
API_URL = os.environ.get("API_URL", "http://api.backend.svc.cluster.local:8000")

# The API requires a key. It lives here, server-side, and is never sent to
# the browser - which is the whole reason this app proxies instead of
# letting the page call the API directly. A key in page JavaScript is
# readable by anyone who can load the page.
API_KEY = os.environ.get("API_KEY", "")

# Generation can run long on a 7B model, and the first request after idle
# also pays a model load of roughly 45 seconds. A short timeout here would
# cut off replies that were about to arrive.
STREAM_TIMEOUT = float(os.environ.get("STREAM_TIMEOUT", "600"))
