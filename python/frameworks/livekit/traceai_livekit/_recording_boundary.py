"""Recording child lifetime and in-process export policy, not application guards.

Only an ordinary OTel SDK tracer, optionally wrapped once by FITracer, is
supported. The supplied tracer/provider and their delegates are never mutated.
"""

from copy import copy
from typing import Any, Sequence, Tuple

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, Tracer as SDKTracer
from opentelemetry.sdk.util.instrumentation import InstrumentationScope


def _mask_policies(key: str, value: Any, policies: Sequence[TraceConfig]) -> Any:
    for policy in policies:
        value = policy.mask(key, value)
        if value is None:
            break
    return value


class _PremaskedPolicy(TraceConfig):
    """Helper inputs already passed their gates; application additions are not ours."""

    def mask(self, key: str, value: Any) -> Any:
        return value


class _RecordingScope(InstrumentationScope):
    """Carry only policies, never response values, outside telemetry attributes.

    ReadableSpan and LiveKit redaction copies retain the scope, including when a
    batch processor exports later. OTLP encodes only the ordinary scope fields.
    No global registry, context key, baggage, serialized policy or writer identity
    is needed. The tuple retains the caller's policies until the span is released.
    """

    def __init__(self, scope: InstrumentationScope, policies: Sequence[TraceConfig]):
        super().__init__(name=scope.name, version=scope.version,
                         schema_url=scope.schema_url, attributes=scope.attributes)
        self.recording_policies = tuple(policies)


def _copy_span(span: ReadableSpan, **changes: Any) -> ReadableSpan:
    fields = {key: getattr(span, key) for key in (
        "name", "context", "parent", "resource", "attributes", "events", "links",
        "kind", "status", "start_time", "end_time", "instrumentation_scope",
    )}
    fields.update(changes)
    return ReadableSpan(**fields)


class _RecordingLifecycle(SpanProcessor):
    def __init__(self, delegate: SpanProcessor, policies: Sequence[TraceConfig]):
        self._delegate = delegate
        self._policies = tuple(policies)
        self.started_span = None

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        # Retain the allocated child before arbitrary application callbacks run.
        # Even if start_span never returns, record_egress can end it in finally.
        self.started_span = span
        self._delegate.on_start(span, parent_context=parent_context)

    def _on_ending(self, span: Any) -> None:
        callback = getattr(self._delegate, "_on_ending", None)
        if callback is not None:
            callback(span)

    def on_end(self, span: ReadableSpan) -> None:
        scope = _RecordingScope(span.instrumentation_scope, self._policies)
        self._delegate.on_end(_copy_span(span, instrumentation_scope=scope))

    def shutdown(self) -> None:
        # This per-call adapter does not own the provider lifecycle.
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._delegate.force_flush(timeout_millis=timeout_millis)


def _protect_recording_tracer(
    selected: FITracer, policies: Sequence[TraceConfig],
) -> Tuple[FITracer, _RecordingLifecycle]:
    # Guarded compatibility seam, not a sandbox for application code.
    if type(selected) is not FITracer:
        raise TypeError("recording tracer configuration is unsupported")
    sdk = selected.__wrapped__
    if type(sdk) is not SDKTracer or not isinstance(sdk.span_processor, SpanProcessor):
        raise TypeError("recording SDK lifecycle is unsupported")
    local = copy(sdk)
    if local is sdk or type(local) is not SDKTracer:
        raise TypeError("recording SDK tracer isolation is unavailable")
    lifetime = _RecordingLifecycle(sdk.span_processor, policies)
    local.span_processor = lifetime
    # No sampler, setter, event, link or name guard: the application is the
    # principal. FI still owns construction/id generation and selected-kind
    # retention; its local pass-through mask avoids re-masking helper inputs.
    return FITracer(local, config=_PremaskedPolicy()), lifetime
