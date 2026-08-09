"""
FileCache monkey-patch for filesystem tile tracing.

MapProxy reads tiles directly via open() — no network library to instrument.
Wrapping FileCache.load_tile / load_tiles / store_tile gives a span for every
filesystem tile operation, including the layer name, tile coords, directory,
cache hit/miss and byte size.
Disable with TELEMETRY_TILE_CACHE_ENABLED=false.
"""
import os

from opentelemetry import trace

from telemetry._logging import otel_log

_TILE_CACHE_TRACING = os.getenv("TELEMETRY_TILE_CACHE_ENABLED", "true").lower() == "true"


def install() -> None:
    if _TILE_CACHE_TRACING:
        try:
            from mapproxy.cache.file import FileCache as _FileCache
            # Runs when install_instrumentation() is called, i.e. possibly
            # pre-fork, so this is a ProxyTracer that resolves once
            # _init_telemetry() sets the real provider in the worker.
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
            otel_log.info("[otel-filecache] FileCache monkey-patched (load_tile, load_tiles, store_tile)")
        except Exception:
            otel_log.exception("[otel-filecache] FileCache tracing FAILED to initialise")
    else:
        otel_log.info("[otel-filecache] FileCache tracing disabled (TELEMETRY_TILE_CACHE_ENABLED=false)")
