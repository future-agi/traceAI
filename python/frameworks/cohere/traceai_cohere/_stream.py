"""Pass-through stream lifecycles and v1/v2 event accumulation."""

import logging
from time import perf_counter
from typing import Any, Dict, Mapping, Optional
from weakref import finalize

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace

from traceai_cohere._response_attributes_extractor import (
    _ChatResponseAttributesExtractor,
)
from traceai_cohere._utils import _chat_response_status, _finish_tracing, _to_dict
from traceai_cohere._with_span import _WithSpan

logger = logging.getLogger(__name__)


class _Accumulator:
    def __init__(self) -> None:
        self.response: Dict[str, Any] = {}
        self.text = []
        self.tools: Dict[int, Dict[str, Any]] = {}
        self.citations = []
        self.v2 = False

    def add(self, event: Any) -> bool:
        """Accumulate an event and report actual generated text/tool content."""
        generated = False
        data = _to_dict(event)
        kind = data.get("type") or data.get("event_type")
        if kind == "text-generation":
            text = data.get("text") or ""
            self.text.append(text)
            generated = bool(text)
        elif kind == "tool-calls-generation":
            tools = data.get("tool_calls") or []
            generated = any(tool.get("name") or tool.get("parameters") for tool in tools)
        elif kind == "stream-end":
            self.response.update(data.get("response") or {})
            if data.get("finish_reason"):
                self.response["finish_reason"] = data["finish_reason"]
        elif kind in (
            "message-start",
            "content-start",
            "content-delta",
            "tool-call-start",
            "tool-call-delta",
            "citation-start",
            "message-end",
        ):
            self.v2 = True
            delta = data.get("delta") or {}
            message = delta.get("message") or {}
            if kind == "message-start":
                if data.get("id"):
                    self.response["id"] = data["id"]
            elif kind in ("content-start", "content-delta"):
                text = (message.get("content") or {}).get("text") or ""
                self.text.append(text)
                generated = bool(text)
            elif kind == "citation-start":
                citation = message.get("citations")
                if isinstance(citation, dict):
                    self.citations.append(citation)
            elif kind in ("tool-call-start", "tool-call-delta"):
                incoming = message.get("tool_calls") or {}
                tool = self.tools.setdefault(
                    data.get("index") or 0, {"function": {"arguments": ""}}
                )
                for key in ("id", "type"):
                    if incoming.get(key) is not None:
                        tool[key] = incoming[key]
                function = incoming.get("function") or {}
                generated = bool(function.get("name") or function.get("arguments"))
                if function.get("name") is not None:
                    tool["function"]["name"] = function["name"]
                tool["function"]["arguments"] += function.get("arguments") or ""
            elif kind == "message-end":
                self.response.update(delta)
        return generated

    def result(self) -> Dict[str, Any]:
        result = dict(self.response)
        if self.v2:
            result["message"] = {
                "role": "assistant",
                "content": [{"type": "text", "text": "".join(self.text)}],
                "tool_calls": [self.tools[i] for i in sorted(self.tools)],
            }
            if self.citations:
                result["message"]["citations"] = self.citations
        elif "text" not in result:
            result["text"] = "".join(self.text)
        return result


class _StreamBase:
    def __init__(
        self,
        response: Any,
        span: _WithSpan,
        request: Mapping[str, Any],
        config: Optional[TraceConfig] = None,
        started_at: Optional[float] = None,
    ) -> None:
        self._started_at = perf_counter() if started_at is None else started_at
        self._first_token_recorded = False
        self._response = response
        self._span = span
        self._request = request
        self._accumulator = _Accumulator()
        self._response_extractor = _ChatResponseAttributesExtractor(config=config)
        self._finished = False
        # Match generator cleanup when callers abandon a stream. Explicit
        # close/aclose is still required for deterministic transport cleanup.
        finalize(self, span.finish_tracing)

    def _use_span(self):
        # Scope the span to SDK work, never to the caller between yielded events.
        return trace.use_span(
            self._span._span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        )

    def _add(self, event: Any) -> Any:
        try:
            generated = self._accumulator.add(event)
            if generated and not self._first_token_recorded:
                self._first_token_recorded = True
                # Core convention is milliseconds. Measure from the SDK call
                # to first non-empty text/tool delta, not metadata/empty starts.
                self._span._span.set_attribute(
                    SpanAttributes.GEN_AI_SERVER_TIME_TO_FIRST_TOKEN,
                    (perf_counter() - self._started_at) * 1000,
                )
        except Exception:
            logger.debug("Could not extract Cohere stream event", exc_info=True)
        return event

    def _finish(self, status: trace.StatusCode = trace.StatusCode.UNSET) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            result = self._accumulator.result()
            # Cohere yields native failure events without raising. Exhaustion
            # (or close after the terminal event) must not mark those as OK.
            status = _chat_response_status(result, self._span, default=status)
            extractor = self._response_extractor
            _finish_tracing(
                status=trace.Status(status),
                with_span=self._span,
                attributes=extractor.get_attributes(result, is_streaming=True),
                extra_attributes=extractor.get_extra_attributes(
                    result, self._request, is_streaming=True
                ),
            )
        except Exception:
            logger.debug("Could not finalize Cohere stream", exc_info=True)
        finally:
            self._span.finish_tracing(status=trace.Status(status))

    def _error(self, error: BaseException) -> None:
        self._span.record_exception(error)
        self._finish(trace.StatusCode.ERROR)


class _Stream(_StreamBase):
    def __iter__(self):
        return self

    def __next__(self):
        if self._finished:
            raise StopIteration
        try:
            with self._use_span():
                event = next(self._response)
        except StopIteration:
            self._finish(trace.StatusCode.OK)
            raise
        except BaseException as error:
            self._error(error)
            raise
        return self._add(event)

    def close(self) -> None:
        try:
            with self._use_span():
                close = getattr(self._response, "close", None)
                if close:
                    close()
        except BaseException as error:
            self._error(error)
            raise
        finally:
            self._finish()


class _AsyncStream(_StreamBase):
    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._finished:
            raise StopAsyncIteration
        try:
            with self._use_span():
                event = await self._response.__anext__()
        except StopAsyncIteration:
            self._finish(trace.StatusCode.OK)
            raise
        except BaseException as error:
            self._error(error)
            raise
        return self._add(event)

    async def aclose(self) -> None:
        try:
            with self._use_span():
                close = getattr(self._response, "aclose", None)
                if close:
                    await close()
        except BaseException as error:
            self._error(error)
            raise
        finally:
            self._finish()
