# MapProxy — Docker Image

MapProxy running under **uWSGI**, instrumented with **OpenTelemetry** (traces, metrics, and log correlation) and exported via OTLP gRPC. Instrumentation covers inbound HTTP, Redis, filesystem tile cache, outbound HTTP, SQL, and AWS/botocore calls.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Repository Layout](#repository-layout)
- [Configuration](#configuration)
  - [mapproxy.yaml](#mapproxyyaml)
  - [Environment Variables](#environment-variables)
- [Running with Docker](#running-with-docker)
- [Running with Docker Compose](#running-with-docker-compose)
- [OpenTelemetry Integration](#opentelemetry-integration)
  - [Traces](#traces)
  - [Metrics](#metrics)
  - [Log Correlation](#log-correlation)
  - [Instrumentation Summary](#instrumentation-summary)
- [CORS](#cors)
- [uWSGI Tuning](#uwsgi-tuning)
- [Health Check](#health-check)
- [Building the Image Locally](#building-the-image-locally)
- [Exposed Ports](#exposed-ports)
- [Volumes](#volumes)
- [OpenShift / Arbitrary UID](#openshift--arbitrary-uid)

---

## Quick Start

```bash
# 1. Pull the image
docker pull acrarolibotnonprod.azurecr.io/raster/mapproxy:v6.0.1

# 2. Place your mapproxy.yaml in a local config directory
mkdir -p ./config
cp your-mapproxy.yaml ./config/mapproxy.yaml

# 3. Run
docker run -d \
  --name mapproxy \
  -p 8080:8080 \
  -v "$(pwd)/config:/mapproxy/config:ro" \
  -e OTEL_EXPORTER_OTLP_ENDPOINT="your-otel-collector:4317" \
  acrarolibotnonprod.azurecr.io/raster/mapproxy:v6.0.1
```

MapProxy will be available at `http://localhost:8080/`.

> **Note:** `OTEL_EXPORTER_OTLP_ENDPOINT` must be a bare `host:port` — **no** `http://` prefix. The gRPC channel is opened with `insecure=True`.

---

## Repository Layout

```
.
├── Dockerfile
├── entrypoint.sh          ← creates /tmp/mapproxy dirs, execs uWSGI as PID 1
├── .dockerignore
└── src/
    └── app.py             ← MapProxy WSGI app + OpenTelemetry instrumentation
```

**`config/`** is not part of the image — mount it at runtime:

```
config/
├── mapproxy.yaml    ← MapProxy configuration (required, mounted read-only)
└── log.ini          ← optional Python logging config (e.g. via k8s ConfigMap)
```

Tile data is **not** stored inside the container. Point `globals.cache.base_dir` in your `mapproxy.yaml` at a PVC or host path and mount it separately.

---

## Configuration

### mapproxy.yaml

Mount your configuration file into `/mapproxy/config/mapproxy.yaml` (override path with `MAPPROXY_CONFIG`). A minimal example:

```yaml
services:
  demo:
  wms:
    srs: ["EPSG:4326", "EPSG:3857"]
    md:
      title: My MapProxy
      abstract: MapProxy WMS

layers:
  - name: osm
    title: OpenStreetMap
    sources: [osm_cache]

caches:
  osm_cache:
    grids: [webmercator]
    sources: [osm_source]

sources:
  osm_source:
    type: tile
    url: https://tile.openstreetmap.org/%(z)s/%(x)s/%(y)s.png

grids:
  webmercator:
    base: GLOBAL_WEBMERCATOR

globals:
  cache:
    base_dir: /outputs/tiles
    lock_dir: /tmp/mapproxy/locks
```

---

### Environment Variables

#### MapProxy

| Variable          | Default                    | Description                                                                             |
| ----------------- | -------------------------- | --------------------------------------------------------------------------------------- |
| `MAPPROXY_CONFIG` | `/mapproxy/mapproxy.yaml`  | Path to the MapProxy configuration file                                                 |
| `LOG_CONFIG`      | `/mapproxy/log.ini`        | Path to a Python `logging.config` ini file; falls back to `basicConfig` if not present |
| `SERVICE_VERSION` | *(set to `MAPPROXY_VERSION` at build time)* | Version string reported as `service.version` in OTel resource attributes  |

#### uWSGI

| Variable    | Default | Description                                      |
| ----------- | ------- | ------------------------------------------------ |
| `PROCESSES` | `6`     | Number of uWSGI worker processes (`--processes`) |
| `THREADS`   | `10`    | Threads per worker (`--threads`)                 |

#### OpenTelemetry — General

| Variable                      | Default           | Description                                          |
| ----------------------------- | ----------------- | ---------------------------------------------------- |
| `OTEL_SERVICE_NAME`           | `mapproxy`        | Service name reported to the collector               |
| `TELEMETRY_TRACING_ENDPOINT`  | `localhost:4317`  | OTLP gRPC endpoint — **bare `host:port`, no scheme** |
| `OTEL_TRACES_EXPORTER`        | `otlp`            | Traces exporter type                                 |
| `OTEL_METRICS_EXPORTER`       | `otlp`            | Metrics exporter type                                |
| `OTEL_LOGS_EXPORTER`          | `otlp`            | Logs exporter type                                   |
| `OTEL_PROPAGATORS`            | `tracecontext,baggage,b3` | Trace context propagation formats          |
| `OTEL_PYTHON_LOG_CORRELATION` | `false`           | Kept for reference; log correlation is managed directly by `app.py` — changing this has no effect |
| `PYTHONUNBUFFERED`            | `1`               | Flush logs immediately                               |

#### OpenTelemetry — Tracing

| Variable                                 | Default | Description                                                                      |
| ---------------------------------------- | ------- | -------------------------------------------------------------------------------- |
| `TELEMETRY_TRACING_ENABLED`              | `true`  | Set to `false` to disable all span creation                                      |
| `TELEMETRY_TRACING_SAMPLING_RATIO_DENOM` | `10`    | Sample 1-in-N requests (e.g. `10` = 10%, `100` = 1%)                             |
| `OTEL_TRACE_DEBUG`                       | `true`  | Also print spans to stdout via `ConsoleSpanExporter` — **disable in production** |

#### OpenTelemetry — Instrumentation toggles

| Variable                         | Default | Description                                                    |
| -------------------------------- | ------- | -------------------------------------------------------------- |
| `TELEMETRY_BOTO_ENABLED`         | `true`  | Instrument botocore/boto3 AWS API calls                        |
| `TELEMETRY_BOTO_CAPTURE_HEADERS` | `false` | Attach full (non-Body) request params to boto spans            |
| `TELEMETRY_HTTP_ENABLED`         | `true`  | Instrument outbound `requests` and `urllib3` calls             |
| `TELEMETRY_SQL_ENABLED`          | `true`  | Instrument SQLite3, SQLAlchemy, and psycopg2 queries           |
| `TELEMETRY_TILE_CACHE_ENABLED`   | `true`  | Monkey-patch `FileCache` to emit spans for filesystem tile I/O |

#### CORS

| Variable               | Default       | Description                              |
| ---------------------- | ------------- | ---------------------------------------- |
| `CORS_ENABLED`         | `true`        | Add CORS headers to every response       |
| `CORS_ALLOWED_ORIGIN`  | `*`           | Value for `Access-Control-Allow-Origin`  |
| `CORS_ALLOWED_HEADERS` | `*`           | Value for `Access-Control-Allow-Headers` |
| `CORS_ALLOWED_METHODS` | `GET,OPTIONS` | Value for `Access-Control-Allow-Methods` |

---

## Running with Docker

### Minimal (no OTel collector)

```bash
docker run -d \
  --name mapproxy \
  -p 8080:8080 \
  -v "$(pwd)/config:/mapproxy/config:ro" \
  acrarolibotnonprod.azurecr.io/raster/mapproxy:v6.0.1
```

> If no OTLP collector is reachable the OTel SDK drops telemetry silently — MapProxy itself continues to function normally.

### With an OTLP collector

```bash
docker run -d \
  --name mapproxy \
  -p 8080:8080 \
  -v "$(pwd)/config:/mapproxy/config:ro" \
  -e OTEL_SERVICE_NAME="mapproxy-prod" \
  -e OTEL_EXPORTER_OTLP_ENDPOINT="otel-collector:4317" \
  --network your-network \
  acrarolibotnonprod.azurecr.io/raster/mapproxy:v6.0.1
```

### Using an env file

```env
OTEL_SERVICE_NAME=mapproxy-prod
OTEL_EXPORTER_OTLP_ENDPOINT=otel-collector:4317
OTEL_PROPAGATORS=tracecontext,baggage,b3
MAPPROXY_CONFIG=/mapproxy/config/mapproxy.yaml
OTEL_TRACE_DEBUG=false
TELEMETRY_TRACING_SAMPLING_RATIO_DENOM=100
```

```bash
docker run -d \
  --name mapproxy \
  -p 8080:8080 \
  -v "$(pwd)/config:/mapproxy/config:ro" \
  --env-file .env \
  acrarolibotnonprod.azurecr.io/raster/mapproxy:v6.0.1
```

---

## Running with Docker Compose

```yaml
version: "3.9"

services:
  mapproxy:
    image: acrarolibotnonprod.azurecr.io/raster/mapproxy:v6.0.1
    ports:
      - "8080:8080"
    volumes:
      - ./config:/mapproxy/config:ro
    environment:
      OTEL_SERVICE_NAME: mapproxy
      OTEL_EXPORTER_OTLP_ENDPOINT: otel-collector:4317 # bare host:port
      OTEL_TRACE_DEBUG: "false"
      TELEMETRY_TRACING_SAMPLING_RATIO_DENOM: "100"
    depends_on:
      - otel-collector
    restart: unless-stopped

  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    ports:
      - "4317:4317" # OTLP gRPC
      - "4318:4318" # OTLP HTTP
    volumes:
      - ./otel-collector-config.yaml:/etc/otelcol-contrib/config.yaml:ro
    restart: unless-stopped
```

Start the stack:

```bash
docker compose up -d
```

---

## OpenTelemetry Integration

### Traces

Every inbound HTTP request is wrapped in a span by `OpenTelemetryMiddleware` (outermost layer, inside CORS). Spans are exported in OTLP gRPC format to `OTEL_EXPORTER_OTLP_ENDPOINT`.

The `BatchSpanProcessor` is initialised **after** uWSGI forks workers (`--lazy-app`). This is required — initialising the processor in the master process causes its background export thread to die silently on fork.

The gRPC channel uses a **bare `host:port`** with `insecure=True`. Passing an `http://` or `https://` prefix causes silent TLS negotiation failure.

### Metrics

A `MeterProvider` with a `PeriodicExportingMetricReader` (60 s interval) exports metrics to the same OTLP endpoint.

### Log Correlation

`LoggingInstrumentor` injects `otelTraceID` and `otelSpanID` attributes into every `LogRecord`. The log formatter reads those attributes and includes them in every line:

```
2026-03-03 12:00:00,123 INFO mapproxy [trace_id=4bf92f3577b34da6 span_id=00f067aa0ba902b7] GET /wmts/... 200
```

When no active span exists (e.g. background worker logs) the fields are printed as empty strings:

```
2026-03-03 12:00:00,456 INFO mapproxy.otel [trace_id= span_id=] [otel-probe] collector REACHABLE at otel-collector:4317
```

> **`log.ini` users:** if you mount a custom `log.ini`, add `%(otelTraceID)s` and `%(otelSpanID)s` to your formatter's `format` string to preserve correlation. The `_OtelFormatter` fallback only applies to the built-in `basicConfig` path.

### Instrumentation Summary

| Instrumentation       | Library                                              | Toggle                         | Span / attributes                                                          |
| --------------------- | ---------------------------------------------------- | ------------------------------ | -------------------------------------------------------------------------- |
| Inbound HTTP          | `opentelemetry-instrumentation-wsgi`                 | always on                      | HTTP method, route, status                                                 |
| Redis                 | `opentelemetry-instrumentation-redis`                | always on                      | `db.redis.command`, `db.redis.key` (first key only)                        |
| Filesystem tile cache | monkey-patch (`mapproxy.cache.file.FileCache`)       | `TELEMETRY_TILE_CACHE_ENABLED` | `tile.x/y/z`, `cache.directory`, `cache.hit`, `tile.size_bytes`            |
| Outbound HTTP         | `opentelemetry-instrumentation-requests` + `urllib3` | `TELEMETRY_HTTP_ENABLED`       | standard HTTP semconv                                                      |
| SQLite3               | `opentelemetry-instrumentation-sqlite3`              | `TELEMETRY_SQL_ENABLED`        | SQL statement                                                              |
| SQLAlchemy            | `opentelemetry-instrumentation-sqlalchemy`           | `TELEMETRY_SQL_ENABLED`        | SQL statement + commenter                                                  |
| psycopg2              | `opentelemetry-instrumentation-psycopg2`             | `TELEMETRY_SQL_ENABLED`        | SQL statement + commenter                                                  |
| AWS / botocore        | `opentelemetry-instrumentation-botocore`             | `TELEMETRY_BOTO_ENABLED`       | `aws.service`, `rpc.method`, S3 bucket/key/ETag, STS role ARN, HTTP status |

#### Collector startup probe

At each worker start `app.py` opens a TCP connection to the collector endpoint and logs the result:

```
[otel-probe] collector REACHABLE at otel-collector:4317
```

or:

```
[otel-probe] collector UNREACHABLE at localhost:4317 — ConnectionRefusedError: … (traces will be dropped until resolved)
```

#### Debug mode

`OTEL_TRACE_DEBUG=true` (the default) attaches a `ConsoleSpanExporter` that prints every span to stdout. Set it to `false` in production to avoid log noise.

---

## CORS

CORS is handled by a lightweight inline middleware (no extra dependencies). It runs as the outermost WSGI layer, deduplicating any headers already added by MapProxy's own `access_control_allow_origin` setting to prevent double headers.

Pre-flight `OPTIONS` requests are short-circuited with `200 OK` before reaching MapProxy.

---

## uWSGI Tuning

The container starts uWSGI with the following fixed flags (not configurable at runtime):

| Flag                         | Value / behaviour                                                      |
| ---------------------------- | ---------------------------------------------------------------------- |
| `--socket 0.0.0.0:3031`      | uwsgi binary protocol — use as nginx upstream                          |
| `--http-socket 0.0.0.0:8080` | plain HTTP — use for liveness probes and direct access                 |
| `--master`                   | master process manages workers                                         |
| `--lazy-app`                 | workers load `app.py` **after** fork — required for OTel thread safety |
| `--harakiri 120`             | kill workers that take > 120 s                                         |
| `--max-requests 1000`        | recycle worker after 1000 requests                                     |
| `--reload-on-rss 2048`       | recycle worker if RSS exceeds 2 GB                                     |
| `--die-on-term`              | map `SIGTERM` → graceful shutdown (correct k8s behaviour)              |
| `--vacuum`                   | clean up sockets on exit                                               |

Worker count and thread count are set at runtime via `PROCESSES` and `THREADS`.

---

## Health Check

The image includes a Docker `HEALTHCHECK` that polls `http://localhost:8080/` every 30 s (5 s timeout, 15 s start period, 3 retries). Kubernetes liveness/readiness probes should target port `8080` directly — no separate endpoint is needed.

---

## Building the Image Locally

```bash
git clone <this-repo>
cd docker-mapproxy

docker build \
  --build-arg SERVICE_VERSION=2.0.0 \
  --build-arg MAPPROXY_VERSION=6.0.1 \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --build-arg VCS_REF="$(git rev-parse --short HEAD)" \
  -t acrarolibotnonprod.azurecr.io/raster/mapproxy:v6.0.1 .
```

Push to the registry:

```bash
az acr login --name acrarolibotnonprod
docker push acrarolibotnonprod.azurecr.io/raster/mapproxy:v6.0.1
```

The image uses a **multi-stage build**: all compile-time dependencies (GCC, GDAL/GEOS/PROJ headers) are confined to the builder stage and the final image contains only the pre-built `/opt/venv` virtual environment plus shared runtime libraries.

`MAPPROXY_VERSION` is the single source of truth — it is used in `pip install`, the OCI image labels, and the `SERVICE_VERSION` env var read by `app.py` at runtime.

---

## Exposed Ports

| Port   | Protocol | Description                                            |
| ------ | -------- | ------------------------------------------------------ |
| `8080` | HTTP     | Plain HTTP — liveness probes and direct browser access |
| `3031` | uwsgi    | Binary uwsgi protocol — intended as nginx upstream     |

---

## Volumes

| Mount path         | Access    | Description                                        |
| ------------------ | --------- | -------------------------------------------------- |
| `/mapproxy/config` | Read-only | MapProxy YAML + optional `log.ini`                 |

Tile data directories (e.g. `/outputs`, `/layerSources`) are **not** declared as volumes — mount them as PVCs or host paths via your orchestrator.

uWSGI lock and cache temp files are written to `/tmp/mapproxy/` (created by `entrypoint.sh` at container start, no volume needed).

---

## OpenShift / Arbitrary UID

The image is compatible with OpenShift's Security Context Constraint that runs containers with a random UID in group `0`. All files under `/mapproxy` are `chgrp 0 && chmod g=u` at build time so the random UID can read and write configuration at runtime.
