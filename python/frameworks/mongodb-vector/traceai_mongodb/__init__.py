"""MongoDB command tracing with metadata-only Atlas Vector Search enrichment."""

import logging
from typing import Any, Collection

from opentelemetry import trace as trace_api
# Upstream marks the entire instrumentor module with a mypy ignore.
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore[attr-defined]
from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
from opentelemetry.trace import Status, StatusCode

from traceai_mongodb._attributes import request_hook, response_hook, failed_hook
from traceai_mongodb.package import _instruments
from traceai_mongodb.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class MongoDBInstrumentor(BaseInstrumentor):
    """Delegate command lifetimes to OpenTelemetry's maintained PyMongo listener.

    Instrument before constructing sync or async clients. No Collection methods
    are patched and no cursors, pipelines or document iterables are consumed.
    Query payloads and server error messages are not recorded.
    """

    _owns_native = False

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        native = PymongoInstrumentor()
        if native.is_instrumented_by_opentelemetry:
            self._owns_native = False
            logger.warning(
                "PyMongo tracing is already enabled; retaining its configuration "
                "without traceAI vector enrichment. Enable only one instrumentor."
            )
            return

        provider = kwargs.get("tracer_provider")
        native.instrument(
            tracer_provider=provider,
            request_hook=request_hook,
            response_hook=response_hook,
            failed_hook=failed_hook,
            capture_statement=False,
        )
        # PyMongo has no unregister API. Upstream reuses its listener on enable,
        # but ignores new providers/hooks. Rebind that SAME listener so existing
        # clients work after reconfiguration without registering duplicate spans.
        # These upstream lifecycle internals are covered at our dependency floor
        # and current version; all command tracking remains upstream-owned.
        listener = native._commandtracer_instance
        if listener is None or not native.is_instrumented_by_opentelemetry:
            raise RuntimeError("OpenTelemetry PyMongo instrumentation could not be enabled")
        listener._tracer = trace_api.get_tracer(__name__, __version__, provider)
        listener.start_hook = request_hook
        listener.success_hook = response_hook
        listener.failed_hook = failed_hook
        listener.capture_statement = False
        self._owns_native = True

    def _uninstrument(self, **kwargs: Any) -> None:
        if not self._owns_native:
            return
        native = PymongoInstrumentor()
        native.uninstrument()
        listener = native._commandtracer_instance
        # Disabled upstream listeners ignore completion events. End outstanding
        # commands once rather than retaining spans across a later enable cycle.
        if listener is not None:
            pending = list(listener._span_dict.values())
            listener._span_dict.clear()
            for span in pending:
                span.set_status(Status(StatusCode.ERROR, "Instrumentation disabled before command completed"))
                span.end()
        self._owns_native = False


__all__ = ["MongoDBInstrumentor", "__version__"]
