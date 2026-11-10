"""Instrumentation for Bedrock's native ConverseStream event schema."""

from __future__ import annotations

import logging
from typing import Any, Callable

import wrapt
from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import (
    FiLLMProviderValues,
    FiSpanKindValues,
    MessageAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from traceai_bedrock.utils import _EventStream, _use_span

logger = logging.getLogger(__name__)


class _ConverseStream:
    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer

    @wrapt.decorator  # type: ignore[misc]
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        with self._tracer.start_as_current_span(
            "bedrock.converse_stream",
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            callback = _ConverseStreamCallback(span)
            try:
                _set_request_attributes(span, kwargs)
            except Exception:
                # Invalid requests must still reach botocore's own validation.
                logger.warning("Could not extract ConverseStream request attributes")
            try:
                response = wrapped(*args, **kwargs)
            except BaseException as exc:
                callback.finish(error=exc)
                raise
            try:
                if (stream := response.get("stream")) is not None:
                    response["stream"] = _ConverseEventStream(stream, callback, _use_span(span))
                else:
                    callback.finish(closed=True)
            except Exception:
                # Telemetry must not prevent the caller from receiving a response.
                callback.finish(closed=True)
            return response


class _ConverseEventStream(_EventStream):
    def __iter__(self):
        try:
            return super().__iter__()
        except BaseException as exc:
            self._self_callback.finish(error=exc)
            raise

    def close(self, *args: Any, **kwargs: Any) -> Any:
        try:
            with self._self_context_manager_factory():
                result = self.__wrapped__.close(*args, **kwargs)
        except BaseException as exc:
            self._self_callback.finish(error=exc)
            raise
        self._self_callback.finish(closed=True)
        return result


class _ConverseStreamCallback:
    def __init__(self, span: Span) -> None:
        self._span = span
        self._role: str | None = None
        self._blocks: dict[int, dict[str, Any]] = {}
        self._finished = False

    def __call__(self, event: Any) -> Any:
        if self._finished:
            return event
        if isinstance(event, StopIteration):
            self.finish()
        elif isinstance(event, BaseException):
            self.finish(error=event)
        elif isinstance(event, dict):
            # _EventStream isolates callback errors and returns the original event.
            if start := event.get("messageStart"):
                self._role = start.get("role")
            if start := event.get("contentBlockStart"):
                if tool := start.get("start", {}).get("toolUse"):
                    block = self._blocks.setdefault(start["contentBlockIndex"], {})
                    block["toolUse"] = dict(tool)
            if delta := event.get("contentBlockDelta"):
                block = self._blocks.setdefault(delta["contentBlockIndex"], {})
                content = delta.get("delta", {})
                if isinstance(text := content.get("text"), str):
                    block["text"] = block.get("text", "") + text
                if isinstance(fragment := content.get("toolUse", {}).get("input"), str):
                    tool = block.setdefault("toolUse", {})
                    tool["input"] = tool.get("input", "") + fragment
            if stop := event.get("messageStop"):
                if reason := stop.get("stopReason"):
                    self._span.set_attribute(SpanAttributes.GEN_AI_RESPONSE_FINISH_REASONS, [reason])
                # Do not end here: metadata (including usage) follows messageStop.
            if metadata := event.get("metadata"):
                usage = metadata.get("usage", {})
                for field, attribute in (
                    ("inputTokens", SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS),
                    ("outputTokens", SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS),
                    ("totalTokens", SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS),
                    ("cacheReadInputTokens", SpanAttributes.GEN_AI_USAGE_CACHE_READ_TOKENS),
                    ("cacheWriteInputTokens", SpanAttributes.GEN_AI_USAGE_CACHE_WRITE_TOKENS),
                ):
                    if isinstance(count := usage.get(field), int):
                        self._span.set_attribute(attribute, count)
            # Botocore normally raises EventStreamError; also handle error union
            # members without changing what a custom stream yields to its caller.
            for name in event:
                if name.endswith("Exception"):
                    self.finish(error=RuntimeError(f"Bedrock ConverseStream {name}"))
                    break
        return event

    def finish(self, error: BaseException | None = None, closed: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            blocks = [block for _, block in sorted(self._blocks.items())]
            message = {"role": self._role, "content": blocks}
            prefix = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0"
            _set_message_attributes(self._span, prefix, message)
            texts = [block["text"] for block in blocks if "text" in block]
            if texts:
                text = "\n".join(texts)
                self._span.set_attribute(SpanAttributes.OUTPUT_VALUE, text)
                self._span.set_attribute(f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}", text)
        except Exception:
            logger.warning("Could not extract ConverseStream output attributes")
        try:
            if error is not None:
                self._span.record_exception(error)
                self._span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            elif not closed:
                self._span.set_status(Status(StatusCode.OK))
            # An explicit close before exhaustion is not evidence of success.
        except Exception:
            logger.warning("Could not set ConverseStream terminal status")
        finally:
            try:
                self._span.end()
            except Exception:
                logger.warning("Could not end ConverseStream span")


def _set_message_attributes(span: Span, prefix: str, message: dict[str, Any]) -> None:
    # Reuse the Converse text/image schema. Import lazily because __init__ installs
    # this wrapper before defining its existing message helpers.
    from traceai_bedrock import _get_attributes_from_message_param

    for key, value in _get_attributes_from_message_param(message):
        span.set_attribute(f"{prefix}.{key}", value)
    tool_index = 0
    for block in message.get("content", []):
        if not isinstance(block, dict) or not isinstance(tool := block.get("toolUse"), dict):
            continue
        tool_prefix = f"{prefix}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{tool_index}"
        tool_index += 1
        for field, attribute in (
            ("toolUseId", ToolCallAttributes.TOOL_CALL_ID),
            ("name", ToolCallAttributes.TOOL_CALL_FUNCTION_NAME),
            ("input", ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON),
        ):
            if (value := tool.get(field)) is not None:
                if field == "input" and not isinstance(value, str):
                    value = safe_json_dumps(value)
                span.set_attribute(f"{tool_prefix}.{attribute}", value)


def _set_request_attributes(span: Span, request: dict[str, Any]) -> None:
    span.set_attribute(SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value)
    span.set_attribute(SpanAttributes.GEN_AI_PROVIDER_NAME, FiLLMProviderValues.AWS.value)
    span.set_attributes(dict(get_attributes_from_context()))
    if model := request.get("modelId"):
        span.set_attribute(SpanAttributes.GEN_AI_REQUEST_MODEL, model)
    if config := request.get("inferenceConfig"):
        span.set_attribute(SpanAttributes.GEN_AI_REQUEST_PARAMETERS, safe_json_dumps(config))
    messages = []
    if system := request.get("system"):
        messages.append({"role": "system", "content": system})
    messages.extend(request.get("messages", []))
    for index, message in enumerate(messages):
        if isinstance(message, dict):
            _set_message_attributes(span, f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}", message)
    if messages and isinstance(messages[-1], dict):
        text = "\n".join(
            block.get("text", "") for block in messages[-1].get("content", [])
            if isinstance(block, dict)
        ).strip("\n")
        if text:
            span.set_attribute(SpanAttributes.INPUT_VALUE, text)
