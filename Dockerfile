# Copyright 2026 Query Farm LLC - https://query.farm
#
# Single image serving BOTH transports of the vgi-hackernews worker:
#   docker run -p 8000:8000 IMG   -> HTTP server on $PORT (default 8000; /health, VGI RPC)
#   docker run -i --rm IMG stdio  -> stdio worker DuckDB spawns on-host
# See docker-entrypoint.sh. The Hacker News API is public and keyless, so the
# image carries no credentials and needs none; it only needs outbound HTTPS to
# hacker-news.firebaseio.com.
# syntax=docker/dockerfile:1
FROM python:3.13-slim

ARG VERSION=0.0.0
ARG GIT_COMMIT=unknown
ARG SOURCE_URL=https://github.com/Query-farm/vgi-hackernews

LABEL org.opencontainers.image.title="vgi-hackernews" \
      org.opencontainers.image.description="Hacker News front page, stories, comment threads and users for DuckDB via VGI (stdio + HTTP)" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.licenses="MIT" \
      farm.query.vgi.transports='["http","stdio"]'

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

# curl backs the HEALTHCHECK and the CI /health smoke.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install the worker and its HTTP-serving extra from the source tree. The wheel
# packages `vgi_hackernews` (catalog, HackerNewsWorker, main), so the image is
# self-contained.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY vgi_hackernews ./vgi_hackernews
RUN pip install '.[serve]'

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Run as an unprivileged user: the worker only reads a public API. It gets a
# writable home of its own because vgi keeps its per-worker state store under
# $HOME/.local/state/vgi. Since vgi-python 0.34.1 that store is created on
# first use rather than on import, so a read-only home no longer stops the
# worker from starting — but a feature that does use the store still needs it.
RUN groupadd --system vgi && useradd --system --gid vgi --create-home --home-dir /home/vgi vgi
USER vgi

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=8s \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
