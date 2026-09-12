FROM python:3.12.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-cache && useradd --uid 10001 --create-home team \
    && mkdir /artifacts /workspace /codex-auth && chown team:team /artifacts /workspace /codex-auth
COPY prompts ./prompts
COPY vendor ./vendor
ENV PATH="/app/.venv/bin:$PATH" ARTIFACTS_DIR=/artifacts
USER 10001:10001
CMD ["uvicorn", "agent_team.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

FROM node:22.22.0-bookworm-slim AS node
FROM runtime AS worker
USER root
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g @openai/codex@0.154.0 \
    && apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* /root/.npm
USER 10001:10001
CMD ["uvicorn", "agent_team.worker:create_worker", "--factory", "--host", "0.0.0.0", "--port", "8090"]
