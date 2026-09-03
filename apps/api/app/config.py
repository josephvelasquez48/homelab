import os

import httpx

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
JOB_QUEUE_KEY = "jobs:queue"

# A single blanket timeout used to cover both "can we even reach Ollama"
# and "how long can generation legitimately take" meant a dead backend
# took as long to fail as a slow-but-working one - up to 3 retry attempts
# at the full timeout each, approaching 6 minutes before the request
# resolved to a 502 (see docs/failure-testing.md). connect stays tight
# since this is LAN traffic to a single host; read stays generous since
# real generation can legitimately take tens of seconds.
OLLAMA_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)

API_KEY = os.environ["API_KEY"]
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60"))
