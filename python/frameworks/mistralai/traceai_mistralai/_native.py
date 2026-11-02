"""Route the SDK's own spans through FITracer; never create a second SDK span."""

import asyncio
import json
import logging
import re
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import RLock
from typing import Any
from weakref import WeakKeyDictionary, ref

from fi_instrumentation import FITracer, TraceConfig
from mistralai.extra.observability.formatting import format_output_message
from opentelemetry import trace
from wrapt import ObjectProxy, wrap_function_wrapper

from traceai_mistralai._privacy import _NativeTraceConfig

logger = logging.getLogger(__name__)
# Fail-open telemetry diagnostics deliberately omit payload-bearing exceptions.


class _NativeSpan(ObjectProxy):
    """Defer success until end so an SSE read error can still mark ERROR."""

    def __init__(self, span):
        super().__init__(span)
        self._self_status = None
        self._self_iteration_depth = 0
        self._self_pending_end = None

    def set_status(self, status, description=None):
        if (
            self._self_status is not None
            and self._self_status.status_code == trace.StatusCode.ERROR
        ):
            return
        self._self_status = (
            status
            if isinstance(status, trace.Status)
            else trace.Status(status, description)
        )

    @contextmanager
    def observe_iteration(self):
        # HTTPX can close at EOF before the SDK decodes its buffered last event.
        # Delay only the end, so decoding failures still update this same span.
        self._self_iteration_depth += 1
        try:
            yield
        except (StopIteration, StopAsyncIteration, GeneratorExit):
            raise
        except BaseException as error:
            _record_error(self, error)
            self.end()
            raise
        finally:
            self._self_iteration_depth -= 1
            if self._self_iteration_depth == 0 and self._self_pending_end is not None:
                args, kwargs = self._self_pending_end
                self._self_pending_end = None
                self.end(*args, **kwargs)

    def end(self, *args, **kwargs):
        if self._self_iteration_depth:
            self._self_pending_end = (args, kwargs)
            return
        if self._self_status is not None:
            self.__wrapped__.set_status(self._self_status)
        return self.__wrapped__.end(*args, **kwargs)


def _record_error(span, error):
    if isinstance(error, GeneratorExit):
        return
    try:
        if (
            span._self_status is None
            or span._self_status.status_code != trace.StatusCode.ERROR
        ):
            span.record_exception(error)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
    except Exception:
        logger.debug("Could not record Mistral stream error")


class _ErrorStream(ObjectProxy):
    """Observe read failures before HTTPX closes and ends the native span."""

    def __init__(self, stream, span):
        super().__init__(stream)
        self._self_span = span

    def __iter__(self):
        try:
            yield from self.__wrapped__
        except BaseException as error:
            _record_error(self._self_span, error)
            raise

    async def __aiter__(self):
        try:
            async for chunk in self.__wrapped__:
                yield chunk
        except BaseException as error:
            _record_error(self._self_span, error)
            raise


class _NativeTracer(FITracer):
    def start_span(self, name: str, *args: Any, **kwargs: Any) -> _NativeSpan:
        if "embeddings" in name:
            kind = "EMBEDDING"
        elif "conversations" in name or name.startswith("invoke_agent"):
            kind = "AGENT"
        elif name.startswith("execute_tool"):
            kind = "TOOL"
        elif any(
            part in name for part in ("chat", "completion", "fim", "stream_agents")
        ):
            kind = "LLM"
        else:
            kind = "CHAIN"
        kwargs.setdefault("fi_span_kind", kind.lower())
        return _NativeSpan(super().start_span(name, *args, **kwargs))


class _NativeProvider(trace.TracerProvider):
    def __init__(self, provider: trace.TracerProvider, config: TraceConfig) -> None:
        self._provider = provider
        self._config = _NativeTraceConfig(config)

    def get_tracer(self, *args: Any, **kwargs: Any) -> _NativeTracer:
        return _NativeTracer(self._provider.get_tracer(*args, **kwargs), self._config)


class _NativeInstrumentation:
    """Adapt the native hook for both existing and subsequently created clients.

    The native hook owns request context and span creation. The adapter routes
    those spans and fills HTTP-error and conversation-stream gaps. Do not patch
    chat or agent methods to create spans alongside this hook. Method observers
    below only hold the existing span open through SDK response validation.
    """

    _FIELDS = (
        "tracer_provider",
        "tracer",
        "tracing_enabled",
        "_telemetry_auto_disabled",
    )

    def __init__(self, provider: trace.TracerProvider, config: TraceConfig) -> None:
        from mistralai.client._hooks.tracing import TracingHook

        self._provider = _NativeProvider(provider, config)
        self._hooks = WeakKeyDictionary()
        self._async_sends = WeakKeyDictionary()
        self._lock = RLock()
        from mistralai.extra.observability.otel import TracedResponse
        from mistralai.client.utils.eventstreaming import EventStream, EventStreamAsync

        self._response_scope = ContextVar("mistral_response_scope", default=None)
        self._originals = []
        for cls, method, wrapper in (
            (TracingHook, "before_request", self._before_request),
            (TracingHook, "after_error", self._after_error),
            (TracingHook, "after_success", self._after_success),
            (TracedResponse, "_finalize_span", self._finalize_span),
            (EventStream, "__next__", self._stream_next),
            (EventStreamAsync, "__anext__", self._stream_anext),
        ):
            self._originals.append((cls, method, getattr(cls, method)))
            wrap_function_wrapper(cls, method, wrapper)

        # The native hook ends buffered responses before generated methods
        # deserialize them. Observe those methods without creating any spans.
        # A per-call context keeps concurrent sync/async requests independent.
        from mistralai.client.agents import Agents
        from mistralai.client.chat import Chat
        from mistralai.client.conversations import Conversations
        from mistralai.client.embeddings import Embeddings
        from mistralai.client.fim import Fim

        for cls, methods in (
            (Chat, ("complete", "stream")),
            (Agents, ("complete", "stream")),
            (Embeddings, ("create",)),
            (Fim, ("complete", "stream")),
            (
                Conversations,
                (
                    "start",
                    "append",
                    "restart",
                    "start_stream",
                    "append_stream",
                    "restart_stream",
                ),
            ),
        ):
            for method in methods:
                for name, wrapper in (
                    (method, self._call),
                    (method + "_async", self._call_async),
                ):
                    self._originals.append((cls, name, getattr(cls, name)))
                    wrap_function_wrapper(cls, name, wrapper)

    @contextmanager
    def _call_scope(self):
        with ExitStack() as scope:
            token = self._response_scope.set(scope)
            try:
                yield
            finally:
                self._response_scope.reset(token)

    def _call(self, wrapped, instance, args, kwargs):
        with self._call_scope():
            return wrapped(*args, **kwargs)

    async def _call_async(self, wrapped, instance, args, kwargs):
        with self._call_scope():
            return await wrapped(*args, **kwargs)

    def _before_request(self, wrapped, instance, args, kwargs):
        with self._lock:
            if (
                instance not in self._hooks
                or instance.tracer_provider is not self._provider
            ):
                self._hooks[instance] = tuple(
                    getattr(instance, key) for key in self._FIELDS
                )
            instance.tracer_provider = self._provider
            # Do not let SDK auto-export configuration create a second destination.
            instance._telemetry_auto_disabled = True
        request = wrapped(*args, **kwargs)
        hook_ctx = kwargs.get("hook_ctx", args[0] if args else None)
        client = getattr(getattr(hook_ctx, "config", None), "async_client", None)
        if client is not None:
            self._wrap_async_send(client, instance)
        return request

    def _wrap_async_send(self, client, hook):
        # SDK 3.6 only catches Exception during send, not CancelledError. Keep
        # its context management and finish its existing request span on cancel.
        try:
            with self._lock:
                previous = self._async_sends.get(client)
                if previous is not None and client.send is previous():
                    return
                original = client.send

                @wraps(original)
                async def send(request, *args, **kwargs):
                    try:
                        return await original(request, *args, **kwargs)
                    except asyncio.CancelledError as error:
                        try:
                            if isinstance(hook._get_request_span(request), _NativeSpan):
                                hook._finish_request_span_with_error(request, error)
                        except Exception:
                            logger.debug("Could not finish cancelled Mistral request")
                        raise

                client.send = send
                # A bound send method retains its client: weak-reference both
                # sides so tracking does not keep closed application clients alive.
                self._async_sends[client] = ref(send)
        except Exception:
            logger.debug("Could not wrap Mistral async send")

    def _after_error(self, wrapped, instance, args, kwargs):
        response = kwargs.get("response", args[1] if len(args) > 1 else None)
        if response is not None and response.status_code >= 400:
            span = instance._get_span(response)
            if isinstance(span, _NativeSpan):
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                span.set_attribute("http.response.status_code", response.status_code)
        return wrapped(*args, **kwargs)

    def _after_success(self, wrapped, instance, args, kwargs):
        response = kwargs.get("response", args[1] if len(args) > 1 else None)
        span = instance._get_span(response)
        scope = self._response_scope.get()
        if isinstance(span, _NativeSpan) and scope is not None:
            # Enter before the hook queues end(), leave after SDK validation.
            scope.enter_context(span.observe_iteration())
        response = wrapped(*args, **kwargs)
        span = getattr(response, "span", None)
        if isinstance(span, _NativeSpan):
            response.stream = _ErrorStream(response.stream, span)
        return response

    def _stream_next(self, wrapped, instance, args, kwargs):
        span = getattr(instance.response, "span", None)
        if not isinstance(span, _NativeSpan):
            return wrapped(*args, **kwargs)
        with span.observe_iteration():
            return wrapped(*args, **kwargs)

    async def _stream_anext(self, wrapped, instance, args, kwargs):
        span = getattr(instance.response, "span", None)
        if not isinstance(span, _NativeSpan):
            return await wrapped(*args, **kwargs)
        with span.observe_iteration():
            return await wrapped(*args, **kwargs)

    def _finalize_span(self, wrapped, instance, args, kwargs):
        span = instance.span
        if (
            not isinstance(span, _NativeSpan)
            or "conversations" not in instance.operation_id
        ):
            return wrapped(*args, **kwargs)
        # SDK 3.6's native stream finalizer only decodes chat CompletionChunks.
        # Adapt conversation SSE attributes on that same span, not a second span.
        try:
            parts = []
            tool_calls = {}
            data_lines = []
            # SSE delimiters are CR/LF only. Unicode separators are valid JSON
            # string content and must not split a message delta's data field.
            payload = bytes(instance._accumulated_sse).decode("utf-8", errors="replace")
            for line in re.split(r"\r\n|\r|\n", payload) + [""]:
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line and data_lines:
                    raw, data_lines = "\n".join(data_lines), []
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_type = event.get("type")
                    if event_type == "conversation.response.done":
                        usage = event.get("usage") or {}
                        if not isinstance(usage, dict):
                            continue
                        for source, target in (
                            ("prompt_tokens", "input_tokens"),
                            ("completion_tokens", "output_tokens"),
                            ("total_tokens", "total_tokens"),
                        ):
                            if isinstance(usage.get(source), int):
                                span.set_attribute(
                                    "gen_ai.usage." + target, usage[source]
                                )
                    elif event_type == "message.output.delta":
                        content = event.get("content")
                        if isinstance(content, (str, dict)):
                            # Reuse the SDK's non-streaming OTel conversion for
                            # text, reasoning and image chunks, not provider keys.
                            message = {
                                "content": [content]
                                if isinstance(content, dict)
                                else content
                            }
                            parts.extend(
                                format_output_message({"message": message})["parts"]
                            )
                    elif event_type == "function.call.delta":
                        # Deltas for different calls may be interleaved. Keep
                        # the first-seen output order and append each argument
                        # fragment to its call instead of emitting duplicates.
                        identity = (
                            event.get("output_index"),
                            event.get("tool_call_id"),
                        )
                        part = tool_calls.get(identity)
                        if part is None:
                            part = {
                                "type": "tool_call",
                                "id": event.get("tool_call_id"),
                                "name": event.get("name"),
                                "arguments": "",
                            }
                            tool_calls[identity] = part
                            parts.append(part)
                        if isinstance(event.get("arguments"), str):
                            part["arguments"] += event["arguments"]
                    elif event_type == "conversation.response.error":
                        span.set_status(trace.Status(trace.StatusCode.ERROR))
            if parts:
                span.set_attribute(
                    "gen_ai.output.messages",
                    json.dumps([{"role": "assistant", "parts": parts}]),
                )
        except Exception:
            logger.debug("Could not extract Mistral conversation stream attributes")
        finally:
            try:
                span.end()
            except Exception:
                logger.debug("Could not end Mistral conversation span")
            instance.span = None

    def uninstrument(self) -> None:
        for cls, method, original in reversed(self._originals):
            setattr(cls, method, original)
        with self._lock:
            for client, send_ref in self._async_sends.items():
                send = send_ref()
                if send is not None and client.send is send:
                    client.send = send.__wrapped__
            self._async_sends.clear()
            for hook, values in self._hooks.items():
                # Respect an explicit customer reconfiguration made after ours.
                if hook.tracer_provider is self._provider:
                    for key, value in zip(self._FIELDS, values):
                        setattr(hook, key, value)
            self._hooks.clear()
