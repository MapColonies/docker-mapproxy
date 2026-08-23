# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# MapProxy with OpenTelemetry Instrumentation
# ─────────────────────────────────────────────────────────────────────────────

# Image/app version — this repo's own SemVer. Distinct from the packaged
# MapProxy engine version below. CI passes the release tag; the default keeps
# local builds and labels correct between releases.
ARG SERVICE_VERSION=2.0.0

# Packaged MapProxy engine version — source of truth for the pip install and
# the MapProxy-engine label only (NOT the image version).
ARG MAPPROXY_VERSION=6.0.1

# OCI label build args (set by CI; safe defaults for local builds)
ARG BUILD_DATE="1970-01-01T00:00:00Z"
ARG VCS_REF="unknown"
ARG IMAGE_SOURCE="https://github.com/MapColonies/docker-mapproxy"

# ── Build Stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS builder

ARG MAPPROXY_VERSION

# Install build dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgeos-dev \
    libgdal-dev \
    libproj-dev \
    libcairo2-dev \
    libpango1.0-dev \
    libjpeg-dev \
    libpng-dev \
    libffi-dev

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    "MapProxy==${MAPPROXY_VERSION}" \
    "uWSGI" \
    "opentelemetry-api" \
    "opentelemetry-sdk" \
    "opentelemetry-distro" \
    "opentelemetry-instrumentation-wsgi" \
    "opentelemetry-instrumentation-logging" \
    "opentelemetry-instrumentation-redis" \
    "opentelemetry-instrumentation-sqlite3" \
    "opentelemetry-exporter-otlp-proto-grpc" \
    "opentelemetry-exporter-otlp-proto-http" \
    "opentelemetry-propagator-b3" \
    "opentelemetry-instrumentation-botocore" \
    "opentelemetry-instrumentation-requests" \
    "opentelemetry-instrumentation-urllib3" \
    "opentelemetry-instrumentation-urllib" \
    "opentelemetry-instrumentation-sqlalchemy" \
    "opentelemetry-instrumentation-psycopg2" \
    "psycopg2-binary" \
    "boto3" \
    "sqlalchemy" \
    "redis[hiredis]"

# Apply MapProxy patches
# Uses a bind mount so the patch file is never written into an image layer —
# only the result (the overwritten site-packages file) is committed.
# Set PATCH_FILES=false at build time to skip (e.g. for upstream compat tests).
ARG PATCH_FILES=true
RUN --mount=type=bind,source=config/patch/redis.py,target=/tmp/redis_patch.py \
    if [ "${PATCH_FILES}" = "true" ]; then \
    cp /tmp/redis_patch.py \
    /opt/venv/lib/python3.11/site-packages/mapproxy/cache/redis.py \
    && python -c "import sys, mapproxy.cache.redis as m; rem = getattr(m.RedisCache, '__abstractmethods__', frozenset()); sys.exit('RedisCache has unimplemented abstract methods: %s' % (set(rem),)) if rem else None" \
    && echo "[patch] redis.py applied and import+abstractmethod check OK" \
    || { echo "[patch] redis.py FAILED — build aborted" >&2; exit 1; }; \
    else \
    echo "[patch] PATCH_FILES=false — redis.py patch skipped (upstream file unchanged)"; \
    fi

RUN --mount=type=bind,source=config/patch/s3.py,target=/tmp/s3_patch.py \
    if [ "${PATCH_FILES}" = "true" ]; then \
    cp /tmp/s3_patch.py \
    /opt/venv/lib/python3.11/site-packages/mapproxy/cache/s3.py \
    && python -c "import sys, mapproxy.cache.s3 as m; rem = getattr(m.S3Cache, '__abstractmethods__', frozenset()); sys.exit('S3Cache has unimplemented abstract methods: %s' % (set(rem),)) if rem else None" \
    && echo "[patch] s3.py applied and import+abstractmethod check OK" \
    || { echo "[patch] s3.py FAILED — build aborted" >&2; exit 1; }; \
    else \
    echo "[patch] PATCH_FILES=false — s3.py patch skipped (upstream file unchanged)"; \
    fi

# Fix the invalid <ows:SupportedCRS> urn MapProxy emits since 6.0.0: correct
# it to the OGC-valid form (urn:ogc:def:crs:EPSG::4326 — upstream renders a
# single colon before the code, which violates OGC 07-092r1).  This one edits
# the installed template in place instead of replacing it, so an upstream
# rework of that line aborts the build rather than silently reverting other
# template changes.
RUN --mount=type=bind,source=config/patch/wmts_capabilities_compat.py,target=/tmp/wmts_capabilities_compat.py \
    if [ "${PATCH_FILES}" = "true" ]; then \
    python /tmp/wmts_capabilities_compat.py \
    /opt/venv/lib/python3.11/site-packages/mapproxy/service/templates/wmts100capabilities.xml \
    && echo "[patch] wmts100capabilities.xml applied and verified" \
    || { echo "[patch] wmts100capabilities.xml FAILED — build aborted" >&2; exit 1; }; \
    else \
    echo "[patch] PATCH_FILES=false — wmts100capabilities.xml patch skipped (upstream file unchanged)"; \
    fi

# ── Runtime Stage ───────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# Install runtime libraries (changes rarely — cached early)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgeos-c1v5 \
    libgdal32 \
    libproj25 \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libjpeg62-turbo \
    libpng16-16

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Re-declare ARGs needed in this stage (ARGs don't cross FROM boundaries)
ARG SERVICE_VERSION
ARG MAPPROXY_VERSION
ARG BUILD_DATE
ARG VCS_REF
ARG IMAGE_SOURCE

# OCI metadata labels — image.version is THIS image's version (SERVICE_VERSION);
# the packaged MapProxy engine version is a separate, explicit label.
LABEL org.opencontainers.image.title="docker-mapproxy" \
    org.opencontainers.image.version="${SERVICE_VERSION}" \
    org.opencontainers.image.description="docker-mapproxy ${SERVICE_VERSION} — MapProxy ${MAPPROXY_VERSION} with OpenTelemetry instrumentation" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.revision="${VCS_REF}" \
    org.opencontainers.image.source="${IMAGE_SOURCE}" \
    io.mapcolonies.mapproxy.version="${MAPPROXY_VERSION}"

# Environment defaults — paths & Python
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED="1"

# Environment defaults — service version (read by app.py at runtime as the
# OTel service.version). This is the image/app version, not the MapProxy version.
ENV SERVICE_VERSION="${SERVICE_VERSION}"

# Environment defaults — OpenTelemetry
ENV OTEL_SERVICE_NAME="mapproxy" \
    OTEL_TRACES_EXPORTER="otlp" \
    OTEL_METRICS_EXPORTER="otlp" \
    OTEL_LOGS_EXPORTER="otlp" \
    TELEMETRY_TRACING_ENDPOINT="localhost:4317" \
    OTEL_PROPAGATORS="tracecontext,baggage,b3" \
    OTEL_PYTHON_LOG_CORRELATION="false"

# Environment defaults — Application
ENV MAPPROXY_CONFIG="/mapproxy/mapproxy.yaml" \
    LOG_CONFIG="/mapproxy/log.ini"

# Environment defaults — Telemetry
ENV TELEMETRY_TRACING_ENABLED="true" \
    TELEMETRY_TRACING_SAMPLING_RATIO_DENOMINATOR="1000" \
    TELEMETRY_BOTO_ENABLED="true" \
    TELEMETRY_BOTO_CAPTURE_HEADERS="false" \
    TELEMETRY_HTTP_ENABLED="true" \
    TELEMETRY_SQL_ENABLED="true" \
    TELEMETRY_TILE_CACHE_ENABLED="true"

# Environment defaults — CORS
ENV CORS_ENABLED="true" \
    CORS_ALLOWED_ORIGIN="*" \
    CORS_ALLOWED_HEADERS="*" \
    CORS_ALLOWED_METHODS="GET,OPTIONS"

# Environment defaults — Redis resilience
# Short timeouts ensure a slow/unreachable Redis never stalls a tile request;
# errors return False (cache-miss) so MapProxy falls back to the next source.
# SSL_CERT_REQS: server-cert verification ('required'/'optional'/'none').
ENV SOCKET_TIMEOUT_SECONDS="0.1" \
    SOCKET_CONNECTION_TIMEOUT_SECONDS="0.1" \
    REDIS_RETRY_ATTEMPTS="0" \
    REDIS_TLS="false" \
    REDIS_HEALTH_CHECK_INTERVAL="0" \
    SSL_CERT_REQS="optional"

# Environment defaults — uWSGI tuning
ENV PROCESSES="6" \
    THREADS="10"

# Create non-root user and working directory
RUN useradd -m -u 1000 -s /bin/bash mapproxy && \
    mkdir -p /mapproxy

WORKDIR /mapproxy

# Copy application code and entrypoint (single layer, correct ownership)
COPY --chown=mapproxy:mapproxy src/app.py       /mapproxy/app.py
COPY --chown=mapproxy:mapproxy src/telemetry/   /mapproxy/telemetry/
COPY --chown=mapproxy:mapproxy entrypoint.sh    /mapproxy/entrypoint.sh

# Fix permissions: mapproxy owns /mapproxy, group 0 can read/write (OpenShift)
RUN chown -R mapproxy:mapproxy /mapproxy && \
    chgrp -R 0 /mapproxy && \
    chmod -R g=u /mapproxy && \
    chmod +x /mapproxy/entrypoint.sh

# Health check — hit the HTTP socket to confirm uWSGI + MapProxy are alive
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:8080/ || exit 1

# Drop to non-root
USER mapproxy

# Expose HTTP (8080) and uWSGI binary protocol (3031)
EXPOSE 8080 3031

# Entrypoint creates temp dirs, then execs the CMD so uWSGI is PID 1
ENTRYPOINT ["/mapproxy/entrypoint.sh"]

CMD ["uwsgi"]
