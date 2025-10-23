from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping, Optional, Union

from agents import MCPListToolsSpanData
from agents.tracing import Span, Trace, TracingProcessor
from agents.tracing.span_data import (
    AgentSpanData,
    CustomSpanData,
    FunctionSpanData,
    GenerationSpanData,
    GuardrailSpanData,
    HandoffSpanData,
    ResponseSpanData,
    SpanData,
)
from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiLLMProviderValues,
    FiLLMSystemValues,
    FiMimeTypeValues,
    FiSpanKindValues,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
    ToolAttributes,
    ToolCallAttributes,
)
from openai.types.responses import (
    EasyInputMessageParam,
    FunctionTool,
    Response,
    ResponseCustomToolCall,
    ResponseCustomToolCallOutputParam,
    ResponseCustomToolCallParam,
    ResponseFunctionToolCall,
    ResponseFunctionToolCallParam,
    ResponseInputContentParam,
    ResponseInputItemParam,
    ResponseOutputItem,
    ResponseOutputMessage,
    ResponseOutputMessageParam,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseUsage,
    Tool,
)
from openai.types.responses.response_input_item_param import FunctionCallOutput, Message
from openai.types.responses.response_output_message_param import Content
from opentelemetry.context import attach, detach
from opentelemetry.trace import Span as OtelSpan
from opentelemetry.trace import (
    Status,
    StatusCode,
    Tracer,
    set_span_in_context,
)
from opentelemetry.util.types import AttributeValue
from typing_extensions import assert_never

logger = logging.getLogger(__name__)


class FiTracingProcessor(TracingProcessor):
    _MAX_HANDOFFS_IN_FLIGHT = 1000

    def __init__(self, tracer: Tracer) -> None:
        self._tracer = tracer
        self._root_spans: dict[str, OtelSpan] = {}
        self._otel_spans: dict[str, OtelSpan] = {}
        self._tokens: dict[str, object] = {}
        self._trace_inputs: dict[str, Any] = {}
        self._trace_outputs: dict[str, Any] = {}
        self._reverse_handoffs_dict: OrderedDict[str, str] = OrderedDict()

    def on_trace_start(self, trace: Trace) -> None:
        """Called when a trace is started.

        Args:
            trace: The trace that started.
        """
        otel_span = self._tracer.start_span(
            name=trace.name,
            attributes={
                FI_SPAN_KIND: FiSpanKindValues.AGENT.value,
            },
        )
        self._root_spans[trace.trace_id] = otel_span

    def on_trace_end(self, trace: Trace) -> None:
        """Called when a trace is finished.

        Args:
            trace: The trace that started.
        """
        if root_span := self._root_spans.pop(trace.trace_id, None):
            if initial_input := self._trace_inputs.pop(trace.trace_id, None):
                if isinstance(initial_input, str):
                    root_span.set_attribute(INPUT_VALUE, initial_input)
                elif isinstance(initial_input, list):
                    root_span.set_attribute(INPUT_MIME_TYPE, JSON)
                    root_span.set_attribute(INPUT_VALUE, safe_json_dumps(initial_input))
                    for k, v in _get_attributes_from_input(initial_input):
                        root_span.set_attribute(k, v)
            if final_output := self._trace_outputs.pop(trace.trace_id, None):
                root_span.set_attribute(RAW_OUTPUT, final_output.model_dump_json())
                if text_output := _get_text_from_response(final_output):
                    root_span.set_attribute(OUTPUT_VALUE, text_output)
                for k, v in _get_attributes_from_response(final_output):
                    root_span.set_attribute(k, v)

            root_span.set_status(Status(StatusCode.OK))
            root_span.end()

    def on_span_start(self, span: Span[Any]) -> None:
        """Called when a span is started.

        Args:
            span: The span that started.
        """
        if not span.started_at:
            return
        start_time = datetime.fromisoformat(span.started_at)
        parent_span = (
            self._otel_spans.get(span.parent_id)
            if span.parent_id
            else self._root_spans.get(span.trace_id)
        )
        context = set_span_in_context(parent_span) if parent_span else None
        span_name = _get_span_name(span)
        otel_span = self._tracer.start_span(
            name=span_name,
            context=context,
            start_time=_as_utc_nano(start_time),
            attributes={
                FI_SPAN_KIND: _get_span_kind(span.span_data),
                LLM_SYSTEM: FiLLMSystemValues.OPENAI.value,
                RAW_INPUT: safe_json_dumps(_to_dict(span.span_data)),
            },
        )
        self._otel_spans[span.span_id] = otel_span
        self._tokens[span.span_id] = attach(set_span_in_context(otel_span))

    def on_span_end(self, span: Span[Any]) -> None:
        """Called when a span is finished. Should not block or raise exceptions.

        Args:
            span: The span that finished.
        """
        if token := self._tokens.pop(span.span_id, None):
            detach(token)  # type: ignore[arg-type]
        if not (otel_span := self._otel_spans.pop(span.span_id, None)):
            return
        otel_span.update_name(_get_span_name(span))
        # flatten_attributes: dict[str, AttributeValue] = dict(_flatten(span.export()))
        # otel_span.set_attributes(flatten_attributes)
        data = span.span_data
        if isinstance(data, ResponseSpanData):
            if span.trace_id not in self._trace_inputs:
                if hasattr(data, "input") and (input_data := data.input):
                    self._trace_inputs[span.trace_id] = input_data
            if hasattr(data, "response") and isinstance(response := data.response, Response):
                self._trace_outputs[span.trace_id] = response
                otel_span.set_attribute(OUTPUT_MIME_TYPE, JSON)
                otel_span.set_attribute(RAW_OUTPUT, response.model_dump_json())
                if text_output := _get_text_from_response(response):
                    otel_span.set_attribute(OUTPUT_VALUE, text_output)
                for k, v in _get_attributes_from_response(response):
                    otel_span.set_attribute(k, v)
            if hasattr(data, "input") and (input := data.input):
                otel_span.set_attribute(RAW_INPUT, safe_json_dumps(input))
                if isinstance(input, str):
                    otel_span.set_attribute(INPUT_VALUE, input)
                elif isinstance(input, list):
                    otel_span.set_attribute(INPUT_MIME_TYPE, JSON)
                    otel_span.set_attribute(INPUT_VALUE, safe_json_dumps(input))
                    for k, v in _get_attributes_from_input(input):
                        otel_span.set_attribute(k, v)
                elif TYPE_CHECKING:
                    assert_never(input)

            if span.parent_id and (parent_otel_span := self._otel_spans.get(span.parent_id)):
                if hasattr(data, "input") and (input := data.input):
                    if isinstance(input, str):
                        parent_otel_span.set_attribute(INPUT_VALUE, input)
                    elif isinstance(input, list):
                        parent_otel_span.set_attribute(INPUT_MIME_TYPE, JSON)
                        parent_otel_span.set_attribute(
                            INPUT_VALUE, safe_json_dumps(input)
                        )
                        for k, v in _get_attributes_from_input(input):
                            parent_otel_span.set_attribute(k, v)
                if hasattr(data, "response") and isinstance(
                    response := data.response, Response
                ):
                    parent_otel_span.set_attribute(RAW_OUTPUT, response.model_dump_json())
                    if text_output := _get_text_from_response(response):
                        parent_otel_span.set_attribute(OUTPUT_VALUE, text_output)
                    for k, v in _get_attributes_from_response(response):
                        parent_otel_span.set_attribute(k, v)
        elif isinstance(data, GenerationSpanData):
            otel_span.set_attribute(RAW_INPUT, safe_json_dumps(data.input))
            otel_span.set_attribute(RAW_OUTPUT, safe_json_dumps(data.output))
            for k, v in _get_attributes_from_generation_span_data(data):
                otel_span.set_attribute(k, v)
        elif isinstance(data, FunctionSpanData):
            for k, v in _get_attributes_from_function_span_data(data):
                otel_span.set_attribute(k, v)
        elif isinstance(data, MCPListToolsSpanData):
            for k, v in _get_attributes_from_mcp_list_tool_span_data(data):
                otel_span.set_attribute(k, v)
        elif isinstance(data, HandoffSpanData):
            # Set this dict to find the parent node when the agent span starts
            if data.to_agent and data.from_agent:
                key = f"{data.to_agent}:{span.trace_id}"
                self._reverse_handoffs_dict[key] = data.from_agent
                # Cap the size of the dict
                while len(self._reverse_handoffs_dict) > self._MAX_HANDOFFS_IN_FLIGHT:
                    self._reverse_handoffs_dict.popitem(last=False)
        elif isinstance(data, AgentSpanData):
            otel_span.set_attribute(GRAPH_NODE_ID, data.name)
            # Lookup the parent node if exists
            key = f"{data.name}:{span.trace_id}"
            if parent_node := self._reverse_handoffs_dict.pop(key, None):
                otel_span.set_attribute(GRAPH_NODE_PARENT_ID, parent_node)

        end_time: Optional[int] = None
        if span.ended_at:
            try:
                end_time = _as_utc_nano(datetime.fromisoformat(span.ended_at))
            except ValueError:
                pass
        otel_span.set_status(status=_get_span_status(span))
        otel_span.end(end_time)

    def force_flush(self) -> None:
        """Forces an immediate flush of all queued spans/traces."""
        # TODO
        pass

    def shutdown(self) -> None:
        """Called when the application stops."""
        # TODO
        pass


def _as_utc_nano(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000_000)


def _get_span_name(obj: Span[Any]) -> str:
    if hasattr(data := obj.span_data, "name") and isinstance(name := data.name, str):
        return name
    if isinstance(obj.span_data, HandoffSpanData) and obj.span_data.to_agent:
        return f"handoff to {obj.span_data.to_agent}"
    return obj.span_data.type  # type: ignore[no-any-return]


def _get_span_kind(obj: SpanData) -> str:
    if isinstance(obj, AgentSpanData):
        return FiSpanKindValues.AGENT.value
    if isinstance(obj, FunctionSpanData):
        return FiSpanKindValues.TOOL.value
    if isinstance(obj, GenerationSpanData):
        return FiSpanKindValues.LLM.value
    if isinstance(obj, ResponseSpanData):
        return FiSpanKindValues.LLM.value
    if isinstance(obj, HandoffSpanData):
        return FiSpanKindValues.TOOL.value
    if isinstance(obj, CustomSpanData):
        return FiSpanKindValues.CHAIN.value
    if isinstance(obj, GuardrailSpanData):
        return FiSpanKindValues.CHAIN.value
    return FiSpanKindValues.CHAIN.value


def _get_attributes_from_input(
    obj: Iterable[ResponseInputItemParam],
    msg_idx: int = 1,
) -> Iterator[tuple[str, AttributeValue]]:
    for i, item in enumerate(obj, msg_idx):
        prefix = f"{LLM_INPUT_MESSAGES}.{i}."
        if "type" not in item:
            if "role" in item and "content" in item:
                yield from _get_attributes_from_message_param(
                    {  # type: ignore[misc, arg-type]
                        "type": "message",
                        "role": item["role"],  # type: ignore[typeddict-item]
                        "content": item["content"],  # type: ignore[typeddict-item]
                    },
                    prefix,
                )
        elif item["type"] == "message":
            yield from _get_attributes_from_message_param(item, prefix)
        elif item["type"] == "file_search_call":
            continue  # TODO
        elif item["type"] == "computer_call":
            continue  # TODO
        elif item["type"] == "computer_call_output":
            continue  # TODO
        elif item["type"] == "web_search_call":
            continue  # TODO
        elif item["type"] == "function_call":
            yield f"{prefix}{MESSAGE_ROLE}", "assistant"
            yield from _get_attributes_from_response_function_tool_call_param(
                item,
                f"{prefix}{MESSAGE_TOOL_CALLS}.0.",
            )
        elif item["type"] == "function_call_output":
            yield from _get_attributes_from_function_call_output(item, prefix)
        elif item["type"] == "custom_tool_call":
            yield f"{prefix}{MessageAttributes.MESSAGE_ROLE}", "assistant"
            yield from _get_attributes_from_response_custom_tool_call_param(
                item,
                f"{prefix}{MessageAttributes.MESSAGE_TOOL_CALLS}.0.",
            )
        elif item["type"] == "custom_tool_call_output":
            yield from _get_attributes_from_response_custom_tool_call_output_param(item, prefix)
        elif item["type"] == "reasoning":
            continue  # TODO
        elif item["type"] == "item_reference":
            continue  # TODO
        elif item["type"] == "image_generation_call":
            continue  # TODO
        elif item["type"] == "code_interpreter_call":
            continue  # TODO
        elif item["type"] == "local_shell_call":
            continue  # TODO
        elif item["type"] == "local_shell_call_output":
            continue  # TODO
        elif item["type"] == "mcp_list_tools":
            continue  # TODO
        elif item["type"] == "mcp_approval_request":
            continue  # TODO
        elif item["type"] == "mcp_approval_response":
            continue  # TODO
        elif item["type"] == "mcp_call":
            continue  # TODO
        elif TYPE_CHECKING and item["type"] is not None:
            assert_never(item["type"])


def _get_attributes_from_message_param(
    obj: Union[
        EasyInputMessageParam,
        Message,
        ResponseOutputMessageParam,
    ],
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    yield f"{prefix}{MESSAGE_ROLE}", obj["role"]
    if content := obj.get("content"):
        if isinstance(content, str):
            yield f"{prefix}{MESSAGE_CONTENT}", content
        elif isinstance(content, list):
            yield from _get_attributes_from_message_content_list(content, prefix)


def _get_attributes_from_response_function_tool_call_param(
    obj: ResponseFunctionToolCallParam,
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    yield f"{prefix}{TOOL_CALL_ID}", obj["call_id"]
    yield f"{prefix}{TOOL_CALL_FUNCTION_NAME}", obj["name"]
    if obj["arguments"] != "{}":
        yield f"{prefix}{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}", obj["arguments"]


def _get_attributes_from_response_custom_tool_call_param(
    obj: ResponseCustomToolCallParam,
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    if (call_id := obj.get("call_id")) is not None:
        yield f"{prefix}{ToolCallAttributes.TOOL_CALL_ID}", call_id
    if (name := obj.get("name")) is not None:
        yield f"{prefix}{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}", name
    if (input_data := obj.get("input")) is not None:
        yield (
            f"{prefix}{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
            safe_json_dumps({"input": input_data}),
        )


def _get_attributes_from_response_custom_tool_call_output_param(
    obj: ResponseCustomToolCallOutputParam,
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    yield f"{prefix}{MessageAttributes.MESSAGE_ROLE}", "tool"
    if (call_id := obj.get("call_id")) is not None:
        yield f"{prefix}{MessageAttributes.MESSAGE_TOOL_CALL_ID}", call_id
    if (output := obj.get("output")) is not None:
        yield f"{prefix}{MessageAttributes.MESSAGE_CONTENT}", output


def _get_attributes_from_function_call_output(
    obj: FunctionCallOutput,
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    yield f"{prefix}{MESSAGE_ROLE}", "tool"
    yield f"{prefix}{MESSAGE_TOOL_CALL_ID}", obj["call_id"]
    yield f"{prefix}{MESSAGE_CONTENT}", obj["output"]


def _get_attributes_from_generation_span_data(
    obj: GenerationSpanData,
) -> Iterator[tuple[str, AttributeValue]]:
    if isinstance(model := obj.model, str):
        yield LLM_MODEL_NAME, model
    if isinstance(obj.model_config, dict) and (
        param := {k: v for k, v in obj.model_config.items() if v is not None}
    ):
        yield LLM_INVOCATION_PARAMETERS, safe_json_dumps(param)
        if base_url := param.get("base_url"):
            if "api.openai.com" in base_url:
                yield LLM_PROVIDER, FiLLMProviderValues.OPENAI.value
    yield from _get_attributes_from_chat_completions_input(obj.input)
    yield from _get_attributes_from_chat_completions_output(obj.output)
    yield from _get_attributes_from_chat_completions_usage(obj.usage)


def _get_attributes_from_mcp_list_tool_span_data(
    obj: MCPListToolsSpanData,
) -> Iterator[tuple[str, AttributeValue]]:
    yield OUTPUT_VALUE, safe_json_dumps(obj.result)
    yield OUTPUT_MIME_TYPE, JSON


def _get_attributes_from_chat_completions_input(
    obj: Optional[Iterable[Mapping[str, Any]]],
) -> Iterator[tuple[str, AttributeValue]]:
    if not obj:
        return
    try:
        yield INPUT_VALUE, safe_json_dumps(obj)
        yield INPUT_MIME_TYPE, JSON
    except Exception:
        pass
    yield from _get_attributes_from_chat_completions_message_dicts(
        obj,
        f"{LLM_INPUT_MESSAGES}.",
    )


def _get_attributes_from_chat_completions_output(
    obj: Optional[Iterable[Mapping[str, Any]]],
) -> Iterator[tuple[str, AttributeValue]]:
    if not obj:
        return
    try:
        yield OUTPUT_VALUE, safe_json_dumps(obj)
        yield OUTPUT_MIME_TYPE, JSON
    except Exception:
        pass
    yield from _get_attributes_from_chat_completions_message_dicts(
        obj,
        f"{LLM_OUTPUT_MESSAGES}.",
    )


def _get_attributes_from_chat_completions_message_dicts(
    obj: Iterable[Mapping[str, Any]],
    prefix: str = "",
    msg_idx: int = 0,
    tool_call_idx: int = 0,
) -> Iterator[tuple[str, AttributeValue]]:
    if not isinstance(obj, Iterable):
        return
    for msg in obj:
        if isinstance(role := msg.get("role"), str):
            yield f"{prefix}{msg_idx}.{MESSAGE_ROLE}", role
        if content := msg.get("content"):
            yield from _get_attributes_from_chat_completions_message_content(
                content,
                f"{prefix}{msg_idx}.",
            )
        if isinstance(tool_call_id := msg.get("tool_call_id"), str):
            yield f"{prefix}{msg_idx}.{MESSAGE_TOOL_CALL_ID}", tool_call_id
        if isinstance(tool_calls := msg.get("tool_calls"), Iterable):
            for tc in tool_calls:
                yield from _get_attributes_from_chat_completions_tool_call_dict(
                    tc,
                    f"{prefix}{msg_idx}.{MESSAGE_TOOL_CALLS}.{tool_call_idx}.",
                )
                tool_call_idx += 1
        msg_idx += 1


def _get_attributes_from_chat_completions_message_content(
    obj: Union[str, Iterable[Mapping[str, Any]]],
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    if isinstance(obj, str):
        yield f"{prefix}{MESSAGE_CONTENT}", obj
    elif isinstance(obj, Iterable):
        for i, item in enumerate(obj):
            if not isinstance(item, Mapping):
                continue
            yield from _get_attributes_from_chat_completions_message_content_item(
                item,
                f"{prefix}{MESSAGE_CONTENTS}.{i}.",
            )


def _get_attributes_from_chat_completions_message_content_item(
    obj: Mapping[str, Any],
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    if obj.get("type") == "text" and (text := obj.get("text")):
        yield f"{prefix}{MESSAGE_CONTENT_TYPE}", "text"
        yield f"{prefix}{MESSAGE_CONTENT_TEXT}", text


def _get_attributes_from_chat_completions_tool_call_dict(
    obj: Mapping[str, Any],
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    if id_ := obj.get("id"):
        yield f"{prefix}{TOOL_CALL_ID}", id_
    if function := obj.get("function"):
        if name := function.get("name"):
            yield f"{prefix}{TOOL_CALL_FUNCTION_NAME}", name
        if arguments := function.get("arguments"):
            if arguments != "{}":
                yield f"{prefix}{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}", arguments


def _get_attributes_from_chat_completions_usage(
    obj: Optional[Mapping[str, Any]],
) -> Iterator[tuple[str, AttributeValue]]:
    if not obj:
        return
    if input_tokens := obj.get("input_tokens"):
        yield LLM_TOKEN_COUNT_PROMPT, input_tokens
    if output_tokens := obj.get("output_tokens"):
        yield LLM_TOKEN_COUNT_COMPLETION, output_tokens


# convert dict, tuple, etc into one of these types ['bool', 'str', 'bytes', 'int', 'float']
def _convert_to_primitive(value: Any) -> Union[bool, str, bytes, int, float]:
    if isinstance(value, (bool, str, bytes, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return safe_json_dumps(value)
    if isinstance(value, dict):
        return safe_json_dumps(value)
    return str(value)


def _get_attributes_from_function_span_data(
    obj: FunctionSpanData,
) -> Iterator[tuple[str, AttributeValue]]:
    yield TOOL_NAME, obj.name
    if obj.input:
        yield INPUT_VALUE, obj.input
        yield INPUT_MIME_TYPE, JSON
    if obj.output is not None:
        yield OUTPUT_VALUE, _convert_to_primitive(obj.output)
        if (
            isinstance(obj.output, str)
            and len(obj.output) > 1
            and obj.output[0] == "{"
            and obj.output[-1] == "}"
        ):
            yield OUTPUT_MIME_TYPE, JSON


def _get_attributes_from_message_content_list(
    obj: Iterable[Union[ResponseInputContentParam, Content]],
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    for i, item in enumerate(obj):
        if item["type"] == "input_text" or item["type"] == "output_text":
            yield f"{prefix}{MESSAGE_CONTENTS}.{i}.{MESSAGE_CONTENT_TYPE}", "text"
            yield f"{prefix}{MESSAGE_CONTENTS}.{i}.{MESSAGE_CONTENT_TEXT}", item["text"]
        elif item["type"] == "input_image":
            # TODO
            ...
        elif item["type"] == "input_file":
            # TODO
            ...
        elif item["type"] == "refusal":
            yield f"{prefix}{MESSAGE_CONTENTS}.{i}.{MESSAGE_CONTENT_TYPE}", "text"
            yield f"{prefix}{MESSAGE_CONTENTS}.{i}.{MESSAGE_CONTENT_TEXT}", item["refusal"]
        elif item["type"] == "input_audio":
            # TODO: Handle input audio (OpenAI 1.105.0+)
            ...
        elif TYPE_CHECKING:
            assert_never(item["type"])


def _get_text_from_response(response: Response) -> Optional[str]:
    """Extracts the concatenated text from a Response object."""
    texts = []
    if not hasattr(response, "output") or not response.output:
        return None
    for item in response.output:
        if isinstance(item, ResponseOutputMessage) and item.content:
            for content_item in item.content:
                if isinstance(content_item, ResponseOutputText):
                    texts.append(content_item.text)
    return " ".join(texts) if texts else None


def _get_attributes_from_response(obj: Response) -> Iterator[tuple[str, AttributeValue]]:
    yield from _get_attributes_from_tools(obj.tools)
    yield from _get_attributes_from_usage(obj.usage)
    yield from _get_attributes_from_response_output(obj.output)
    if isinstance(obj.instructions, str):
        yield from _get_attributes_from_response_instruction(obj.instructions)
    else:
        pass  # TODO: handle list instructions
    yield LLM_MODEL_NAME, obj.model
    param = obj.model_dump(
        exclude_none=True,
        exclude={"object", "tools", "usage", "output", "error", "status"},
    )
    yield LLM_INVOCATION_PARAMETERS, safe_json_dumps(param)


def _get_attributes_from_tools(
    tools: Optional[Iterable[Tool]],
) -> Iterator[tuple[str, AttributeValue]]:
    if not tools:
        return
    for i, tool in enumerate(tools):
        if isinstance(tool, FunctionTool):
            yield (
                f"{LLM_TOOLS}.{i}.{TOOL_JSON_SCHEMA}",
                safe_json_dumps(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                            "strict": tool.strict,
                        },
                    }
                ),
            )
        else:
            pass


def _get_attributes_from_response_output(
    obj: Iterable[ResponseOutputItem],
    msg_idx: int = 0,
) -> Iterator[tuple[str, AttributeValue]]:
    tool_call_idx = 0
    for i, item in enumerate(obj):
        if item.type == "message":
            prefix = f"{LLM_OUTPUT_MESSAGES}.{msg_idx}."
            yield from _get_attributes_from_message(item, prefix)
            msg_idx += 1
        elif item.type == "function_call":
            yield f"{LLM_OUTPUT_MESSAGES}.{msg_idx}.{MESSAGE_ROLE}", "assistant"
            prefix = f"{LLM_OUTPUT_MESSAGES}.{msg_idx}.{MESSAGE_TOOL_CALLS}.{tool_call_idx}."
            yield from _get_attributes_from_function_tool_call(item, prefix)
            tool_call_idx += 1
        elif item.type == "custom_tool_call":
            yield f"{prefix}{MessageAttributes.MESSAGE_ROLE}", "assistant"
            yield from _get_attributes_from_response_custom_tool_call(
                item,
                f"{prefix}{MessageAttributes.MESSAGE_TOOL_CALLS}.0.",
            )
        elif item.type == "file_search_call":
            ...  # TODO
        elif item.type == "web_search_call":
            ...  # TODO
        elif item.type == "computer_call":
            ...  # TODO
        elif item.type == "reasoning":
            ...  # TODO
        elif item.type == "image_generation_call":
            ...  # TODO
        elif item.type == "code_interpreter_call":
            ...  # TODO
        elif item.type == "local_shell_call":
            ...  # TODO
        elif item.type == "mcp_call":
            ...  # TODO
        elif item.type == "mcp_list_tools":
            ...  # TODO
        elif item.type == "mcp_approval_request":
            ...  # TODO
        elif TYPE_CHECKING:
            assert_never(item)


def _get_attributes_from_response_instruction(
    instructions: Optional[str],
) -> Iterator[tuple[str, AttributeValue]]:
    if not instructions:
        return
    yield f"{LLM_INPUT_MESSAGES}.0.{MESSAGE_ROLE}", "system"
    yield f"{LLM_INPUT_MESSAGES}.0.{MESSAGE_CONTENT}", instructions


def _get_attributes_from_function_tool_call(
    obj: ResponseFunctionToolCall,
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    yield f"{prefix}{TOOL_CALL_ID}", obj.call_id
    yield f"{prefix}{TOOL_CALL_FUNCTION_NAME}", obj.name
    if obj.arguments != "{}":
        yield f"{prefix}{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}", obj.arguments


def _get_attributes_from_response_custom_tool_call(
    obj: ResponseCustomToolCall,
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    if (call_id := obj.call_id) is not None:
        yield f"{prefix}{ToolCallAttributes.TOOL_CALL_ID}", call_id
    if (name := obj.name) is not None:
        yield f"{prefix}{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}", name
    if (input_data := obj.input) is not None:
        yield (
            f"{prefix}{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
            safe_json_dumps({"input": input_data}),
        )


def _get_attributes_from_message(
    obj: ResponseOutputMessage,
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    yield f"{prefix}{MESSAGE_ROLE}", obj.role
    for i, item in enumerate(obj.content):
        if isinstance(item, ResponseOutputText):
            yield f"{prefix}{MESSAGE_CONTENTS}.{i}.{MESSAGE_CONTENT_TYPE}", "text"
            yield f"{prefix}{MESSAGE_CONTENTS}.{i}.{MESSAGE_CONTENT_TEXT}", item.text
        elif isinstance(item, ResponseOutputRefusal):
            yield f"{prefix}{MESSAGE_CONTENTS}.{i}.{MESSAGE_CONTENT_TYPE}", "text"
            yield f"{prefix}{MESSAGE_CONTENTS}.{i}.{MESSAGE_CONTENT_TEXT}", item.refusal
        elif TYPE_CHECKING:
            assert_never(item)


def _get_attributes_from_usage(
    obj: Optional[ResponseUsage],
) -> Iterator[tuple[str, AttributeValue]]:
    if not obj:
        return
    yield LLM_TOKEN_COUNT_COMPLETION, obj.output_tokens
    yield LLM_TOKEN_COUNT_PROMPT, obj.input_tokens
    yield LLM_TOKEN_COUNT_TOTAL, obj.total_tokens
    yield LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ, obj.input_tokens_details.cached_tokens
    yield LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING, obj.output_tokens_details.reasoning_tokens


def _get_span_status(obj: Span[Any]) -> Status:
    if error := getattr(obj, "error", None):
        return Status(
            status_code=StatusCode.ERROR, description=f"{error.get('message')}: {error.get('data')}"
        )
    else:
        return Status(StatusCode.OK)


def _flatten(
    obj: Mapping[str, Any],
    prefix: str = "",
) -> Iterator[tuple[str, AttributeValue]]:
    for key, value in obj.items():
        if isinstance(value, dict):
            yield from _flatten(value, f"{prefix}{key}.")
        elif isinstance(value, (str, int, float, bool, str)):
            yield f"{prefix}{key}", value
        else:
            yield f"{prefix}{key}", str(value)


def _to_dict(result: Any) -> Any:
    if not result:
        return
    if hasattr(result, "to_dict"):
        return result.to_dict()
    elif hasattr(result, "__dict__"):
        return result.__dict__
    elif isinstance(result, list):
        return [_to_dict(item) for item in result]
    elif isinstance(result, dict):
        return {key: _to_dict(value) for key, value in result.items()}
    else:
        return result
        
INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
LLM_INPUT_MESSAGES = SpanAttributes.LLM_INPUT_MESSAGES
LLM_INVOCATION_PARAMETERS = SpanAttributes.LLM_INVOCATION_PARAMETERS
LLM_MODEL_NAME = SpanAttributes.LLM_MODEL_NAME
LLM_OUTPUT_MESSAGES = SpanAttributes.LLM_OUTPUT_MESSAGES
LLM_PROVIDER = SpanAttributes.LLM_PROVIDER
LLM_SYSTEM = SpanAttributes.LLM_SYSTEM
LLM_TOKEN_COUNT_COMPLETION = SpanAttributes.LLM_TOKEN_COUNT_COMPLETION
LLM_TOKEN_COUNT_PROMPT = SpanAttributes.LLM_TOKEN_COUNT_PROMPT
LLM_TOKEN_COUNT_TOTAL = SpanAttributes.LLM_TOKEN_COUNT_TOTAL
LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ = SpanAttributes.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ
LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING = (
    SpanAttributes.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING
)
LLM_TOOLS = SpanAttributes.LLM_TOOLS
METADATA = SpanAttributes.METADATA
FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
TOOL_DESCRIPTION = SpanAttributes.TOOL_DESCRIPTION
TOOL_NAME = SpanAttributes.TOOL_NAME
TOOL_PARAMETERS = SpanAttributes.TOOL_PARAMETERS
RAW_INPUT = SpanAttributes.RAW_INPUT
RAW_OUTPUT = SpanAttributes.RAW_OUTPUT
GRAPH_NODE_ID = SpanAttributes.GRAPH_NODE_ID
GRAPH_NODE_PARENT_ID = SpanAttributes.GRAPH_NODE_PARENT_ID

MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_CONTENTS = MessageAttributes.MESSAGE_CONTENTS
MESSAGE_CONTENT_IMAGE = MessageContentAttributes.MESSAGE_CONTENT_IMAGE
MESSAGE_CONTENT_TEXT = MessageContentAttributes.MESSAGE_CONTENT_TEXT
MESSAGE_CONTENT_TYPE = MessageContentAttributes.MESSAGE_CONTENT_TYPE
MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON
MESSAGE_FUNCTION_CALL_NAME = MessageAttributes.MESSAGE_FUNCTION_CALL_NAME
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE
MESSAGE_TOOL_CALLS = MessageAttributes.MESSAGE_TOOL_CALLS
MESSAGE_TOOL_CALL_ID = MessageAttributes.MESSAGE_TOOL_CALL_ID

TOOL_CALL_FUNCTION_ARGUMENTS_JSON = ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON
TOOL_CALL_FUNCTION_NAME = ToolCallAttributes.TOOL_CALL_FUNCTION_NAME
TOOL_CALL_ID = ToolCallAttributes.TOOL_CALL_ID

TOOL_JSON_SCHEMA = ToolAttributes.TOOL_JSON_SCHEMA

JSON = FiMimeTypeValues.JSON.value
