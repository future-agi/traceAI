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

import inspect
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
    extract_artifact_text,
    get_agent_card_attributes,
    get_artifact_type,
    get_send_message_request_attributes,
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

# API generation tags shared with __init__.py; duplicated here to avoid a
# circular import.
_API_V0 = "v0"
_API_V1 = "v1"

try:
    from fi_instrumentation import get_attributes_from_context
    from fi_instrumentation.fi_types import SpanAttributes

    _FI_AVAILABLE = True
    _OUTPUT_VALUE_KEY = SpanAttributes.OUTPUT_VALUE
except ImportError:
    _FI_AVAILABLE = False
    _OUTPUT_VALUE_KEY = "output.value"

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
        self._output_fragments: list = []
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

        # Capture artifact type AND text content from streaming events
        artifact = _extract_event_artifact(event)
        if artifact is not None:
            atype = get_artifact_type(artifact)
            if atype:
                self._last_artifact_type = atype
            text = extract_artifact_text(artifact)
            if text:
                self._output_fragments.append(text)

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
        if self._output_fragments:
            self._span.set_attribute(
                _OUTPUT_VALUE_KEY, "".join(self._output_fragments)
            )
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
        self._output_fragments: list = []
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

        artifact = _extract_event_artifact(event)
        if artifact is not None:
            atype = get_artifact_type(artifact)
            if atype:
                self._last_artifact_type = atype
            text = extract_artifact_text(artifact)
            if text:
                self._output_fragments.append(text)

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
        if self._output_fragments:
            self._span.set_attribute(
                _OUTPUT_VALUE_KEY, "".join(self._output_fragments)
            )
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
    """Try to extract task state from a streaming event object.

    Handles both the v0 SDK shape (event has a top-level ``.status``) and the
    v1 protobuf ``StreamResponse`` shape (oneof of ``task`` /
    ``status_update`` / ``artifact_update`` / ``message``). On v1 the status
    lives at ``event.status_update.status`` for incremental updates and at
    ``event.task.status`` when the full task is delivered.
    """
    try:
        for status_owner in (
            getattr(event, "status_update", None),
            getattr(event, "task", None),
            event,
        ):
            if status_owner is None:
                continue
            status = getattr(status_owner, "status", None)
            if not status:
                continue
            state = getattr(status, "state", None)
            if state in (None, 0, ""):  # protobuf default = TASK_STATE_UNSPECIFIED
                continue
            return str(state.value if hasattr(state, "value") else state)
    except Exception:
        pass
    return None


def _extract_event_artifact(event: Any) -> Any:
    """Pull an artifact off a streaming event in v0 or v1 shape.

    v0: ``event.artifact``. v1: ``event.artifact_update.artifact`` (and
    ``event.task.artifacts`` carries them in batch on the final task event).
    """
    try:
        direct = getattr(event, "artifact", None)
        if direct is not None:
            return direct
        artifact_update = getattr(event, "artifact_update", None)
        if artifact_update is not None:
            artifact = getattr(artifact_update, "artifact", None)
            if artifact is not None:
                return artifact
        task = getattr(event, "task", None)
        if task is not None:
            artifacts = getattr(task, "artifacts", None) or []
            if len(artifacts) > 0:
                return artifacts[-1]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main wrapper
# ---------------------------------------------------------------------------

class A2AClientWrapper:
    """
    Wraps the A2A Python SDK's client class to add OpenTelemetry instrumentation.

    Two API generations are supported and dispatched based on ``api_version``:

    * **v0** (a2a-sdk < 1.0): ``A2AClient.send_task`` (sync) and
      ``A2AClient.send_task_streaming`` (sync iterator). Payload is a dict;
      W3C trace context is injected into ``kwargs["headers"]`` so it reaches
      the outbound HTTP request.

    * **v1** (a2a-sdk >= 1.0): ``Client.send_message`` — an ``async def`` that
      returns ``AsyncIterator[StreamResponse]`` (streaming is the only mode).
      Payload is a protobuf ``SendMessageRequest`` and the call signature has
      no ``headers`` kwarg, so HTTP-header injection is skipped on v1; the
      W3C trace-id is still recorded on the local span as
      ``gen_ai.a2a.propagated_trace_id``. Future work: plumb context into
      the SDK's ``ClientCallInterceptor`` so it crosses the wire too.
    """

    def __init__(self, tracer: Tracer, api_version: str = _API_V0) -> None:
        self._tracer = tracer
        self._api_version = api_version

    # ------------------------------------------------------------------
    # wrapt entry point. Routes sync vs async based on the wrapped function.
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

        if inspect.iscoroutinefunction(wrapped):
            return self._async_call(wrapped, instance, args, kwargs)
        return self._sync_call(wrapped, instance, args, kwargs)

    # ------------------------------------------------------------------
    # Sync code path — v0 only (`A2AClient.send_task` / `send_task_streaming`).
    # ------------------------------------------------------------------

    def _sync_call(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        is_streaming = getattr(wrapped, "_a2a_streaming", False)
        span_name = self._span_name(is_streaming)
        agent_url = self._get_agent_url(instance)
        payload_attrs = self._payload_attributes(args, kwargs, is_streaming)

        with _start_a2a_client_span(
            tracer=self._tracer,
            agent_url=agent_url,
            span_name=span_name,
            extra_attributes=payload_attrs,
        ) as span:
            propagated_headers, trace_id = _inject_trace_context()
            if trace_id:
                span.set_attribute(A2A_PROPAGATED_TRACE_ID, trace_id)

            if self._api_version == _API_V0:
                kwargs = self._inject_headers(kwargs, propagated_headers)

            try:
                result = wrapped(*args, **kwargs)

                if is_streaming:
                    return _StreamingSpanWrapper(result, span)

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
    # Async code path — primarily v1 (`Client.send_message`) but also covers
    # any v0 install that happens to expose an async send_task.
    # ------------------------------------------------------------------

    async def _async_call(
        self,
        wrapped: Any,
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        is_streaming = getattr(wrapped, "_a2a_streaming", False)
        span_name = self._span_name(is_streaming)
        agent_url = self._get_agent_url(instance)
        payload_attrs = self._payload_attributes(args, kwargs, is_streaming)

        with _start_a2a_client_span(
            tracer=self._tracer,
            agent_url=agent_url,
            span_name=span_name,
            extra_attributes=payload_attrs,
        ) as span:
            propagated_headers, trace_id = _inject_trace_context()
            if trace_id:
                span.set_attribute(A2A_PROPAGATED_TRACE_ID, trace_id)

            if self._api_version == _API_V0:
                kwargs = self._inject_headers(kwargs, propagated_headers)

            try:
                result = await wrapped(*args, **kwargs)

                if is_streaming:
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

    def _span_name(self, is_streaming: bool) -> str:
        if self._api_version == _API_V1:
            return "Client.send_message"
        return (
            "A2AClient.send_task_streaming" if is_streaming else "A2AClient.send_task"
        )

    def _payload_attributes(
        self,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
        is_streaming: bool,
    ) -> Dict[str, AttributeValue]:
        """Build pre-call payload attributes for the span.

        Dispatches on api_version so v1 (protobuf SendMessageRequest) and
        v0 (dict payload) each use their own extractor.
        """
        if self._api_version == _API_V1:
            request = self._first_positional_or_kwarg(args, kwargs, "request")
            return dict(
                get_send_message_request_attributes(request, streaming=is_streaming)
            )

        payload = self._extract_v0_payload(args, kwargs)
        return dict(
            get_send_task_payload_attributes(payload, streaming=is_streaming)
        )

    def _get_agent_url(self, instance: Any) -> str:
        """Extract the agent base URL from the client instance.

        Probes several common attribute names because the field name has
        shifted across SDK versions (``url`` → ``base_url`` → ``_url`` → in
        v1 the URL lives behind the transport, accessible via ``url`` again).
        """
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

    @staticmethod
    def _extract_v0_payload(
        args: Tuple[Any, ...], kwargs: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Extract a v0 dict payload from call arguments."""
        try:
            if kwargs.get("payload"):
                return dict(kwargs["payload"])
            if args and isinstance(args[0], dict):
                return dict(args[0])
        except Exception:
            logger.debug("Failed to extract A2A payload", exc_info=True)
        return {}

    @staticmethod
    def _first_positional_or_kwarg(
        args: Tuple[Any, ...], kwargs: Mapping[str, Any], name: str
    ) -> Any:
        if name in kwargs:
            return kwargs[name]
        if args:
            return args[0]
        return None

    def _inject_headers(
        self, kwargs: Mapping[str, Any], extra_headers: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Merge W3C propagation headers into the kwargs passed to A2AClient.

        Only meaningful on v0 — v0's ``send_task`` accepts a ``headers``
        kwarg that is passed through to httpx. v1's ``Client.send_message``
        has no such kwarg, so the caller is responsible for routing this
        path only for v0 (see ``_sync_call`` / ``_async_call`` above).
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
