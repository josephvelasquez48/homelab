FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml ./
RUN uv sync --no-install-project --no-dev

COPY main.py ./

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 9618

CMD ["python", "main.py"]
