from enum import Enum
from inspect import signature
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, Mapping, Optional, Tuple

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue

from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import (
    MessageAttributes,
    FiMimeTypeValues,
    FiSpanKindValues,
    SpanAttributes,
    ToolAttributes,
    ToolCallAttributes,
    MessageContentAttributes,
)

if TYPE_CHECKING:
    from smolagents.tools import Tool


def _flatten(mapping: Optional[Mapping[str, Any]]) -> Iterator[Tuple[str, AttributeValue]]:
    if not mapping:
        return
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            for sub_key, sub_value in _flatten(value):
                yield f"{key}.{sub_key}", sub_value
        elif isinstance(value, list) and any(isinstance(item, Mapping) for item in value):
            for index, sub_mapping in enumerate(value):
                for sub_key, sub_value in _flatten(sub_mapping):
                    yield f"{key}.{index}.{sub_key}", sub_value
        else:
            if isinstance(value, Enum):
                value = value.value
            yield key, value


def _get_input_value(method: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    arguments = _bind_arguments(method, *args, **kwargs)
    arguments = _strip_method_args(arguments)
    return safe_json_dumps(arguments)


def _bind_arguments(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    method_signature = signature(method)
    bound_args = method_signature.bind(*args, **kwargs)
    bound_args.apply_defaults()
    return bound_args.arguments


def _strip_method_args(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if key not in ("self", "cls")}


def _smolagent_run_attributes(
    agent: Any, arguments: dict[str, Any]
) -> Iterator[Tuple[str, AttributeValue]]:
    if task := agent.task:
        yield "smolagents.task", task
    if additional_args := arguments.get("additional_args"):
        yield "smolagents.additional_args", safe_json_dumps(additional_args)
    yield "smolagents.max_steps", agent.max_steps
    yield "smolagents.tools_names", list(agent.tools.keys())
    for managed_agent_index, managed_agent in enumerate(agent.managed_agents.values()):
        yield f"smolagents.managed_agents.{managed_agent_index}.name", managed_agent.name
        yield (
            f"smolagents.managed_agents.{managed_agent_index}.description",
            managed_agent.description,
        )
        if getattr(managed_agent, "additional_prompting", None):
            yield (
                f"smolagents.managed_agents.{managed_agent_index}.additional_prompting",
                managed_agent.additional_prompting,
            )
        elif getattr(managed_agent, "managed_agent_prompt", None):
            yield (
                f"smolagents.managed_agents.{managed_agent_index}.managed_agent_prompt",
                managed_agent.managed_agent_prompt,
            )
        if getattr(managed_agent, "agent", None):
            yield (
                f"smolagents.managed_agents.{managed_agent_index}.max_steps",
                managed_agent.agent.max_steps,
            )
            yield (
                f"smolagents.managed_agents.{managed_agent_index}.tools_names",
                list(managed_agent.agent.tools.keys()),
            )


class _RunWrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        span_name = f"{instance.__class__.__name__}.run"
        agent = instance
        arguments = _bind_arguments(wrapped, *args, **kwargs)
        with self._tracer.start_as_current_span(
            span_name,
            attributes=dict(
                _flatten(
                    {
                        FI_SPAN_KIND: AGENT,
                        INPUT_VALUE: _get_input_value(
                            wrapped,
                            *args,
                            **kwargs,
                        ),
                        **dict(_smolagent_run_attributes(agent, arguments)),
                        **dict(get_attributes_from_context()),
                        **dict(_get_raw_input(args, **kwargs)),
                    }
                )
            ),
        ) as span:
            agent_output = wrapped(*args, **kwargs)
            span.set_attribute(RAW_OUTPUT, _get_raw_output(agent_output))
            span.set_attribute(LLM_TOKEN_COUNT_PROMPT, agent.monitor.total_input_token_count)
            span.set_attribute(LLM_TOKEN_COUNT_COMPLETION, agent.monitor.total_output_token_count)
            span.set_attribute(
                LLM_TOKEN_COUNT_TOTAL,
                agent.monitor.total_input_token_count + agent.monitor.total_output_token_count,
            )
            span.set_status(trace_api.StatusCode.OK)
            span.set_attribute(OUTPUT_VALUE, str(agent_output))
        return agent_output


class _StepWrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        agent = instance
        span_name = f"Step {agent.step_number}"
        with self._tracer.start_as_current_span(
            span_name,
            attributes={
                FI_SPAN_KIND: CHAIN,
                INPUT_VALUE: _get_input_value(wrapped, *args, **kwargs),
                **dict(get_attributes_from_context()),
                **dict(_get_raw_input(args, **kwargs)),
            },
        ) as span:
            result = wrapped(*args, **kwargs)
            step_log = args[0]  # ActionStep
            span.set_attribute(RAW_OUTPUT, _get_raw_output(result))
            span.set_attribute(OUTPUT_VALUE, step_log.observations)
            if step_log.error is not None:
                span.record_exception(step_log.error)
            span.set_status(trace_api.StatusCode.OK)
        return result


def _llm_input_messages(messages: list[Any]) -> Iterator[Tuple[str, Any]]:
    for i, message in enumerate(messages):
        content = message.get("content")
        if content:
            if isinstance(content, str):
                yield f"{LLM_INPUT_MESSAGES}.{i}.{MESSAGE_CONTENT}", content
            elif isinstance(content, list):
                for index, subcontent in enumerate(content):
                    if subcontent.get("type") == "text":
                        yield f"{LLM_INPUT_MESSAGES}.{i}.{MESSAGE_CONTENT}.{index}.{MESSAGE_CONTENT_TYPE}", "text"
                        yield f"{LLM_INPUT_MESSAGES}.{i}.{MESSAGE_CONTENT}.{index}.{MESSAGE_CONTENT_TEXT}", subcontent.get("text")
                    elif subcontent.get("type") == "image_url":
                        if image_url := subcontent.get("image_url"):
                            yield f"{LLM_INPUT_MESSAGES}.{i}.{MESSAGE_CONTENT}.{index}.{MESSAGE_CONTENT_TYPE}", "image"
                            yield f"{LLM_INPUT_MESSAGES}.{i}.{MESSAGE_CONTENT}.{index}.{MESSAGE_CONTENT_IMAGE}", image_url.get("url")
                    elif subcontent.get("type") == "input_audio":
                        if input_audio := subcontent.get("input_audio"):
                            yield f"{LLM_INPUT_MESSAGES}.{i}.{MESSAGE_CONTENT}.{index}.{MESSAGE_CONTENT_TYPE}", "audio"
                            yield f"{LLM_INPUT_MESSAGES}.{i}.{MESSAGE_CONTENT}.{index}.{MESSAGE_CONTENT_AUDIO}", input_audio.get("data")

            yield f"{LLM_INPUT_MESSAGES}.{i}.{MESSAGE_ROLE}", message.get("role", "")


def _llm_output_messages(output_message: Any) -> Iterator[Tuple[str, Any]]:
    if (role := getattr(output_message, "role", None)) is not None:
        yield (
            f"{LLM_OUTPUT_MESSAGES}.0.{MESSAGE_ROLE}",
            role,
        )
    if (content := getattr(output_message, "content", None)) is not None:
        yield (
            f"{LLM_OUTPUT_MESSAGES}.0.{MESSAGE_CONTENT}",
            content,
        )
    if isinstance(tool_calls := getattr(output_message, "tool_calls", None), list):
        for tool_call_index, tool_call in enumerate(tool_calls):
            if (tool_call_id := getattr(tool_call, "id", None)) is not None:
                yield (
                    f"{LLM_OUTPUT_MESSAGES}.0.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_ID}",
                    tool_call_id,
                )
            if (function := getattr(tool_call, "function", None)) is not None:
                if (name := getattr(function, "name", None)) is not None:
                    yield (
                        f"{LLM_OUTPUT_MESSAGES}.0.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_FUNCTION_NAME}",
                        name,
                    )
                if isinstance(arguments := getattr(function, "arguments", None), dict):
                    yield (
                        f"{LLM_OUTPUT_MESSAGES}.0.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                        safe_json_dumps(arguments),
                    )


def _output_value_and_mime_type(output: Any) -> Iterator[Tuple[str, Any]]:
    yield OUTPUT_MIME_TYPE, JSON
    yield OUTPUT_VALUE, output.model_dump_json()


def _llm_invocation_parameters(
    model: Any, arguments: Mapping[str, Any]
) -> Iterator[Tuple[str, Any]]:
    model_kwargs = _ if isinstance(_ := getattr(model, "kwargs", {}), dict) else {}
    kwargs = _ if isinstance(_ := arguments.get("kwargs"), dict) else {}
    yield LLM_INVOCATION_PARAMETERS, safe_json_dumps(model_kwargs | kwargs)


def _llm_tools(tools_to_call_from: list[Any]) -> Iterator[Tuple[str, Any]]:
    from smolagents import Tool
    from smolagents.models import get_tool_json_schema

    if not isinstance(tools_to_call_from, list):
        return
    for tool_index, tool in enumerate(tools_to_call_from):
        if isinstance(tool, Tool):
            yield (
                f"{LLM_TOOLS}.{tool_index}.{TOOL_JSON_SCHEMA}",
                safe_json_dumps(get_tool_json_schema(tool)),
            )


def _tools(tool: "Tool") -> Iterator[Tuple[str, Any]]:
    if tool_name := getattr(tool, "name", None):
        yield TOOL_NAME, tool_name
    if tool_description := getattr(tool, "description", None):
        yield TOOL_DESCRIPTION, tool_description
    yield TOOL_PARAMETERS, safe_json_dumps(tool.inputs)


def _input_value_and_mime_type(arguments: Mapping[str, Any]) -> Iterator[Tuple[str, Any]]:
    yield INPUT_MIME_TYPE, JSON
    yield INPUT_VALUE, safe_json_dumps(arguments)


class _ModelWrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        arguments = _bind_arguments(wrapped, *args, **kwargs)
        span_name = f"{instance.__class__.__name__}.__call__"
        model = instance
        with self._tracer.start_as_current_span(
            span_name,
            attributes={
                FI_SPAN_KIND: LLM,
                **dict(_input_value_and_mime_type(arguments)),
                **dict(_llm_invocation_parameters(instance, arguments)),
                **dict(_llm_input_messages(kwargs.get("messages", []))),
                **dict(get_attributes_from_context()),
                **dict(_get_raw_input(args, **kwargs)),
            },
        ) as span:
            output_message = wrapped(*args, **kwargs)
            span.set_status(trace_api.StatusCode.OK)
            span.set_attribute(RAW_OUTPUT, _get_raw_output(output_message))
            span.set_attribute(LLM_TOKEN_COUNT_PROMPT, model.last_input_token_count)
            span.set_attribute(LLM_TOKEN_COUNT_COMPLETION, model.last_output_token_count)
            span.set_attribute(LLM_MODEL_NAME, model.model_id)
            span.set_attribute(
                LLM_TOKEN_COUNT_TOTAL, model.last_input_token_count + model.last_output_token_count
            )
            span.set_attributes(dict(_llm_output_messages(output_message)))
            span.set_attributes(dict(_llm_tools(arguments.get("tools_to_call_from", []))))
            span.set_attributes(dict(_output_value_and_mime_type(output_message)))
        return output_message


class _ToolCallWrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        span_name = f"{instance.__class__.__name__}"
        with self._tracer.start_as_current_span(
            span_name,
            attributes={
                FI_SPAN_KIND: TOOL,
                INPUT_VALUE: _get_input_value(
                    wrapped,
                    *args,
                    **kwargs,
                ),
                **dict(_tools(instance)),
                **dict(get_attributes_from_context()),
                **dict(_get_raw_input(args, **kwargs)),
            },
        ) as span:
            response = wrapped(*args, **kwargs)
            span.set_attribute(RAW_OUTPUT, _get_raw_output(response))
            span.set_status(trace_api.StatusCode.OK)
            span.set_attributes(
                dict(
                    _output_value_and_mime_type_for_tool_span(
                        response=response,
                        output_type=instance.output_type,
                    )
                )
            )
        return response


def _output_value_and_mime_type_for_tool_span(
    response: Any, output_type: str
) -> Iterator[Tuple[str, Any]]:
    if output_type in (
        "string",
        "boolean",
        "integer",
        "number",
    ):
        yield OUTPUT_VALUE, response
        yield OUTPUT_MIME_TYPE, TEXT
    elif output_type == "object":
        yield OUTPUT_VALUE, safe_json_dumps(response)
        yield OUTPUT_MIME_TYPE, JSON

    # TODO: handle other types


def _get_raw_input(args: Any, **kwargs: Any) -> Iterator[Tuple[str, Any]]:
    raw_input = safe_json_dumps(
        {
            "args": _to_dict(args),
            **(_to_dict(kwargs) or {})
        }
    )
    
    yield RAW_INPUT, raw_input

def _get_raw_output(response: Any):
    raw_output = _to_dict(response)
    
    return safe_json_dumps(raw_output or {})


def _to_dict(result: Any) -> Any:
    if not result:
        return {}
    # this method has some Json serializability issue in the Smolagents library
    # if isinstance(result, ChatMessage):
    #     return result.dict()
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

# span attributes
INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
LLM_INPUT_MESSAGES = SpanAttributes.LLM_INPUT_MESSAGES
LLM_INVOCATION_PARAMETERS = SpanAttributes.LLM_INVOCATION_PARAMETERS
LLM_MODEL_NAME = SpanAttributes.LLM_MODEL_NAME
LLM_OUTPUT_MESSAGES = SpanAttributes.LLM_OUTPUT_MESSAGES
LLM_PROMPTS = SpanAttributes.LLM_PROMPTS
LLM_TOKEN_COUNT_COMPLETION = SpanAttributes.LLM_TOKEN_COUNT_COMPLETION
LLM_TOKEN_COUNT_PROMPT = SpanAttributes.LLM_TOKEN_COUNT_PROMPT
LLM_TOKEN_COUNT_TOTAL = SpanAttributes.LLM_TOKEN_COUNT_TOTAL
LLM_TOOLS = SpanAttributes.LLM_TOOLS
FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
TOOL_DESCRIPTION = SpanAttributes.TOOL_DESCRIPTION
TOOL_NAME = SpanAttributes.TOOL_NAME
TOOL_PARAMETERS = SpanAttributes.TOOL_PARAMETERS
RAW_INPUT = SpanAttributes.RAW_INPUT
RAW_OUTPUT = SpanAttributes.RAW_OUTPUT

# message attributes
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON
MESSAGE_FUNCTION_CALL_NAME = MessageAttributes.MESSAGE_FUNCTION_CALL_NAME
MESSAGE_NAME = MessageAttributes.MESSAGE_NAME
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE
MESSAGE_TOOL_CALLS = MessageAttributes.MESSAGE_TOOL_CALLS

# message content attributes
MESSAGE_CONTENT_TYPE = MessageContentAttributes.MESSAGE_CONTENT_TYPE
MESSAGE_CONTENT_IMAGE = MessageContentAttributes.MESSAGE_CONTENT_IMAGE
MESSAGE_CONTENT_TEXT = MessageContentAttributes.MESSAGE_CONTENT_TEXT
MESSAGE_CONTENT_AUDIO = MessageContentAttributes.MESSAGE_CONTENT_AUDIO
MESSAGE_AUDIO_TRANSCRIPT = MessageContentAttributes.MESSAGE_AUDIO_TRANSCRIPT


# mime types
JSON = FiMimeTypeValues.JSON.value
TEXT = FiMimeTypeValues.TEXT.value

# span kinds
AGENT = FiSpanKindValues.AGENT.value
CHAIN = FiSpanKindValues.CHAIN.value
LLM = FiSpanKindValues.LLM.value
TOOL = FiSpanKindValues.TOOL.value

# tool attributes
TOOL_JSON_SCHEMA = ToolAttributes.TOOL_JSON_SCHEMA

# tool call attributes
TOOL_CALL_FUNCTION_ARGUMENTS_JSON = ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON
TOOL_CALL_FUNCTION_NAME = ToolCallAttributes.TOOL_CALL_FUNCTION_NAME
TOOL_CALL_ID = ToolCallAttributes.TOOL_CALL_ID
