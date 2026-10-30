# syntax=docker/dockerfile:1.7
#
# Multi-stage build:
#   builder  - compiles the virtualenv (CPU-only torch keeps the image ~4x smaller than the CUDA wheel)
#   runtime  - slim, non-root, read-only-friendly image with only the venv copied in
#
# Build:  docker build -t crypto-quant:local .
# Run:    docker compose up -d        (see docker-compose.yml)

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN python -m venv "${VIRTUAL_ENV}"

WORKDIR /build

# Dependency layer first: only invalidated when requirements.txt changes, not on every code edit.
COPY requirements.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2.0" \
 && pip install -r requirements.txt

COPY pyproject.toml README.md requirements-dev.txt ./
COPY src ./src
RUN pip install --no-deps .


FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="crypto-quant" \
      org.opencontainers.image.description="Hyperliquid microstructure trading bot with Deribit GEX gating" \
      org.opencontainers.image.source="https://github.com/Ad1xon/crypto-bot" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    HF_HOME=/app/.cache/huggingface \
    DASHBOARD_ENABLED=false \
    LOG_FORMAT=json \
    METRICS_PORT=9100

RUN groupadd --system --gid 10001 bot \
 && useradd --system --uid 10001 --gid bot --home-dir /app --shell /usr/sbin/nologin bot

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

RUN mkdir -p /app/data /app/logs /app/.cache \
 && chown -R bot:bot /app

USER bot

EXPOSE 9100

# Liveness = market data is actually flowing, not merely "PID exists".
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["crypto-quant-healthcheck", "--port", "9100", "--max-age", "120"]

STOPSIGNAL SIGTERM

CMD ["crypto-quant"]
