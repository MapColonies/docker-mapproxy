"""
MapProxy WSGI application wrapped with OpenTelemetry instrumentation.

Load order matters here.  The OTel providers own gRPC channels that are not
fork-safe, so they are built by the telemetry package in the worker rather
than at import time — see telemetry/__init__.py for the postfork/worker_id
dispatch that picks the right moment for both lazy-app settings; read it
before moving either of the two calls below across the boundaries they mark.

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
from opentelemetry.instrumentation.wsgi import OpenTelemetryMiddleware
from mapproxy.wsgiapp import make_wsgi_app

import os

import telemetry

# Instrumentors must run before make_wsgi_app(): mapproxy constructs its cache
# backends/connections there, and anything patched afterward is not instrumented.
telemetry.install_instrumentation()

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

# The dispatch must stay last: moving it earlier is untested territory, not a
# validated equivalent. See telemetry.init_when_safe()'s docstring.
telemetry.init_when_safe()
