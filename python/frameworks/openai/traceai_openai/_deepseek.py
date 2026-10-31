"""DeepSeek response fields on the shared OpenAI request/stream lifecycle."""

from typing import Any, Iterator, Tuple

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import MessageAttributes, SpanAttributes
from opentelemetry.util.types import AttributeValue

from traceai_openai._response_accumulator import (
    _ChatCompletionAccumulator,
    _IndexedAccumulator,
    _StringAccumulator,
    _ValuesAccumulator,
)
from traceai_openai._utils import _to_dict


def _output_alias(
    config: TraceConfig, key: str, value: Any
) -> Iterator[Tuple[str, AttributeValue]]:
    # Alias keys are not known to older cores: apply the output-text policy
    # explicitly, including explicit configuration overrides of environment flags.
    masked = config.mask(
        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}",
        value,
    )
    if masked is not None:
        yield key, masked


def deepseek_attributes(
    response: Any, config: TraceConfig
) -> Iterator[Tuple[str, AttributeValue]]:
    data = _to_dict(response)
    if response_id := data.get("id"):
        yield "deepseek.response_id", response_id
    usage = data.get("usage") or {}
    for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        if (value := usage.get(key)) is not None:
            yield f"deepseek.{key}", value
    details = (
        usage.get("completion_tokens_details")
        or usage.get("output_tokens_details")
        or {}
    )
    if (tokens := details.get("reasoning_tokens")) is not None:
        yield "deepseek.reasoning_tokens", tokens
    for position, choice in enumerate(data.get("choices") or []):
        index = choice.get("index", position)
        message = choice.get("message") or {}
        if reasoning := message.get("reasoning_content"):
            yield from _output_alias(
                config,
                f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.reasoning_content",
                reasoning,
            )
            # Retain the existing single-response aliases for the first choice.
            if position == 0:
                yield from _output_alias(
                    config, "deepseek.reasoning_content", reasoning
                )
                yield from _output_alias(config, "deepseek.output_reasoning", reasoning)
        if position != 0:
            continue
        if finish_reason := choice.get("finish_reason"):
            yield "deepseek.finish_reason", finish_reason
        if choice.get("logprobs"):
            yield "deepseek.has_logprobs", True
        tools = message.get("tool_calls") or []
        if tools:
            yield "deepseek.tool_calls_count", len(tools)
        for i, tool in enumerate(tools[:10]):
            for key in ("id", "type"):
                if (value := tool.get(key)) is not None:
                    yield from _output_alias(
                        config, f"deepseek.tool_calls.{i}.{key}", value
                    )
            for key in ("name", "arguments"):
                if (value := (tool.get("function") or {}).get(key)) is not None:
                    yield from _output_alias(
                        config, f"deepseek.tool_calls.{i}.function.{key}", value
                    )


class _DeepSeekChatAccumulator(_ChatCompletionAccumulator):
    """Accumulate extension fields without replacing the SDK stream interface.

    Use the shared indexed/string primitives so tool arguments and reasoning
    fragments stay separate across choices. No second list of raw chunks is kept.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._values = _ValuesAccumulator(
            choices=_IndexedAccumulator(
                lambda: _ValuesAccumulator(
                    message=_ValuesAccumulator(
                        content=_StringAccumulator(),
                        reasoning_content=_StringAccumulator(),
                        function_call=_ValuesAccumulator(
                            arguments=_StringAccumulator()
                        ),
                        tool_calls=_IndexedAccumulator(
                            lambda: _ValuesAccumulator(
                                function=_ValuesAccumulator(
                                    arguments=_StringAccumulator()
                                )
                            )
                        ),
                    )
                )
            )
        )

    def process_chunk(self, chunk: Any) -> None:
        values = _to_dict(chunk)
        choices = []
        for choice in values.get("choices") or []:
            choice = dict(choice)
            choice["message"] = choice.pop("delta", None) or {}
            choices.append(choice)
        self._values += {**values, "choices": choices}
        self._is_null = False
        self._cached_result = None

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        result = self._result() or {}
        choices = result.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            if content := message.get("content"):
                yield SpanAttributes.OUTPUT_VALUE, content
            elif tools := message.get("tool_calls"):
                yield (
                    SpanAttributes.OUTPUT_VALUE,
                    " \n ".join(
                        f"Function: {tool.get('function', {}).get('name', '')}"
                        f"({tool.get('function', {}).get('arguments', '')})"
                        for tool in tools
                    ),
                )
