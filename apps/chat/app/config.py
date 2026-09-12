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

# Models offered in the picker, as "id|label" pairs. A list rather than a
# live query of Ollama: the card holds 8GB, so only one 7B model is resident
# at a time and every switch costs a reload of roughly 45 seconds. Offering
# everything installed would invite thrashing the GPU between turns.
#
# Chosen per conversation rather than per message for the same reason.
CHAT_MODELS = os.environ.get(
    "CHAT_MODELS",
    "qwen2.5:7b|General (default),qwen2.5-coder:7b|Code",
)
