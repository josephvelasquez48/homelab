FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml ./
RUN uv sync --no-install-project --no-dev

COPY main.py ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1

# Non-root - nothing here needs root, and Compose additionally runs this
# with read_only + cap_drop: [ALL] + no-new-privileges (docker-compose.yml).
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin exporter \
    && chown -R exporter:exporter /app
USER exporter

EXPOSE 9618

CMD ["python", "main.py"]
