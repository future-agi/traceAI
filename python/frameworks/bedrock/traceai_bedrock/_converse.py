"""Converse tracing shared by synchronous and asynchronous Runtime clients.

Only consumed events are observed. A messageStop is not stream exhaustion: usage
arrives afterwards in metadata. The caller must exhaust or explicitly close a
stream; creating a response alone does not finish its span.
"""

import logging
from functools import wraps
from typing import (
    Any,
    AsyncIterator,
    Callable,
    ContextManager,
    Dict,
    Iterator,
    Optional,
)

import wrapt
from opentelemetry import context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import Span, Status, StatusCode, Tracer, use_span

from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import (
    FiSpanKindValues,
    MessageAttributes,
    SpanAttributes as SA,
    ToolCallAttributes,
)

logger = logging.getLogger(__name__)


def _observe(callback: Callable[..., Any], *args: Any) -> None:
    """Telemetry failures must neither replace SDK errors nor drop SDK events."""
    try:
        callback(*args)
    except Exception:
        # Do not log payloads or exception text: these may contain private content.
        logger.debug("Unable to collect Bedrock Converse telemetry")


def _set_message(span: Any, message: Dict[str, Any], prefix: str) -> None:
    # Keep existing text/image attribute conventions and their privacy filters.
    from traceai_bedrock import _get_attributes_from_message_param

    for key, value in _get_attributes_from_message_param(message):
        span.set_attribute(f"{prefix}.{key}", value)
    tool_index = 0
    for block in message.get("content", []):
        if not isinstance(block, dict):
            continue
        if tool := block.get("toolUse"):
            tool_prefix = (
                f"{prefix}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{tool_index}"
            )
            for key, value in (
                (ToolCallAttributes.TOOL_CALL_ID, tool.get("toolUseId")),
                (ToolCallAttributes.TOOL_CALL_FUNCTION_NAME, tool.get("name")),
                (
                    ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON,
                    tool.get("input"),
                ),
            ):
                if value is not None:
                    span.set_attribute(
                        f"{tool_prefix}.{key}",
                        value if isinstance(value, str) else safe_json_dumps(value),
                    )
            tool_index += 1
        if result := block.get("toolResult"):
            span.set_attribute(
                f"{prefix}.{MessageAttributes.MESSAGE_TOOL_CALL_ID}",
                result.get("toolUseId", ""),
            )
            span.set_attribute(
                f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}",
                safe_json_dumps(result.get("content", [])),
            )


class _ConverseTrace:
    def __init__(self, tracer: Tracer, name: str, request: Dict[str, Any]) -> None:
        self.span = tracer.start_span(name)
        self.finished = False
        self.role = "assistant"
        self.blocks: Dict[int, Dict[str, Any]] = {}
        self.usage: Dict[str, Any] = {}
        self.stop_reason: Optional[str] = None
        _observe(self._input, request)

    def active(self) -> ContextManager[Span]:
        return use_span(
            self.span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        )

    def _input(self, request: Dict[str, Any]) -> None:
        self.span.set_attributes(dict(get_attributes_from_context()))
        self.span.set_attribute(SA.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value)
        self.span.set_attribute(SA.GEN_AI_PROVIDER_NAME, "aws")
        if model := request.get("modelId"):
            self.span.set_attribute(SA.GEN_AI_REQUEST_MODEL, model)
        if params := request.get("inferenceConfig"):
            self.span.set_attribute(
                SA.GEN_AI_REQUEST_PARAMETERS, safe_json_dumps(params)
            )
        messages = []
        if system := request.get("system"):
            messages.append({"role": "system", "content": system})
        messages.extend(request.get("messages", []))
        for index, message in enumerate(messages):
            _set_message(self.span, message, f"{SA.GEN_AI_INPUT_MESSAGES}.{index}")
        if messages:
            text = "\n".join(
                block.get("text", "") for block in messages[-1].get("content", [])
            ).strip("\n")
            if text:
                self.span.set_attribute(SA.INPUT_VALUE, text)

    def event(self, event: Any) -> None:
        if self.finished or not isinstance(event, dict):
            return
        _observe(self._event, event)

    def _event(self, event: Dict[str, Any]) -> None:
        if start := event.get("messageStart"):
            self.role = start.get("role", "assistant")
        if start := event.get("contentBlockStart"):
            block = self.blocks.setdefault(start["contentBlockIndex"], {})
            if tool := start.get("start", {}).get("toolUse"):
                block["toolUse"] = {**tool, "input": ""}
        if item := event.get("contentBlockDelta"):
            block = self.blocks.setdefault(item["contentBlockIndex"], {})
            delta = item.get("delta", {})
            if "text" in delta:
                block.setdefault("_text_parts", []).append(delta["text"])
            if tool := delta.get("toolUse"):
                target = block.setdefault("toolUse", {"input": ""})
                target.setdefault("_input_parts", []).append(tool.get("input", ""))
        if stop := event.get("messageStop"):
            self.stop_reason = stop.get("stopReason")
        if metadata := event.get("metadata"):
            self.usage.update(metadata.get("usage", {}))
        # botocore normally raises EventStreamError. Preserve raw error events
        # too if a custom transport returns one instead of raising.
        for name in event:
            if name.endswith("Exception"):
                self.finish(RuntimeError(name))
                break

    def response(self, response: Dict[str, Any]) -> None:
        message = response.get("output", {}).get("message", {})
        self.role = message.get("role", "assistant")
        self.blocks = dict(enumerate(message.get("content", [])))
        self.usage = response.get("usage", {})
        self.stop_reason = response.get("stopReason")

    def _output(self) -> None:
        content = []
        for _, original in sorted(self.blocks.items()):
            block = dict(original)
            # Join each content block once, not on every delta (quadratic copying).
            if "_text_parts" in block:
                block["text"] = "".join(block.pop("_text_parts"))
            if "toolUse" in block:
                tool = block["toolUse"] = dict(block["toolUse"])
                if "_input_parts" in tool:
                    tool["input"] = "".join(tool.pop("_input_parts"))
            content.append(block)
        if content:
            prefix = f"{SA.GEN_AI_OUTPUT_MESSAGES}.0"
            _set_message(self.span, {"role": self.role, "content": content}, prefix)
            text = "\n".join(block["text"] for block in content if "text" in block)
            if any("text" in block for block in content):
                self.span.set_attribute(SA.OUTPUT_VALUE, text)
                self.span.set_attribute(
                    f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}", text
                )
        if self.stop_reason:
            self.span.set_attribute(
                SA.GEN_AI_RESPONSE_FINISH_REASONS, [self.stop_reason]
            )
        for key, attribute in (
            ("inputTokens", SA.GEN_AI_USAGE_INPUT_TOKENS),
            ("outputTokens", SA.GEN_AI_USAGE_OUTPUT_TOKENS),
            ("totalTokens", SA.GEN_AI_USAGE_TOTAL_TOKENS),
            ("cacheReadInputTokens", SA.GEN_AI_USAGE_CACHE_READ_TOKENS),
            ("cacheWriteInputTokens", SA.GEN_AI_USAGE_CACHE_WRITE_TOKENS),
        ):
            if (value := self.usage.get(key)) is not None:
                self.span.set_attribute(attribute, value)

    def finish(self, error: Optional[BaseException] = None) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            _observe(self._output)
            if error is not None:
                _observe(self.span.record_exception, error)
                # No SDK message in the description; FITracer filters exception
                # details according to TraceConfig (including hide_error_details).
                _observe(self.span.set_status, Status(StatusCode.ERROR))
            else:
                _observe(self.span.set_status, Status(StatusCode.OK))
        finally:
            self.blocks.clear()
            _observe(self.span.end)

    def result(self, response: Dict[str, Any], streaming: bool) -> Dict[str, Any]:
        if streaming and "stream" in response:
            response["stream"] = _ConverseStream(response["stream"], self)
        else:
            _observe(self.response, response)
            self.finish()
        return response


class _ConverseIterator:
    def __init__(self, iterator: Any, state: _ConverseTrace) -> None:
        self.iterator, self.state = iterator, state

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        try:
            with self.state.active():
                event = next(self.iterator)
        except StopIteration:
            self.state.finish()
            raise
        except BaseException as error:
            self.state.finish(error)
            raise
        self.state.event(event)
        return event


class _AsyncConverseIterator:
    def __init__(self, iterator: Any, state: _ConverseTrace) -> None:
        self.iterator, self.state = iterator, state

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        try:
            with self.state.active():
                event = await self.iterator.__anext__()
        except StopAsyncIteration:
            self.state.finish()
            raise
        except BaseException as error:
            self.state.finish(error)
            raise
        self.state.event(event)
        return event


class _ConverseStream(wrapt.ObjectProxy):
    def __init__(self, stream: Any, state: _ConverseTrace) -> None:
        super().__init__(stream)
        self._self_state = state

    def __iter__(self) -> Iterator[Any]:
        try:
            return _ConverseIterator(iter(self.__wrapped__), self._self_state)
        except BaseException as error:
            self._self_state.finish(error)
            raise

    def __aiter__(self) -> AsyncIterator[Any]:
        try:
            return _AsyncConverseIterator(
                self.__wrapped__.__aiter__(), self._self_state
            )
        except BaseException as error:
            self._self_state.finish(error)
            raise

    def close(self) -> Any:
        # boto3 EventStream and aiobotocore AioEventStream both close synchronously.
        try:
            result = self.__wrapped__.close()
        except BaseException as error:
            self._self_state.finish(error)
            raise
        self._self_state.finish()
        return result


def _converse_wrapper(
    tracer: Tracer,
    wrapped: Callable[..., Any],
    *,
    streaming: bool = False,
    asynchronous: bool = False,
) -> Callable[..., Any]:
    name = "bedrock.converse_stream" if streaming else "bedrock.converse"

    @wraps(wrapped)
    def sync(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        state = _ConverseTrace(tracer, name, kwargs)
        try:
            with state.active():
                response = wrapped(*args, **kwargs)
        except BaseException as error:
            state.finish(error)
            raise
        return state.result(response, streaming)

    @wraps(wrapped)
    async def aio(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)
        state = _ConverseTrace(tracer, name, kwargs)
        try:
            with state.active():
                response = await wrapped(*args, **kwargs)
        except BaseException as error:
            state.finish(error)
            raise
        return state.result(response, streaming)

    return aio if asynchronous else sync


def _async_client_creation_wrapper(tracer: Tracer) -> Callable[..., Any]:
    async def create(
        wrapped: Callable[..., Any], instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        client = await wrapped(*args, **kwargs)
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return client
        if client.meta.service_model.service_name == "bedrock-runtime":
            for method in ("converse", "converse_stream"):
                if hasattr(client, method):
                    original = getattr(client, method)
                    setattr(client, f"_unwrapped_{method}", original)
                    setattr(
                        client,
                        method,
                        _converse_wrapper(
                            tracer,
                            original,
                            streaming=method == "converse_stream",
                            asynchronous=True,
                        ),
                    )
        return client

    return create
