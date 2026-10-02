FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

# pyproject only, like the other images: uv.lock is written by a far newer
# uv (0.12) than the 0.5.11 pinned above, and isn't read here.
COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project

COPY app ./app

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# --proxy-headers: Traefik is the only way in, and the login rate limit
# needs the browser's address from X-Forwarded-For, not Traefik's.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
