"""
MapProxy WSGI application wrapped with OpenTelemetry instrumentation.

Override the behaviour with environment variables:
  OTEL_SERVICE_NAME                        - service name reported to the collector
  TELEMETRY_TRACING_ENDPOINT              - OTLP gRPC collector endpoint
  MAPPROXY_CONFIG                          - path to mapproxy.yaml
  TELEMETRY_TRACING_ENABLED                - set to 'true' to enable tracing (default: true)
  TELEMETRY_TRACING_SAMPLING_RATIO_DENOMINATOR   - 1-in-N sampling (default: 1000, i.e. 0.1%)

  CORS_ENABLED                   - set to 'true' to enable CORS headers
  CORS_ALLOWED_ORIGIN            - value for Access-Control-Allow-Origin  (default: *)
  CORS_ALLOWED_HEADERS           - value for Access-Control-Allow-Headers (default: *)
  CORS_ALLOWED_METHODS           - value for Access-Control-Allow-Methods (default: GET,OPTIONS)

  TELEMETRY_BOTO_ENABLED         - instrument botocore/boto3 AWS calls (default: true)
  TELEMETRY_BOTO_CAPTURE_HEADERS - capture request/response headers on boto spans (default: false)
  TELEMETRY_HTTP_ENABLED         - instrument outbound requests/urllib3 HTTP calls (default: true)
  TELEMETRY_SQL_ENABLED          - instrument SQLite3/SQLAlchemy/psycopg2 queries (default: true)
"""
from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.wsgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
# botocore, requests, urllib3, sqlalchemy, psycopg2 are imported lazily
# inside their respective guard blocks so a missing native lib never
# crashes the whole app at worker startup.
from mapproxy.wsgiapp import make_wsgi_app

import os
import socket
import logging
from logging.config import fileConfig

# OTel-aware formatter — includes trace/span IDs when an active span exists.
# LoggingInstrumentor injects otelTraceID/otelSpanID onto LogRecords, but only
# after instrument() is called.  Records emitted before that point (early import
# logs, the collector probe, etc.) don't carry those attributes, so a plain
# %-format string raises KeyError.  This formatter fills in safe defaults so the
# same format string works for the entire process lifetime.
class _OtelFormatter(logging.Formatter):
    _OTEL_FMT = (
        "%(asctime)s %(levelname)s %(name)s "
        "[trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] "
        "%(message)s"
    )

    def __init__(self):
        super().__init__(fmt=self._OTEL_FMT)

    def format(self, record: logging.LogRecord) -> str:
        record.__dict__.setdefault("otelTraceID", "")
        record.__dict__.setdefault("otelSpanID", "")
        return super().format(record)

_LOG_INI = os.getenv("LOG_CONFIG", "/mapproxy/log.ini")
if os.path.isfile(_LOG_INI):
    # fileConfig installs handlers from the ini file; disable_existing_loggers=False
    # preserves any loggers already created by imports above.
    fileConfig(_LOG_INI, {'here': os.path.dirname(os.path.abspath(_LOG_INI))}, disable_existing_loggers=False)
else:
    # force=True replaces any handlers accumulated by early imports, ensuring
    # exactly one StreamHandler on the root logger.
    _handler = logging.StreamHandler()
    _handler.setFormatter(_OtelFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)

# ── OTel SDK diagnostic logging ───────────────────────────────────────────────
# Enables the OTel SDK's own internal logger so exporter errors, retry
# attempts, and dropped spans are visible in docker/k8s logs.
_otel_log = logging.getLogger("mapproxy.otel")
_otel_log.setLevel(logging.DEBUG)
for _sdk_logger in (
    "opentelemetry.sdk.trace.export",
    "opentelemetry.exporter.otlp.proto.grpc",
    "opentelemetry.sdk.metrics.export",
):
    logging.getLogger(_sdk_logger).setLevel(logging.DEBUG)

_SERVICE_VERSION       = os.getenv("SERVICE_VERSION", "0.0.0")
_OTLP_ENDPOINT        = os.getenv("TELEMETRY_TRACING_ENDPOINT", "localhost:4317")
_TRACING_ENABLED      = os.getenv("TELEMETRY_TRACING_ENABLED", "true").lower() == "true"
_SAMPLE_DENOM         = max(1, int(os.getenv("TELEMETRY_TRACING_SAMPLING_RATIO_DENOMINATOR", "1000")))
_TRACE_DEBUG          = os.getenv("OTEL_TRACE_DEBUG", "true").lower() == "true"
_BOTO_ENABLED         = os.getenv("TELEMETRY_BOTO_ENABLED", "true").lower() == "true"
_BOTO_CAPTURE_HEADERS = os.getenv("TELEMETRY_BOTO_CAPTURE_HEADERS", "false").lower() == "true"
_HTTP_ENABLED         = os.getenv("TELEMETRY_HTTP_ENABLED", "true").lower() == "true"
_SQL_ENABLED          = os.getenv("TELEMETRY_SQL_ENABLED", "true").lower() == "true"
_TILE_CACHE_TRACING   = os.getenv("TELEMETRY_TILE_CACHE_ENABLED", "true").lower() == "true"

# ── Collector TCP probe ───────────────────────────────────────────────────────
# Runs once at worker startup. Logs clearly whether the collector is reachable
# before any spans are sent — avoids silent trace loss.
def _probe_collector(endpoint: str) -> None:
    try:
        _raw = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        _host, _port = _raw.rsplit(":", 1)
        with socket.create_connection((_host, int(_port)), timeout=5):
            _otel_log.info("[otel-probe] collector REACHABLE at %s", endpoint)
    except Exception as exc:
        _otel_log.error(
            "[otel-probe] collector UNREACHABLE at %s — %s: %s "
            "(traces will be dropped until resolved)",
            endpoint, type(exc).__name__, exc,
        )

_probe_collector(_OTLP_ENDPOINT)

# ── Resource ──────────────────────────────────────────────────────────────────
resource = Resource.create({
    "service.name":    os.getenv("OTEL_SERVICE_NAME", "mapproxy"),
    "service.version": _SERVICE_VERSION,
})

# ── Tracing ───────────────────────────────────────────────────────────────────
# Sample 1-in-N requests (default 1/10 = 10%) — tune via TELEMETRY_TRACING_SAMPLING_RATIO_DENOMINATOR.
# OTLPSpanExporter (gRPC) requires a bare host:port — strip http:// and set
# insecure=True explicitly, otherwise the channel defaults to TLS and the
# handshake fails silently against a plaintext collector endpoint.
try:
    _grpc_endpoint = _OTLP_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
    _otel_log.info("[otel-trace] gRPC endpoint: %s  insecure=True  sampling=1/%s",
                   _grpc_endpoint, _SAMPLE_DENOM)
    tracer_provider = TracerProvider(
        resource=resource,
        sampler=TraceIdRatioBased(1 / _SAMPLE_DENOM) if _TRACING_ENABLED else TraceIdRatioBased(0),
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=_grpc_endpoint, insecure=True),
            max_export_batch_size=512,
            export_timeout_millis=10_000,
        )
    )
    # OTEL_TRACE_DEBUG=true → also print every span to stdout so you can
    # confirm spans are being created independently of collector connectivity.
    if _TRACE_DEBUG:
        _otel_log.warning("[otel-trace] OTEL_TRACE_DEBUG=true — ConsoleSpanExporter active (not for production)")
        tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(tracer_provider)
    _otel_log.info("[otel-trace] TracerProvider ready")
except Exception:
    _otel_log.exception("[otel-trace] FAILED to initialise — tracing disabled")
    tracer_provider = TracerProvider(resource=resource, sampler=TraceIdRatioBased(0))
    trace.set_tracer_provider(tracer_provider)

# ── Metrics ───────────────────────────────────────────────────────────────────
try:
    _grpc_endpoint = _OTLP_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=_grpc_endpoint, insecure=True),
                export_interval_millis=60_000,
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)
    _otel_log.info("[otel-metrics] MeterProvider ready → %s", _grpc_endpoint)
except Exception:
    _otel_log.exception("[otel-metrics] FAILED to initialise — metrics disabled")
    metrics.set_meter_provider(MeterProvider(resource=resource))

# ── Redis instrumentation ────────────────────────────────────────────────────────────
# request_hook enriches every Redis span with the command name and the first
# key argument so cache hit/miss patterns are visible without enabling full
# command logging (which may expose tile coordinates or auth tokens).
def _redis_request_hook(span, instance, args, kwargs):
    if not span or not span.is_recording():
        return
    if len(args) > 1:
        key = args[1].decode("utf-8", errors="replace") if isinstance(args[1], bytes) else str(args[1])
        span.set_attribute("db.redis.key", key[:500])

try:
    RedisInstrumentor().instrument(
        tracer_provider=tracer_provider,
        request_hook=_redis_request_hook,
    )
    _otel_log.info("[otel-redis] RedisInstrumentor active (command+key hooks enabled)")
except Exception:
    _otel_log.exception("[otel-redis] RedisInstrumentor FAILED to initialise")

# ── SQL instrumentation ─────────────────────────────────────────────────────────────
# Covers all three SQL layers MapProxy may use:
#   SQLite3     – file-based tile/cache locks
#   SQLAlchemy  – when MapProxy is configured with a SQLAlchemy cache backend
#   psycopg2    – direct PostgreSQL connections (MapProxy postgis source / cache)
# Disable all three with TELEMETRY_SQL_ENABLED=false.
if _SQL_ENABLED:
    try:
        SQLite3Instrumentor().instrument(tracer_provider=tracer_provider)
        _otel_log.info("[otel-sql] SQLite3Instrumentor active")
    except Exception:
        _otel_log.exception("[otel-sql] SQLite3Instrumentor FAILED to initialise")
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        SQLAlchemyInstrumentor().instrument(
            tracer_provider=tracer_provider,
            enable_commenter=True,
            commenter_options={},
        )
        _otel_log.info("[otel-sql] SQLAlchemyInstrumentor active")
    except Exception:
        _otel_log.exception("[otel-sql] SQLAlchemyInstrumentor FAILED to initialise")
    try:
        from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
        Psycopg2Instrumentor().instrument(
            tracer_provider=tracer_provider,
            skip_dep_check=True,
            enable_commenter=True,
        )
        _otel_log.info("[otel-sql] Psycopg2Instrumentor active")
    except Exception:
        _otel_log.exception("[otel-sql] Psycopg2Instrumentor FAILED to initialise")
else:
    _otel_log.info("[otel-sql] SQL instrumentation disabled (TELEMETRY_SQL_ENABLED=false)")

# ── AWS / botocore instrumentation ───────────────────────────────────────────
# request_hook: fires before every AWS API call — extracts S3 bucket/key/prefix,
#               STS role ARN, and (when TELEMETRY_BOTO_CAPTURE_HEADERS=true) the
#               full sanitised param dict so you can diagnose mis-configured calls.
# response_hook: fires after every AWS response — adds HTTP status, AWS request ID,
#                S3 ETag/ContentLength/ContentType to the span.
def _boto_request_hook(span, service_name, operation_name, api_params):
    if not span or not span.is_recording():
        return
    if service_name == "s3":
        if "Bucket" in api_params:
            span.set_attribute("aws.s3.bucket", api_params["Bucket"])
        if "Key" in api_params:
            span.set_attribute("aws.s3.key", api_params["Key"])
        if "Prefix" in api_params:
            span.set_attribute("aws.s3.prefix", api_params["Prefix"])
        if "CopySource" in api_params:
            src = api_params["CopySource"]
            span.set_attribute("aws.s3.copy_source", str(src)[:500])
        # Full params only when explicitly opted-in — Body is excluded to avoid
        # logging large binary payloads.
        if _BOTO_CAPTURE_HEADERS and "Body" not in api_params:
            span.set_attribute("aws.request.params", str(api_params)[:2000])
    elif service_name == "sts":
        if "RoleArn" in api_params:
            span.set_attribute("aws.sts.role_arn", api_params["RoleArn"])
        if "RoleSessionName" in api_params:
            span.set_attribute("aws.sts.session_name", api_params["RoleSessionName"])

def _boto_response_hook(span, service_name, operation_name, result):
    if not span or not span.is_recording():
        return
    meta = result.get("ResponseMetadata", {})
    if meta.get("HTTPStatusCode"):
        span.set_attribute("http.status_code", meta["HTTPStatusCode"])
    if meta.get("RequestId"):
        span.set_attribute("aws.request_id", meta["RequestId"])
    if meta.get("HostId"):
        span.set_attribute("aws.s3.host_id", meta["HostId"])
    if service_name == "s3":
        if "ETag" in result:
            span.set_attribute("aws.s3.etag", result["ETag"].strip('"'))
        if "ContentLength" in result:
            span.set_attribute("aws.s3.content_length", result["ContentLength"])
        if "ContentType" in result:
            span.set_attribute("aws.s3.content_type", result["ContentType"])
        if "VersionId" in result:
            span.set_attribute("aws.s3.version_id", result["VersionId"])

if _BOTO_ENABLED:
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
        BotocoreInstrumentor().instrument(
            tracer_provider=tracer_provider,
            request_hook=_boto_request_hook,
            response_hook=_boto_response_hook,
        )
        _otel_log.info("[otel-boto] BotocoreInstrumentor active (request+response hooks, capture_headers=%s)",
                       _BOTO_CAPTURE_HEADERS)
    except Exception:
        _otel_log.exception("[otel-boto] BotocoreInstrumentor FAILED to initialise")
else:
    _otel_log.info("[otel-boto] BotocoreInstrumentor disabled (TELEMETRY_BOTO_ENABLED=false)")

# ── Outbound HTTP instrumentation ─────────────────────────────────────────────
# Instruments requests + urllib3 so every upstream WMS/WMTS tile fetch and
# health-check MapProxy makes appears as a child span in the trace.
# Disable with TELEMETRY_HTTP_ENABLED=false.
if _HTTP_ENABLED:
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
        RequestsInstrumentor().instrument(tracer_provider=tracer_provider)
        URLLib3Instrumentor().instrument(tracer_provider=tracer_provider)
        _otel_log.info("[otel-http] RequestsInstrumentor + URLLib3Instrumentor active")
    except Exception:
        _otel_log.exception("[otel-http] HTTP instrumentors FAILED to initialise")
else:
    _otel_log.info("[otel-http] HTTP instrumentors disabled (TELEMETRY_HTTP_ENABLED=false)")

# ── Logging correlation ───────────────────────────────────────────────────────
# Instruments the root logger to inject otelTraceID / otelSpanID attributes
# into every LogRecord so the format string above can reference them.
# set_logging_format is intentionally omitted (defaults to False via the env
# var OTEL_PYTHON_LOG_CORRELATION=false set in the Dockerfile) so that
# LoggingInstrumentor does NOT call logging.basicConfig() internally — the
# format and handlers are already configured above and must not be overwritten.
LoggingInstrumentor().instrument()

# ── FileCache monkey-patch for filesystem tile tracing ────────────────────────
# MapProxy reads tiles directly via open() — no network library to instrument.
# Wrapping FileCache.load_tile / load_tiles / store_tile gives a span for every
# filesystem tile operation, including the layer name, tile coords, directory,
# cache hit/miss and byte size.
# Disable with TELEMETRY_TILE_CACHE_ENABLED=false.
if _TILE_CACHE_TRACING:
    try:
        from mapproxy.cache.file import FileCache as _FileCache
        _tile_tracer = trace.get_tracer("mapproxy.cache.file")
        _orig_load_tile  = _FileCache.load_tile
        _orig_load_tiles = _FileCache.load_tiles
        _orig_store_tile = _FileCache.store_tile

        def _traced_load_tile(self, tile, with_metadata=False, **kwargs):
            with _tile_tracer.start_as_current_span("file_cache.load_tile") as span:
                if span.is_recording():
                    span.set_attribute("tile.x",            tile.coord[0])
                    span.set_attribute("tile.y",            tile.coord[1])
                    span.set_attribute("tile.z",            tile.coord[2])
                    span.set_attribute("cache.directory",   str(getattr(self, "cache_dir", "")))
                result = _orig_load_tile(self, tile, with_metadata, **kwargs)
                if span.is_recording():
                    span.set_attribute("cache.hit", tile.source is not None)
                    if tile.source is not None and hasattr(tile, "size") and tile.size:
                        span.set_attribute("tile.size_bytes", tile.size)
                return result

        def _traced_load_tiles(self, tiles, with_metadata=False, **kwargs):
            with _tile_tracer.start_as_current_span("file_cache.load_tiles") as span:
                if span.is_recording():
                    span.set_attribute("tile.batch_size",  len(tiles))
                    span.set_attribute("cache.directory",  str(getattr(self, "cache_dir", "")))
                result = _orig_load_tiles(self, tiles, with_metadata, **kwargs)
                if span.is_recording():
                    hits   = sum(1 for t in tiles if t.source is not None)
                    misses = len(tiles) - hits
                    span.set_attribute("cache.hits",   hits)
                    span.set_attribute("cache.misses", misses)
                return result

        def _traced_store_tile(self, tile, **kwargs):
            with _tile_tracer.start_as_current_span("file_cache.store_tile") as span:
                if span.is_recording():
                    span.set_attribute("tile.x",          tile.coord[0])
                    span.set_attribute("tile.y",          tile.coord[1])
                    span.set_attribute("tile.z",          tile.coord[2])
                    span.set_attribute("cache.directory", str(getattr(self, "cache_dir", "")))
                    if hasattr(tile, "size") and tile.size:
                        span.set_attribute("tile.size_bytes", tile.size)
                return _orig_store_tile(self, tile, **kwargs)

        _FileCache.load_tile  = _traced_load_tile
        _FileCache.load_tiles = _traced_load_tiles
        _FileCache.store_tile = _traced_store_tile
        _otel_log.info("[otel-filecache] FileCache monkey-patched (load_tile, load_tiles, store_tile)")
    except Exception:
        _otel_log.exception("[otel-filecache] FileCache tracing FAILED to initialise")
else:
    _otel_log.info("[otel-filecache] FileCache tracing disabled (TELEMETRY_TILE_CACHE_ENABLED=false)")

# ── MapProxy WSGI app + OTel WSGI middleware ──────────────────────────────────
_mapproxy = make_wsgi_app(
    os.getenv("MAPPROXY_CONFIG", "/mapproxy/mapproxy.yaml"),
    reloader=False,
)
application = OpenTelemetryMiddleware(_mapproxy)

# ── CORS middleware ───────────────────────────────────────────────────────────
# Implemented inline (zero extra dependencies) following the MapColonies pattern.
# Deduplicates any CORS headers already injected by MapProxy's own
# access_control_allow_origin setting so headers are never sent twice.
_CORS_HEADER_NAMES = {
    "access-control-allow-origin",
    "access-control-allow-headers",
    "access-control-allow-methods",
    "access-control-max-age",
}

if os.getenv("CORS_ENABLED", "false").lower() == "true":
    _cors_origin  = os.getenv("CORS_ALLOWED_ORIGIN",  "*")
    _cors_headers = os.getenv("CORS_ALLOWED_HEADERS", "*")
    _cors_methods = os.getenv("CORS_ALLOWED_METHODS", "GET,OPTIONS")

    _cors_headers_to_add = [
        ("Access-Control-Allow-Origin",  _cors_origin),
        ("Access-Control-Allow-Headers", _cors_headers),
        ("Access-Control-Allow-Methods", _cors_methods),
        ("Access-Control-Max-Age",       "86400"),
    ]

    _inner = application

    def application(environ, start_response):  # noqa: F811
        method = environ.get("REQUEST_METHOD", "")

        # Handle pre-flight OPTIONS without forwarding to MapProxy
        if method == "OPTIONS":
            start_response("200 OK", list(_cors_headers_to_add))
            return [b""]

        def _start_response(status, headers, exc_info=None):
            # Remove any CORS headers already set by MapProxy to avoid duplicates
            filtered = [
                (k, v) for k, v in headers
                if k.lower() not in _CORS_HEADER_NAMES
            ]
            filtered.extend(_cors_headers_to_add)
            return start_response(status, filtered, exc_info)

        return _inner(environ, _start_response)
