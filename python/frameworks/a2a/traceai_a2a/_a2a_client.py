"""
A2A Protocol client-side instrumentation.

Wraps A2AClient.send_task() and A2AClient.send_task_streaming() to:
1. Start an A2A_CLIENT span
2. Inject W3C TraceContext (traceparent/tracestate) into outbound HTTP headers
3. Record task/message/artifact attributes on the span
4. Propagate span context so the remote agent's trace is linked

This is the key mechanism that "stitches" distributed multi-agent traces
into a single unified view in your observability backend.

Span lifecycle for streaming calls
-----------------------------------
The reviewer correctly noted that handing span ownership to a bare generator
is unsafe: if the caller does ``for ev in stream: if done: break`` the
generator's ``finally`` block never fires and the span leaks.

Fix: streaming results are wrapped in ``_StreamingSpanWrapper`` /
``_AsyncStreamingSpanWrapper`` — real objects that implement
``__iter__``/``close()`` (and ``__aiter__``/``aclose()``).  The span is
ended inside ``close()``/``aclose()``, and a ``__del__`` guard provides a
last-resort finalizer in case the caller drops the reference without
iterating.
"""

import logging
from contextlib import contextmanager
from typing import Any, AsyncIterator, Dict, Iterator, Mapping, Optional, Tuple

from opentelemetry import context as context_api
from opentelemetry import propagate
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import INVALID_SPAN, Span, Status, StatusCode, Tracer
from opentelemetry.util.types import AttributeValue

from traceai_a2a._attributes import (
    get_agent_card_attributes,
    get_artifact_type,
    get_send_task_payload_attributes,
    get_task_attributes,
)
from traceai_a2a._semantic_conventions import (
    A2A_AGENT_URL,
    A2A_ARTIFACT_TYPE,
    A2A_PROPAGATED_TRACE_ID,
    A2A_SPAN_KIND_CLIENT,
    A2A_TASK_ID,
    A2A_TASK_STATE,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
)

try:
    from fi_instrumentation import get_attributes_from_context
    from fi_instrumentation.fi_types import SpanAttributes
    _FI_AVAILABLE = True
except ImportError:
    _FI_AVAILABLE = False

logger = logging.getLogger(__name__)


class _A2ACarrier(dict):
    """
    A simple dict-based carrier for OTel context propagation.
    Passed to propagate.inject() then merged into the A2A HTTP call headers.
    """
    pass


def _inject_trace_context() -> Tuple[Dict[str, str], Optional[str]]:
    """
    Inject W3C TraceContext into a carrier dict.
    Returns (headers_dict, trace_id_hex_string).

    The headers dict should be merged into the A2A client's HTTP request headers.
    The trace_id is recorded as a span attribute (gen_ai.a2a.propagated_trace_id)
    so you can easily look up the stitched trace by ID.
    """
    carrier = _A2ACarrier()
    propagate.inject(carrier)

    trace_id: Optional[str] = None
    traceparent = carrier.get("traceparent", "")
    if traceparent:
        # traceparent format: 00-<trace-id>-<span-id>-<flags>
        parts = traceparent.split("-")
        if len(parts) == 4:
            trace_id = parts[1]

    return dict(carrier), trace_id


@contextmanager
def _start_a2a_client_span(
    tracer: Tracer,
    agent_url: str,
    span_name: str = "A2AClient.send_task",
    extra_attributes: Optional[Mapping[str, AttributeValue]] = None,
) -> Iterator[Span]:
    """
    Context manager that starts an A2A_CLIENT span.
    Sets the agent URL and span kind attributes immediately on start.
    """
    attributes: Dict[str, AttributeValue] = {
        SpanAttributes.GEN_AI_SPAN_KIND: A2A_SPAN_KIND_CLIENT,
        A2A_AGENT_URL: agent_url,
    }

    if extra_attributes:
        attributes.update(extra_attributes)

    # Inherit any context attributes from the current FI context
    if _FI_AVAILABLE:
        try:
            ctx_attrs = dict(get_attributes_from_context())
            attributes.update(ctx_attrs)
        except Exception:
            pass

    try:
        span = tracer.start_span(name=span_name, attributes=attributes)
    except Exception:
        logger.exception("Failed to start A2A client span")
        span = INVALID_SPAN

    with trace_api.use_span(
        span,
        end_on_exit=False,
        record_exception=False,
        set_status_on_exception=False,
    ) as current_span:
        yield current_span


# ---------------------------------------------------------------------------
# Streaming span wrapper objects
# ---------------------------------------------------------------------------

class _StreamingSpanWrapper:
    """
    Wraps a synchronous SSE iterator and guarantees ``span.end()`` is called
    regardless of *how* the caller exits — normal exhaustion, ``break``, or
    garbage collection.

    Implements the full iterator protocol plus ``close()`` so callers that
    call ``generator.close()`` explicitly are handled correctly too.
    """

    def __init__(self, result: Iterator[Any], span: Span) -> None:
        self._result = result
        self._span = span
        self._last_artifact_type: Optional[str] = None
        self._last_task_state: Optional[str] = None
        self._span_ended = False

    # --- iterator protocol ---------------------------------------------------

    def __iter__(self) -> "_StreamingSpanWrapper":
        return self

    def __next__(self) -> Any:
        try:
            event = next(self._result)
        except StopIteration:
            self._finalize_span()
            raise
        except Exception as exc:
            self._span.record_exception(exc)
            self._span.set_status(
                Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}")
            )
            self._finalize_span(error=True)
            raise

        # Capture artifact type from streaming events
        artifact = getattr(event, "artifact", None)
        if artifact is not None:
            atype = get_artifact_type(artifact)
            if atype:
                self._last_artifact_type = atype

        # Capture task state from status events
        task_state = _extract_event_state(event)
        if task_state:
            self._last_task_state = task_state

        return event

    # --- explicit close (e.g. caller does ``for e in s: break``) ------------

    def close(self) -> None:
        try:
            if hasattr(self._result, "close"):
                self._result.close()
        finally:
            self._finalize_span()

    # --- last-resort gc finalizer -------------------------------------------

    def __del__(self) -> None:
        self._finalize_span()

    # --- internal ------------------------------------------------------------

    def _finalize_span(self, error: bool = False) -> None:
        if self._span_ended:
            return
        self._span_ended = True
        if self._last_artifact_type:
            self._span.set_attribute(A2A_ARTIFACT_TYPE, self._last_artifact_type)
        if self._last_task_state:
            self._span.set_attribute(A2A_TASK_STATE, self._last_task_state)
            if not error:
                if self._last_task_state == TASK_STATE_COMPLETED:
                    self._span.set_status(Status(StatusCode.OK))
                elif self._last_task_state == TASK_STATE_FAILED:
                    self._span.set_status(Status(StatusCode.ERROR, "A2A task failed"))
        elif not error:
            self._span.set_status(Status(StatusCode.OK))
        self._span.end()


class _AsyncStreamingSpanWrapper:
    """
    Wraps an async SSE iterator and guarantees ``span.end()`` is called
    regardless of how the caller exits — normal exhaustion, ``break``,
    ``aclose()``, or garbage collection.

    Implements ``__aiter__``, ``__anext__``, and ``aclose()`` so it is a
    proper async generator replacement.
    """

    def __init__(self, result: AsyncIterator[Any], span: Span) -> None:
        self._result = result
        self._span = span
        self._last_artifact_type: Optional[str] = None
        self._last_task_state: Optional[str] = None
        self._span_ended = False

    # --- async iterator protocol --------------------------------------------

    def __aiter__(self) -> "_AsyncStreamingSpanWrapper":
        return self

    async def __anext__(self) -> Any:
        try:
            event = await self._result.__anext__()
        except StopAsyncIteration:
            self._finalize_span()
            raise
        except Exception as exc:
            self._span.record_exception(exc)
            self._span.set_status(
                Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}")
            )
            self._finalize_span(error=True)
            raise

        artifact = getattr(event, "artifact", None)
        if artifact is not None:
            atype = get_artifact_type(artifact)
            if atype:
                self._last_artifact_type = atype

        task_state = _extract_event_state(event)
        if task_state:
            self._last_task_state = task_state

        return event

    # --- explicit aclose (e.g. ``async for e in s: break``) ----------------

    async def aclose(self) -> None:
        try:
            if hasattr(self._result, "aclose"):
                await self._result.aclose()
        finally:
            self._finalize_span()

    # --- last-resort gc finalizer -------------------------------------------

    def __del__(self) -> None:
        self._finalize_span()

    # --- internal ------------------------------------------------------------

    def _finalize_span(self, error: bool = False) -> None:
        if self._span_ended:
            return
        self._span_ended = True
        if self._last_artifact_type:
            self._span.set_attribute(A2A_ARTIFACT_TYPE, self._last_artifact_type)
        if self._last_task_state:
            self._span.set_attribute(A2A_TASK_STATE, self._last_task_state)
            if not error:
                if self._last_task_state == TASK_STATE_COMPLETED:
                    self._span.set_status(Status(StatusCode.OK))
                elif self._last_task_state == TASK_STATE_FAILED:
                    self._span.set_status(Status(StatusCode.ERROR, "A2A task failed"))
        elif not error:
            self._span.set_status(Status(StatusCode.OK))
        self._span.end()


# ---------------------------------------------------------------------------
# Shared helper (module-level so wrapper objects can call it)
# ---------------------------------------------------------------------------

def _extract_event_state(event: Any) -> Optional[str]:
    """Try to extract task state from a streaming event object."""
    try:
        status = getattr(event, "status", None)
        if status:
            state = getattr(status, "state", None)
            if state:
                return str(state.value if hasattr(state, "value") else state)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main wrapper
# ---------------------------------------------------------------------------

class A2AClientWrapper:
    """
    Wraps the A2A Python SDK's A2AClient to add OpenTelemetry instrumentation.

    This is installed via wrapt into A2AClient.send_task and
    A2AClient.send_task_streaming by A2AInstrumentor._instrument().
    """

    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer

    # ------------------------------------------------------------------
    # Sync wrapper for send_task
    # ------------------------------------------------------------------

    def __call__(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        agent_url = self._get_agent_url(instance)
        payload = self._extract_payload(args, kwargs)
        is_streaming = getattr(wrapped, "_a2a_streaming", False)

        payload_attrs = dict(get_send_task_payload_attributes(payload, streaming=is_streaming))

        with _start_a2a_client_span(
            tracer=self._tracer,
            agent_url=agent_url,
            span_name="A2AClient.send_task_streaming" if is_streaming else "A2AClient.send_task",
            extra_attributes=payload_attrs,
        ) as span:
            # Inject distributed trace context into outbound headers
            propagated_headers, trace_id = _inject_trace_context()
            if trace_id:
                span.set_attribute(A2A_PROPAGATED_TRACE_ID, trace_id)

            # Merge propagation headers into the call kwargs
            kwargs = self._inject_headers(kwargs, propagated_headers)

            try:
                result = wrapped(*args, **kwargs)

                if is_streaming:
                    # Return a wrapper object — span.end() is guaranteed via
                    # close() or __del__, even if caller breaks early.
                    return _StreamingSpanWrapper(result, span)

                # Non-streaming: extract task attributes from the returned Task
                self._finalize_span_from_task(span, result)
                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as exc:
                span.record_exception(exc)
                span.set_status(
                    Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}")
                )
                raise
            finally:
                if not is_streaming:
                    span.end()

    # ------------------------------------------------------------------
    # Async wrapper for send_task (async version)
    # ------------------------------------------------------------------

    async def __call_async__(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)

        agent_url = self._get_agent_url(instance)
        payload = self._extract_payload(args, kwargs)
        is_streaming = getattr(wrapped, "_a2a_streaming", False)

        payload_attrs = dict(get_send_task_payload_attributes(payload, streaming=is_streaming))

        with _start_a2a_client_span(
            tracer=self._tracer,
            agent_url=agent_url,
            span_name="A2AClient.send_task_streaming" if is_streaming else "A2AClient.send_task",
            extra_attributes=payload_attrs,
        ) as span:
            propagated_headers, trace_id = _inject_trace_context()
            if trace_id:
                span.set_attribute(A2A_PROPAGATED_TRACE_ID, trace_id)

            kwargs = self._inject_headers(kwargs, propagated_headers)

            try:
                import asyncio
                if asyncio.iscoroutinefunction(wrapped):
                    result = await wrapped(*args, **kwargs)
                else:
                    # async generator — call without await, returns async_generator object
                    result = wrapped(*args, **kwargs)

                if is_streaming:
                    # Return a wrapper object — span.end() is guaranteed via
                    # aclose() or __del__, even if caller breaks early.
                    return _AsyncStreamingSpanWrapper(result, span)

                self._finalize_span_from_task(span, result)
                span.set_status(Status(StatusCode.OK))
                return result

            except Exception as exc:
                span.record_exception(exc)
                span.set_status(
                    Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}")
                )
                raise
            finally:
                if not is_streaming:
                    span.end()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_agent_url(self, instance: Any) -> str:
        """Extract the agent base URL from the A2AClient instance."""
        try:
            url = (
                getattr(instance, "url", None)
                or getattr(instance, "base_url", None)
                or getattr(instance, "_url", None)
                or "unknown"
            )
            return str(url)
        except Exception:
            return "unknown"

    def _extract_payload(
        self, args: Tuple[Any, ...], kwargs: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract the task payload dict from call arguments."""
        try:
            # A2AClient.send_task(payload={...}) or send_task({...})
            if kwargs.get("payload"):
                return dict(kwargs["payload"])
            if args and isinstance(args[0], dict):
                return dict(args[0])
        except Exception:
            logger.debug("Failed to extract A2A payload", exc_info=True)
        return {}

    def _inject_headers(
        self, kwargs: Mapping[str, Any], extra_headers: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Merge W3C propagation headers into the kwargs passed to A2AClient.

        A2AClient accepts headers via kwargs["headers"] (passed through to httpx).
        We merge without overwriting any existing headers the caller may have set.
        """
        kwargs = dict(kwargs)
        existing = dict(kwargs.get("headers") or {})
        existing.update(extra_headers)
        kwargs["headers"] = existing
        return kwargs

    def _finalize_span_from_task(self, span: Span, task: Any) -> None:
        """Set task ID and final state on the span from the returned Task object."""
        try:
            for key, value in get_task_attributes(task):
                span.set_attribute(key, value)
        except Exception:
            logger.debug("Failed to finalize span from task", exc_info=True)
