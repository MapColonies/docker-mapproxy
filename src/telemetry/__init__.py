"""
Telemetry package: OTel provider construction, instrumentor installation, and
the postfork/worker_id dispatch that decides when providers may be built.

The OTel providers own gRPC channels that are not fork-safe, so they are built
by _init_telemetry() in the worker rather than at import time. Instrumentation
is installed against ProxyTracers (via install_instrumentation(), called from
app.py before make_wsgi_app()) that resolve once _init_telemetry() lands.
init_when_safe() — called last in app.py — picks the right moment to run
_init_telemetry() for both lazy-app settings; read its docstring before moving
anything across that boundary.
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
from opentelemetry.instrumentation.logging import LoggingInstrumentor

import atexit
import os
import socket

from telemetry import instrumentation, filecache_tracing
from telemetry._logging import otel_log

_SERVICE_VERSION = os.getenv("SERVICE_VERSION", "0.0.0")
_OTLP_ENDPOINT   = os.getenv("TELEMETRY_TRACING_ENDPOINT", "localhost:4317")
_TRACING_ENABLED = os.getenv("TELEMETRY_TRACING_ENABLED", "true").lower() == "true"
_SAMPLE_DENOM    = max(1, int(os.getenv("TELEMETRY_TRACING_SAMPLING_RATIO_DENOMINATOR", "1000")))
_TRACE_DEBUG     = os.getenv("OTEL_TRACE_DEBUG", "true").lower() == "true"

# ── Collector TCP probe ───────────────────────────────────────────────────────
# Runs once at worker startup. Logs clearly whether the collector is reachable
# before any spans are sent — avoids silent trace loss.
def _probe_collector(endpoint: str) -> None:
    try:
        _raw = endpoint.replace("https://", "").replace("http://", "").rstrip("/")
        _host, _port = _raw.rsplit(":", 1)
        with socket.create_connection((_host, int(_port)), timeout=5):
            otel_log.info("[otel-probe] collector REACHABLE at %s", endpoint)
    except Exception as exc:
        otel_log.error(
            "[otel-probe] collector UNREACHABLE at %s — %s: %s "
            "(traces will be dropped until resolved)",
            endpoint, type(exc).__name__, exc,
        )

# ── Resource ──────────────────────────────────────────────────────────────────
resource = Resource.create({
    "service.name":    os.getenv("OTEL_SERVICE_NAME", "mapproxy"),
    "service.version": _SERVICE_VERSION,
})

# ── Telemetry providers ───────────────────────────────────────────────────────
# Populated by _init_telemetry(), which MUST run post-fork.  See init_when_safe()
# below for how that is arranged.
#
# Nothing at import scope may call trace.set_tracer_provider() /
# metrics.set_meter_provider().  Until the first such call the OTel API hands
# out ProxyTracer / proxy-instrument objects that resolve lazily on first use,
# and that indirection is what lets the instrumentors below be installed
# pre-fork while the providers they end up using are built per worker.  The
# first set-provider call wins and later ones are a no-op, so a master-side
# call would permanently bind every proxy to the master's provider — including
# its fork-unsafe gRPC channels — defeating the arrangement below.
tracer_provider = None
meter_provider  = None


def _shutdown_telemetry() -> None:
    """Flush both providers so the final batch is exported before we exit.

    Workers recycle often (max-requests, reload-on-rss).  Without this the spans
    and metrics still sitting in the BatchSpanProcessor / PeriodicExporting-
    MetricReader queues are discarded on every recycle.

    Registered via atexit, which under uWSGI is BEST-EFFORT ONLY.  The python
    plugin returns from uwsgi_python_atexit() without reaching Py_Finalize() --
    so without running any atexit handler -- if the worker is hijacked, is still
    busy in a request, or is running async.  Recycles that land while a sibling
    thread is mid-request therefore still drop the queue.  SIGKILL paths
    (harakiri, worker-reload-mercy expiry) skip it outright.  Do not treat this
    as a guarantee; a uWSGI-level shutdown hook would be needed for that.
    """
    if tracer_provider is not None:
        try:
            tracer_provider.shutdown()
            otel_log.info("[otel-trace] TracerProvider shut down — final batch flushed")
        except Exception:
            otel_log.exception("[otel-trace] TracerProvider shutdown FAILED — spans may be lost")
    if meter_provider is not None:
        try:
            meter_provider.shutdown()
            otel_log.info("[otel-metrics] MeterProvider shut down — final batch flushed")
        except Exception:
            otel_log.exception("[otel-metrics] MeterProvider shutdown FAILED — metrics may be lost")


def _init_telemetry() -> None:
    """Build the providers, their gRPC exporters, and the export threads.

    Runs post-fork.  Two reasons, in order of weight:

    1. The OTLP exporters hold gRPC channels, and grpc-python does not support
       forking with live channels.  The SDK's at-fork handling restarts export
       threads but does NOT rebuild those channels, so this is not covered.
    2. It avoids depending on private SDK internals for correctness — that
       machinery has already moved between versions (see below).

    Do NOT reintroduce the claim that a master-built provider loses its export
    threads on fork.  Verified against the SDK pinned in this image (1.44.0):
    BatchSpanProcessor delegates to BatchProcessor in
    opentelemetry.sdk._shared_internal, which registers an os.register_at_fork
    handler that restarts the export thread in the child;
    PeriodicExportingMetricReader registers one as well.  A span emitted in a
    forked child was exported with no force_flush.  Note the module: in SDK
    1.7.1 that handler lived in opentelemetry.sdk.trace.export, which is why
    grepping the old location reports zero and looks like it is missing.

    Calling this resolves every ProxyTracer handed out at import time.
    """
    global tracer_provider, meter_provider

    _probe_collector(_OTLP_ENDPOINT)

    # ── Tracing ───────────────────────────────────────────────────────────────
    # Sample 1-in-N requests — tune via TELEMETRY_TRACING_SAMPLING_RATIO_DENOMINATOR.
    # OTLPSpanExporter (gRPC) requires a bare host:port — strip http:// and set
    # insecure=True explicitly, otherwise the channel defaults to TLS and the
    # handshake fails silently against a plaintext collector endpoint.
    try:
        _grpc_endpoint = _OTLP_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
        otel_log.info("[otel-trace] gRPC endpoint: %s  insecure=True  sampling=1/%s",
                       _grpc_endpoint, _SAMPLE_DENOM)
        _provider = TracerProvider(
            resource=resource,
            sampler=TraceIdRatioBased(1 / _SAMPLE_DENOM) if _TRACING_ENABLED else TraceIdRatioBased(0),
        )
        _provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=_grpc_endpoint, insecure=True),
                max_export_batch_size=512,
                export_timeout_millis=10_000,
            )
        )
        # OTEL_TRACE_DEBUG=true → also print every span to stdout so you can
        # confirm spans are being created independently of collector connectivity.
        if _TRACE_DEBUG:
            otel_log.warning("[otel-trace] OTEL_TRACE_DEBUG=true — ConsoleSpanExporter active (not for production)")
            _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(_provider)
        otel_log.info("[otel-trace] TracerProvider ready")
    except Exception:
        otel_log.exception("[otel-trace] FAILED to initialise — tracing disabled")
        try:
            trace.set_tracer_provider(TracerProvider(resource=resource, sampler=TraceIdRatioBased(0)))
        except Exception:
            otel_log.exception("[otel-trace] fallback provider also FAILED")
    finally:
        # Bind the global to what the API actually holds, never to the object we
        # built.  set_tracer_provider is FIRST-WINS: a second call logs
        # "Overriding of current TracerProvider is not allowed" and does nothing.
        # So if the block above raised *after* the set succeeded, the fallback
        # provider is inert while the real one stays installed — and shutting
        # down the object we built would flush an orphan and leave the live
        # provider's queue unexported, the exact opposite of the intent.
        # A ProxyTracerProvider (nothing installed) is not an SDK provider and
        # has no shutdown(), so leave the global None in that case.
        _installed = trace.get_tracer_provider()
        tracer_provider = _installed if isinstance(_installed, TracerProvider) else None

    # ── Metrics ───────────────────────────────────────────────────────────────
    try:
        _grpc_endpoint = _OTLP_ENDPOINT.replace("https://", "").replace("http://", "").rstrip("/")
        _provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=_grpc_endpoint, insecure=True),
                    export_interval_millis=60_000,
                )
            ],
        )
        metrics.set_meter_provider(_provider)
        otel_log.info("[otel-metrics] MeterProvider ready → %s", _grpc_endpoint)
    except Exception:
        otel_log.exception("[otel-metrics] FAILED to initialise — metrics disabled")
        try:
            metrics.set_meter_provider(MeterProvider(resource=resource))
        except Exception:
            otel_log.exception("[otel-metrics] fallback provider also FAILED")
    finally:
        # Same first-wins hazard as the tracer provider above — see that comment.
        _installed = metrics.get_meter_provider()
        meter_provider = _installed if isinstance(_installed, MeterProvider) else None

    # Registered here rather than at import scope so it only ever runs in a
    # process that actually owns providers.
    atexit.register(_shutdown_telemetry)


def install_instrumentation() -> None:
    """Install all OTel instrumentors plus the FileCache tracing patch.

    Must run before make_wsgi_app() — mapproxy constructs its cache
    backends/connections there, and anything patched afterward is not
    instrumented. See the ordering-contract comment at app.py's call site.
    """
    instrumentation.install()

    # ── Logging correlation ───────────────────────────────────────────────────
    # Instruments the root logger to inject otelTraceID / otelSpanID attributes
    # into every LogRecord so _logging._OtelFormatter's format string can
    # reference them. set_logging_format is intentionally omitted (defaults to
    # False via the env var OTEL_PYTHON_LOG_CORRELATION=false set in the
    # Dockerfile) so that LoggingInstrumentor does NOT call
    # logging.basicConfig() internally — the format and handlers are already
    # configured by telemetry._logging and must not be overwritten.
    LoggingInstrumentor().instrument()

    filecache_tracing.install()


_init_when_safe_called = False


def init_when_safe() -> None:
    """Dispatch _init_telemetry() to the right moment post-fork.

    Everything install_instrumentation() does is fork-safe: instrumentors hold
    ProxyTracers, the FileCache patch is plain attribute assignment, and
    make_wsgi_app() builds an object graph that copies cleanly.  The providers
    are not fork-safe, so where _init_telemetry() runs depends on which
    process imported the module that calls this:

      lazy-app = false → imported by the master, pre-fork.  Defer to the
                         postfork hook so each worker builds its own export
                         threads.
      lazy-app = true  → imported by the worker, i.e. after the fork already
                         happened.  A postfork hook would register too late
                         and never fire, so initialise immediately instead.
      no uWSGI         → dev server or docker-compose.  Initialise immediately.

    The worker_id() check is what makes this correct under either lazy-app
    setting rather than only the one currently configured.

    uwsgidecorators is imported only on the branch that actually needs it.  It
    does more than expose decorators at import time: it raises a bare
    Exception (NOT an ImportError) when the master process is disabled, so
    importing it up front would take down every uWSGI run without
    `master = true` — and `need-app = true` turns that into a refusal to boot.
    Hence the narrow placement and the broad except.

    Must run last — moving it earlier is untested territory, not a validated
    equivalent. Guarded by a module-level flag: a second call is a no-op
    (first-wins provider semantics already make a repeat _init_telemetry()
    call harmless — this guard is for a clear log line, not correctness).
    """
    global _init_when_safe_called
    if _init_when_safe_called:
        otel_log.info("[otel] init_when_safe() called again — ignoring (telemetry already dispatched)")
        return
    _init_when_safe_called = True

    try:
        import uwsgi
    except ImportError:
        otel_log.info("[otel] not running under uWSGI — initialising telemetry eagerly")
        _init_telemetry()
    else:
        _worker_id = uwsgi.worker_id()
        if _worker_id > 0:
            otel_log.info("[otel] imported inside worker %s (lazy-app=true) — "
                           "fork already happened, initialising telemetry now", _worker_id)
            _init_telemetry()
        else:
            try:
                from uwsgidecorators import postfork
            except Exception:
                # No master process, so there is no fork to hook and nothing will
                # register the providers later.  Initialise now rather than boot a
                # worker with telemetry silently absent.
                otel_log.warning("[otel] uwsgidecorators unavailable (uWSGI master "
                                  "disabled?) — initialising telemetry eagerly")
                _init_telemetry()
            else:
                postfork(_init_telemetry)
                otel_log.info("[otel] imported in uWSGI master (lazy-app=false) — "
                               "telemetry deferred to postfork hook")
