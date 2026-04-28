# syntax=docker/dockerfile:1.7
# Production Dockerfile for the coracle (slim variant).
# Multi-stage build keeps the runtime layer free of build tooling.
# Build: docker build -t coracle:slim --target runtime .
# Target image size: < 250 MB compressed.

ARG PYTHON_VERSION=3.11

# ---------- Stage 1: builder ----------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps (kept only in this stage). git is needed by setuptools-scm
# to derive the version when building from a checkout.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY pyproject.toml README.md ./
COPY coracle ./coracle

# `--user` install lands everything under /root/.local so the runtime
# stage can copy it as a single self-contained tree.
RUN pip install --user --no-cache-dir .

# ---------- Stage 2: runtime ----------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

# OCI image labels (https://github.com/opencontainers/image-spec)
LABEL org.opencontainers.image.source="https://github.com/skgandikota/coracle" \
      org.opencontainers.image.title="coracle" \
      org.opencontainers.image.description="Local-first agent coracle (slim runtime image)." \
      org.opencontainers.image.licenses="CC-BY-NC-SA-4.0" \
      org.opencontainers.image.url="https://github.com/skgandikota/coracle" \
      org.opencontainers.image.documentation="https://github.com/skgandikota/coracle/blob/main/docs/DEPLOY.md"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/coracle/.local/bin:${PATH}" \
    CORACLE_CONFIG=/etc/coracle/config.yaml \
    CORACLE_DATA_DIR=/var/lib/coracle

# curl is needed for the HEALTHCHECK; tini gives us a proper PID 1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
    && groupadd --system --gid 1000 coracle \
    && useradd  --system --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin coracle \
    && mkdir -p /etc/coracle /var/lib/coracle /app \
    && chown -R coracle:coracle /etc/coracle /var/lib/coracle /app

# Bring the installed package + console scripts from the builder.
COPY --from=builder --chown=coracle:coracle /root/.local /home/coracle/.local

WORKDIR /app

USER coracle

VOLUME ["/etc/coracle", "/var/lib/coracle"]

# OpenAI-compatible HTTP API. MCP-stdio is intentionally NOT exposed:
# attach via `docker exec -i` or `docker run -i ... mcp`.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/v1/models || exit 1

ENTRYPOINT ["tini", "--", "python", "-m", "coracle"]
CMD ["serve"]
