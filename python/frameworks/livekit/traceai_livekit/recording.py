"""Explicitly session-bound, opt-in egress response tracing.

The proxy is a structural async-service adapter, not a LiveKit monkeypatch. It
never inspects requests, catches delegate failures, polls, or reads media bytes.
"""

import inspect
import logging
from typing import Any, Optional, Tuple

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, Tracer

from ._recording_attributes import (
    _OPERATIONS,
    _operation,
    _recording_span_attributes,
)
from ._recording_boundary import _mask_policies, _protect_recording_tracer

logger = logging.getLogger(__name__)


def _diagnostic(exc: Exception) -> None:
    # Never pass exc_info, str(exc), response fields, requests or URLs to logging.
    logger.debug("traceai-livekit recording capture skipped (%s)", type(exc).__name__)


def _resolve_session_context(session_span: Optional[Span]) -> Optional[SpanContext]:
    parent = session_span if session_span is not None else trace.get_current_span()
    selected = parent.get_span_context()
    return selected if isinstance(selected, SpanContext) and selected.is_valid else None


def _get_recording_tracer(
    tracer: Optional[Tracer], trace_config: Optional[TraceConfig],
) -> Tuple[Any, Tuple[TraceConfig, ...]]:
    config = trace_config if trace_config is not None else TraceConfig()
    if not isinstance(config, TraceConfig):
        raise TypeError("recording privacy configuration is unsupported")
    selected = tracer if tracer is not None else trace.get_tracer("traceai_livekit.recording")
    if isinstance(selected, FITracer):
        # Keep the supplied tracer intact. Its policy must also govern destination
        # aliases and OUTPUT_VALUE, not merely the pii-qualified source setters.
        # This is a guarded compatibility seam: fail closed if FI changes it.
        existing = getattr(selected, "_self_config", None)
        if not isinstance(existing, TraceConfig):
            raise TypeError("recording tracer privacy configuration is unavailable")
        policies = (config,) if existing is config else (config, existing)
        return selected, policies
    return FITracer(selected, config=config), (config,)


def record_egress(
    egress_info: Any, *, session_span: Optional[Span] = None,
    room_name: Optional[str] = None, conversation_id: Optional[str] = None,
    operation: str = "snapshot", tracer: Optional[Tracer] = None,
    trace_config: Optional[TraceConfig] = None,
) -> bool:
    """End one recording snapshot beneath a valid session span/context.

    ``egress_info`` is an attribute-based response with a bounded nonempty
    ``egress_id``; optional ``started_at``/``ended_at`` are Unix nanoseconds.
    Supply an ended span or NonRecordingSpan for a late response. Without an
    explicit span, only the current valid OTel span is used (no ancestor search).

    True means locally recorded and ended, not exported, playable or complete.
    False includes missing context/id, room mismatch, suppression, sampling and
    instrumentation failure. No API call, media IO or root-span mutation occurs.

    The recording helper controls only the metadata it writes. Attributes, events or links that your own samplers, span processors or code add are exported as written; the recording policy does not filter them, except the helper's reserved keys, which export always re-checks.
    """
    try:
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return False
        parent = _resolve_session_context(session_span)
        if parent is None:
            return False
        recording_tracer, policies = _get_recording_tracer(tracer, trace_config)
        selected_operation = _operation(operation)
        attributes = _recording_span_attributes(
            egress_info, operation=selected_operation, room_name=room_name,
            conversation_id=conversation_id, policies=policies,
        )
        if attributes is None:
            logger.debug("traceai-livekit recording capture skipped (metadata unavailable)")
            return False
        kind = _mask_policies(SpanAttributes.GEN_AI_SPAN_KIND, "CHAIN", policies)
        if type(kind) is str:
            attributes[SpanAttributes.GEN_AI_SPAN_KIND] = kind
        recording_tracer, lifetime = _protect_recording_tracer(recording_tracer, policies)
        # FITracer reads ambient FI attributes independently of its context arg.
        # A late callback must not inherit another session's metadata or bypass
        # the helper policy through a weaker supplied tracer. Propagate only the
        # selected parent (including its trace state), not ambient FI attributes
        # or baggage. Suppression was checked before isolating the context.
        parent_context = trace.set_span_in_context(NonRecordingSpan(parent), context.Context())
        token = context.attach(parent_context)
        span = None
        interrupted = False
        try:
            span = recording_tracer.start_span(
                "livekit.recording." + selected_operation,
                context=parent_context,
                kind=trace.SpanKind.INTERNAL, attributes=attributes,
            )
            return bool(span.is_recording())
        except BaseException as exc:
            # Only an interruption pending in this call counts, not one the
            # caller may already be handling. Bare raise retains its traceback.
            interrupted = not isinstance(exc, Exception)
            raise
        finally:
            try:
                if span is not None:
                    span.end()
                elif lifetime.started_span is not None:
                    # on_start can raise after allocation, before FI returns a
                    # handle. Never leave that recording child open.
                    lifetime.started_span.end()
            except Exception:
                if not interrupted:
                    raise
                # Do not replace a pending interrupt with a cleanup failure
                # (including one from a diagnostic/logging callback).
            except BaseException:
                # A new cleanup interruption must also propagate, even when
                # context restoration subsequently raises an ordinary error.
                interrupted = True
                raise
            finally:
                # on_end must also see only the selected parent, not ambient
                # session metadata/baggage restored from an unrelated callback.
                try:
                    context.detach(token)
                except Exception:
                    if not interrupted:
                        raise
    except Exception as exc:
        _diagnostic(exc)
        return False


class _EgressClientProxy:
    """Per-service binding; retains context, not a session object or response history."""

    def __init__(
        self, client: Any, *, parent: Optional[SpanContext], room_name: Optional[str],
        conversation_id: Optional[str], tracer: Optional[Tracer],
        trace_config: Optional[TraceConfig],
    ) -> None:
        self._client = client
        self._parent = NonRecordingSpan(parent) if parent is not None else trace.INVALID_SPAN
        self._room_name = room_name
        self._conversation_id = conversation_id
        self._tracer = tracer
        self._config = trace_config
        self._methods: dict = {}

    def __getattr__(self, name: str) -> Any:
        # Resolve the delegate first: removed/replaced attributes keep their normal
        # semantics, even when a wrapper was previously cached for this method.
        method = getattr(self._client, name)
        if name not in _OPERATIONS or not inspect.iscoroutinefunction(method):
            return method
        cached = self._methods.get(name)
        if cached is not None and cached[0] == method:
            return cached[1]

        async def capture(*args: Any, **kwargs: Any) -> Any:
            response = await method(*args, **kwargs)
            # No try/except around the await: exception/cancellation identity and
            # traceback propagate naturally, without retry or exception telemetry.
            record_egress(
                response, session_span=self._parent, room_name=self._room_name,
                conversation_id=self._conversation_id, operation=name,
                tracer=self._tracer, trace_config=self._config,
            )
            return response

        self._methods[name] = (method, capture)
        return capture


def wrap_egress_client(
    client: Any, *, session_span: Optional[Span] = None,
    room_name: Optional[str] = None, conversation_id: Optional[str] = None,
    tracer: Optional[Tracer] = None, trace_config: Optional[TraceConfig] = None,
) -> Any:
    """Bind an async egress service once; capture successful supported responses.

    The eight candidate start/stop/update method names are accepted only when the
    attribute is recognized as a coroutine function. Arguments and return objects
    are unchanged. Unsupported decorators/returns need manual ``record_egress``.
    This structural contract is not an SDK-version compatibility assertion.

    Rewrapping with omitted or identical options is idempotent; any conflicting
    supplied option raises ValueError. No binding is mutated. The caller owns the
    original API/client shutdown; Python context-manager protocols are not proxied.
    """
    if isinstance(client, _EgressClientProxy):
        if (
            (session_span is not None and _resolve_session_context(session_span) != (
                client._parent.get_span_context() if client._parent.get_span_context().is_valid else None
            ))
            or (room_name is not None and room_name != client._room_name)
            or (conversation_id is not None and conversation_id != client._conversation_id)
            or (tracer is not None and tracer is not client._tracer)
            or (trace_config is not None and trace_config is not client._config)
        ):
            raise ValueError("conflicting egress tracing binding")
        return client
    try:
        parent = _resolve_session_context(session_span)
        # Freeze the effective default policy and tracer selection at factory time.
        config = trace_config if trace_config is not None else TraceConfig()
        selected = tracer if tracer is not None else trace.get_tracer("traceai_livekit.recording")
    except Exception as exc:
        _diagnostic(exc)
        parent, config, selected = None, None, tracer
    return _EgressClientProxy(
        client, parent=parent, room_name=room_name, conversation_id=conversation_id,
        tracer=selected, trace_config=config,
    )
