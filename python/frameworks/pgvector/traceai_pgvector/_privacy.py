"""A scoped provider adapter: filter driver telemetry before SDK processors.

Filtering at this boundary (rather than in an exporter or on_end processor)
keeps SQL, connection details and exception messages away from samplers,
processors and every exporter attached to the application's provider.
"""

from contextlib import contextmanager

from opentelemetry import trace
from wrapt import ObjectProxy

from traceai_pgvector._wrappers import _QUERY_ATTRIBUTES


class _Span(ObjectProxy):
    def set_attribute(self, key, value):
        if key in ("db.system", "db.system.name"):
            self.__wrapped__.set_attribute(key, value)

    def set_attributes(self, attributes):
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def set_status(self, status, description=None):
        code = status.status_code if isinstance(status, trace.Status) else status
        self.__wrapped__.set_status(code)

    def record_exception(self, *args, **kwargs):
        pass

    def add_event(self, *args, **kwargs):
        pass

    def update_name(self, name):
        pass


class _Tracer(ObjectProxy):
    def __init__(self, tracer, enabled):
        super().__init__(tracer)
        self._self_enabled = enabled

    def start_span(
        self,
        name,
        context=None,
        kind=trace.SpanKind.INTERNAL,
        attributes=None,
        links=None,
        start_time=None,
        record_exception=True,
        set_status_on_exception=True,
    ):
        if not self._self_enabled():
            return trace.INVALID_SPAN
        metadata = _QUERY_ATTRIBUTES.get() or {}
        safe_attributes = {"db.system": "postgresql", **metadata}
        operation = metadata.get("db.operation.name", "query")
        span = self.__wrapped__.start_span(
            "postgresql " + operation,
            context=context,
            kind=kind,
            attributes=safe_attributes,
            start_time=start_time,
            record_exception=False,
            set_status_on_exception=False,
        )
        return _Span(span)

    @contextmanager
    def start_as_current_span(
        self,
        name,
        context=None,
        kind=trace.SpanKind.INTERNAL,
        attributes=None,
        links=None,
        start_time=None,
        record_exception=True,
        set_status_on_exception=True,
        end_on_exit=True,
    ):
        # After uninstrument, even cursors from existing connections stop tracing.
        if not self._self_enabled():
            yield trace.INVALID_SPAN
            return
        span = self.start_span(name, context=context, kind=kind, start_time=start_time)
        with trace.use_span(
            span,
            end_on_exit=end_on_exit,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                yield span
            except BaseException:
                span.set_status(trace.StatusCode.ERROR)
                raise


class DriverTracerProvider(ObjectProxy):
    def __init__(self, provider: trace.TracerProvider) -> None:
        super().__init__(provider)
        self._self_enabled = True

    def is_enabled(self) -> bool:
        return self._self_enabled

    def disable(self) -> None:
        self._self_enabled = False

    def get_tracer(self, *args, **kwargs) -> trace.Tracer:
        return _Tracer(self.__wrapped__.get_tracer(*args, **kwargs), self.is_enabled)
