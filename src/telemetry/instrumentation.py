"""
OpenTelemetry instrumentation guard blocks: redis / sql / boto / http.

Each block is independently try/except-guarded so a missing native library or
a misconfigured instrumentor never takes down the whole worker. They are
lift-and-shifted verbatim from app.py — do NOT collapse them into a shared
loop/registry: they're not actually uniform (different fault-isolation
granularity, different custom hooks per block), so unifying them risks a
quiet behaviour change for no real benefit.
"""
import os

from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
# botocore, requests, urllib3, sqlalchemy, psycopg2 are imported lazily
# inside their respective guard blocks so a missing native lib never
# crashes the whole app at worker startup.

from telemetry._logging import otel_log

_BOTO_ENABLED         = os.getenv("TELEMETRY_BOTO_ENABLED", "true").lower() == "true"
_BOTO_CAPTURE_HEADERS = os.getenv("TELEMETRY_BOTO_CAPTURE_HEADERS", "false").lower() == "true"
_HTTP_ENABLED         = os.getenv("TELEMETRY_HTTP_ENABLED", "true").lower() == "true"
_SQL_ENABLED          = os.getenv("TELEMETRY_SQL_ENABLED", "true").lower() == "true"


# request_hook enriches every Redis span with the command name and the first
# key argument so cache hit/miss patterns are visible without enabling full
# command logging (which may expose tile coordinates or auth tokens).
def _redis_request_hook(span, instance, args, kwargs):
    if not span or not span.is_recording():
        return
    if len(args) > 1:
        key = args[1].decode("utf-8", errors="replace") if isinstance(args[1], bytes) else str(args[1])
        span.set_attribute("db.redis.key", key[:500])


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


def install() -> None:
    """Install the redis / sql / boto / http instrumentors, in that order.

    Must run before make_wsgi_app() — see the ordering-contract comment at
    app.py's call site.
    """
    # ── Redis instrumentation ────────────────────────────────────────────────
    try:
        RedisInstrumentor().instrument(
            request_hook=_redis_request_hook,
        )
        otel_log.info("[otel-redis] RedisInstrumentor active (command+key hooks enabled)")
    except Exception:
        otel_log.exception("[otel-redis] RedisInstrumentor FAILED to initialise")

    # ── SQL instrumentation ──────────────────────────────────────────────────
    # Covers all three SQL layers MapProxy may use:
    #   SQLite3     – file-based tile/cache locks
    #   SQLAlchemy  – when MapProxy is configured with a SQLAlchemy cache backend
    #   psycopg2    – direct PostgreSQL connections (MapProxy postgis source / cache)
    # Disable all three with TELEMETRY_SQL_ENABLED=false.
    if _SQL_ENABLED:
        try:
            SQLite3Instrumentor().instrument()
            otel_log.info("[otel-sql] SQLite3Instrumentor active")
        except Exception:
            otel_log.exception("[otel-sql] SQLite3Instrumentor FAILED to initialise")
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument(
                enable_commenter=True,
                commenter_options={},
            )
            otel_log.info("[otel-sql] SQLAlchemyInstrumentor active")
        except Exception:
            otel_log.exception("[otel-sql] SQLAlchemyInstrumentor FAILED to initialise")
        try:
            from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
            Psycopg2Instrumentor().instrument(
                skip_dep_check=True,
                enable_commenter=True,
            )
            otel_log.info("[otel-sql] Psycopg2Instrumentor active")
        except Exception:
            otel_log.exception("[otel-sql] Psycopg2Instrumentor FAILED to initialise")
    else:
        otel_log.info("[otel-sql] SQL instrumentation disabled (TELEMETRY_SQL_ENABLED=false)")

    # ── AWS / botocore instrumentation ───────────────────────────────────────
    if _BOTO_ENABLED:
        try:
            from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
            BotocoreInstrumentor().instrument(
                request_hook=_boto_request_hook,
                response_hook=_boto_response_hook,
            )
            otel_log.info("[otel-boto] BotocoreInstrumentor active (request+response hooks, capture_headers=%s)",
                           _BOTO_CAPTURE_HEADERS)
        except Exception:
            otel_log.exception("[otel-boto] BotocoreInstrumentor FAILED to initialise")
    else:
        otel_log.info("[otel-boto] BotocoreInstrumentor disabled (TELEMETRY_BOTO_ENABLED=false)")

    # ── Outbound HTTP instrumentation ────────────────────────────────────────
    # Instruments requests + urllib3 so every upstream WMS/WMTS tile fetch and
    # health-check MapProxy makes appears as a child span in the trace.
    # Disable with TELEMETRY_HTTP_ENABLED=false.
    if _HTTP_ENABLED:
        try:
            from opentelemetry.instrumentation.requests import RequestsInstrumentor
            from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
            RequestsInstrumentor().instrument()
            URLLib3Instrumentor().instrument()
            otel_log.info("[otel-http] RequestsInstrumentor + URLLib3Instrumentor active")
        except Exception:
            otel_log.exception("[otel-http] HTTP instrumentors FAILED to initialise")
    else:
        otel_log.info("[otel-http] HTTP instrumentors disabled (TELEMETRY_HTTP_ENABLED=false)")
