"""
Logging bootstrap for the telemetry package.

Leaf module — must not import from any other module in this package, so
instrumentation.py, mapproxy_cache.py, and __init__.py can all import
otel_log from here without circular-import ordering games.
"""
import logging
import os
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
otel_log = logging.getLogger("mapproxy.otel")
otel_log.setLevel(logging.DEBUG)
for _sdk_logger in (
    "opentelemetry.sdk.trace.export",
    "opentelemetry.exporter.otlp.proto.grpc",
    "opentelemetry.sdk.metrics.export",
):
    logging.getLogger(_sdk_logger).setLevel(logging.DEBUG)
