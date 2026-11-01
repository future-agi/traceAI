"""Route the SDK's own spans through FITracer; never create a second SDK span."""

import json
import logging
from threading import RLock
from typing import Any
from weakref import WeakKeyDictionary

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

    def end(self, *args, **kwargs):
        if self._self_status is not None:
            self.__wrapped__.set_status(self._self_status)
        return self.__wrapped__.end(*args, **kwargs)


class _ErrorStream(ObjectProxy):
    """Observe read failures before HTTPX closes and ends the native span."""

    def __init__(self, stream, span):
        super().__init__(stream)
        self._self_span = span

    def _error(self, error):
        if not isinstance(error, GeneratorExit):
            try:
                self._self_span.record_exception(error)
                self._self_span.set_status(trace.Status(trace.StatusCode.ERROR))
            except Exception:
                logger.debug("Could not record Mistral stream error")

    def __iter__(self):
        try:
            yield from self.__wrapped__
        except BaseException as error:
            self._error(error)
            raise

    async def __aiter__(self):
        try:
            async for chunk in self.__wrapped__:
                yield chunk
        except BaseException as error:
            self._error(error)
            raise


class _NativeTracer(FITracer):
    def start_span(self, name: str, *args: Any, **kwargs: Any) -> _NativeSpan:
        if "embeddings" in name:
            kind = "EMBEDDING"
        elif "conversations" in name or name.startswith("invoke_agent"):
            kind = "AGENT"
        elif name.startswith("execute_tool"):
            kind = "TOOL"
        elif any(part in name for part in ("chat", "completion", "fim")):
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
    chat or agent methods alongside this hook: that would duplicate native spans.
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
        self._lock = RLock()
        from mistralai.extra.observability.otel import TracedResponse

        self._originals = []
        for cls, method, wrapper in (
            (TracingHook, "before_request", self._before_request),
            (TracingHook, "after_error", self._after_error),
            (TracingHook, "after_success", self._after_success),
            (TracedResponse, "_finalize_span", self._finalize_span),
        ):
            self._originals.append((cls, method, getattr(cls, method)))
            wrap_function_wrapper(cls, method, wrapper)

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
        return wrapped(*args, **kwargs)

    def _after_error(self, wrapped, instance, args, kwargs):
        response = kwargs.get("response", args[1] if len(args) > 1 else None)
        if response is not None and response.status_code >= 400:
            span = instance._get_span(response)
            if isinstance(span, _NativeSpan):
                span.set_status(trace.Status(trace.StatusCode.ERROR))
                span.set_attribute("http.response.status_code", response.status_code)
        return wrapped(*args, **kwargs)

    def _after_success(self, wrapped, instance, args, kwargs):
        response = wrapped(*args, **kwargs)
        span = getattr(response, "span", None)
        if isinstance(span, _NativeSpan):
            response.stream = _ErrorStream(response.stream, span)
        return response

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
            data_lines = []
            for line in bytes(instance._accumulated_sse).decode(
                "utf-8", errors="replace"
            ).splitlines() + [""]:
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
                        parts.append(
                            {
                                "type": "tool_call",
                                "id": event.get("tool_call_id"),
                                "name": event.get("name"),
                                "arguments": event.get("arguments"),
                            }
                        )
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
            for hook, values in self._hooks.items():
                # Respect an explicit customer reconfiguration made after ours.
                if hook.tracer_provider is self._provider:
                    for key, value in zip(self._FIELDS, values):
                        setattr(hook, key, value)
            self._hooks.clear()
