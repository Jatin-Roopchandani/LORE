# syntax=docker/dockerfile:1.7
# LORE: one image for the web app (`lore ui`) and the job runner (`lore worker`).
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t lore:$(git rev-parse --short HEAD) .
# Cross-build for the server (arm64) from an x86 machine:
#   docker buildx build --platform linux/arm64 -f deploy/Dockerfile .

# --- 1. the web UI ------------------------------------------------------------
# Built on the build machine's own platform: the output is plain JS/CSS, so an
# arm64 build never runs node under emulation.
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund --fetch-retries=5
COPY web/ ./
# tsc --noEmit, vite build into ../lore/web/static (see web/package.json).
RUN mkdir -p ../lore/web/static && npm run build

# --- 2. Python dependencies and the lore package -------------------------------
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_HTTP_TIMEOUT=120 \
    UV_HTTP_RETRIES=5 \
    UV_CONCURRENT_DOWNLOADS=4 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first (cached until uv.lock changes), without dev tools.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY lore/ lore/
COPY --from=web /src/lore/web/static/ lore/web/static/
# The package itself, installed (not editable) with the built UI inside it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- 3. runtime -----------------------------------------------------------------
FROM python:3.12-slim-bookworm
RUN groupadd --system --gid 10001 lore \
 && useradd --system --uid 10001 --gid lore --home-dir /app --shell /usr/sbin/nologin lore \
 && mkdir -p /data /app \
 && chown lore:lore /data
COPY --from=build /app/.venv /app/.venv
COPY lore.toml /app/lore.toml
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LORE_CONFIG=/app/lore.toml \
    LORE_DATA_DIR=/data \
    HOME=/app
WORKDIR /app
USER lore:lore
VOLUME ["/data"]
EXPOSE 8000
# /healthz checks the central database. 127.0.0.1:8000 is one of the addresses
# the app answers (its origin guard refuses any other Host). The worker has no
# HTTP server: compose turns this check off for it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
CMD ["lore", "ui", "--host", "0.0.0.0", "--port", "8000"]
