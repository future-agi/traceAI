# Adapted from OpenInference Agno 1.1.0 (Arize AI), Apache-2.0.
# Modified by Future AGI; see NOTICE and LICENSE in this directory.

import asyncio
import json
import threading
from contextlib import aclosing, closing
from dataclasses import fields, is_dataclass
from enum import Enum
from inspect import Parameter, signature
from os import getenv
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    Mapping,
    Optional,
    OrderedDict,
    Tuple,
)

from agno.models.base import Model
from openinference.instrumentation import (
    get_attributes_from_context,
    infer_llm_system_from_model_name,
    safe_json_dumps,
)
from openinference.semconv.trace import (
    MessageAttributes,
    MessageContentAttributes,
    OpenInferenceLLMSystemValues,
    OpenInferenceMimeTypeValues,
    OpenInferenceSpanKindValues,
    SpanAttributes,
    ToolAttributes,
    ToolCallAttributes,
)
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue
from pydantic import BaseModel

from ._stream import aiterate, finish_span, iterate, span_scope

_MODEL_INVOCATION_KEY = context_api.create_key("traceai_agno.model_invocation")


def _invocation_owner() -> Any:
    # Context is inherited by child tasks, but their requests are independent.
    try:
        return asyncio.current_task() or threading.current_thread()
    except RuntimeError:
        return threading.current_thread()


def _invocation_context(instance: Any, operation: str, wrapped: Any) -> Dict[str, Any]:
    return {
        _MODEL_INVOCATION_KEY: (
            instance,
            operation,
            _invocation_owner(),
            getattr(wrapped, "__func__", wrapped),
        )
    }


def _is_delegated(instance: Any, operation: str, wrapped: Any) -> bool:
    active = context_api.get_value(_MODEL_INVOCATION_KEY)
    return (
        isinstance(active, tuple)
        and active[0] is instance
        and active[1] == operation
        and active[2] is _invocation_owner()
        # Re-entering the same implementation is a separate request, not super().
        and active[3] is not getattr(wrapped, "__func__", wrapped)
    )


def _get_gemini_vertexai_mode(model: Model) -> Optional[bool]:
    """Return whether an Agno Gemini model uses Vertex AI."""
    # Agno returns a prebuilt client without consulting its other configuration.
    client = getattr(model, "client", None)
    if client is not None:
        client_vertexai = getattr(client, "vertexai", None)
        return client_vertexai if isinstance(client_vertexai, bool) else None

    vertexai_env = getenv("GOOGLE_GENAI_USE_VERTEXAI", "false").lower()
    vertexai_mode: Optional[bool] = (
        True
        if bool(getattr(model, "vertexai", False)) or vertexai_env == "true"
        else None
    )
    client_params = getattr(model, "client_params", None)
    # Agno applies client_params last, so an explicit value overrides the flag.
    if isinstance(client_params, Mapping) and "vertexai" in client_params:
        client_param_vertexai = client_params["vertexai"]
        if client_param_vertexai is None:
            vertexai_mode = None
        elif isinstance(client_param_vertexai, bool):
            vertexai_mode = client_param_vertexai
        else:
            return None
    if vertexai_mode is not None:
        return vertexai_mode
    return vertexai_env in {"true", "1"}


def _get_llm_system(model: Model) -> Optional[str]:
    """Derive the OpenInference ``llm.system`` value from an Agno model."""
    system = infer_llm_system_from_model_name(model.id or "")
    if system is None:
        return None
    if system is OpenInferenceLLMSystemValues.VERTEXAI and (
        hasattr(model, "vertexai") or getattr(model, "provider", None) == "Google"
    ):
        if (vertexai := _get_gemini_vertexai_mode(model)) is None:
            return None
        return "vertexai" if vertexai else "google"
    return system.value


def _get_attr(obj: Any, key: str, default: Any = None) -> Any:
    """Helper function to get attribute from either dict or object."""
    if obj is None:
        return default
    if isinstance(obj, dict):  # It's a dict
        return obj.get(key, default)
    else:  # It's an object with attributes
        return getattr(obj, key, default)


def _flatten(
    mapping: Optional[Mapping[str, Any]],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not mapping:
        return
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            for sub_key, sub_value in _flatten(value):
                yield f"{key}.{sub_key}", sub_value
        elif isinstance(value, list) and any(
            isinstance(item, Mapping) for item in value
        ):
            for index, sub_mapping in enumerate(value):
                for sub_key, sub_value in _flatten(sub_mapping):
                    yield f"{key}.{index}.{sub_key}", sub_value
        else:
            if isinstance(value, Enum):
                value = value.value
            yield key, value


def _bind_arguments(
    method: Callable[..., Any], *args: Any, **kwargs: Any
) -> Dict[str, Any]:
    method_signature = signature(method)
    bound_args = method_signature.bind(*args, **kwargs)
    instance = getattr(method, "__self__", None)
    if (
        args
        and instance is not None
        and any(
            parameter.kind == Parameter.VAR_POSITIONAL
            for parameter in method_signature.parameters.values()
        )
    ):
        # A delegating override can hide positional parameter names behind *args.
        # Use the first compatible concrete base signature, without calling it.
        for cls in type(instance).__mro__:
            candidate = cls.__dict__.get(method.__name__)
            if candidate is None:
                continue
            try:
                candidate_signature = signature(candidate)
                if any(
                    p.kind == Parameter.VAR_POSITIONAL
                    for p in candidate_signature.parameters.values()
                ):
                    continue
                candidate_arguments = candidate_signature.bind(
                    instance, *args, **kwargs
                )
            except (TypeError, ValueError):
                continue
            method_signature, bound_args = candidate_signature, candidate_arguments
            break
    bound_args.apply_defaults()
    arguments: Dict[str, Any] = {}
    for key, value in bound_args.arguments.items():
        if key == "self" and value is instance:
            continue
        # Delegating provider overrides (e.g. MiniMax) accept **kwargs instead
        # of repeating the base signature. Keep the outer span's real inputs.
        if method_signature.parameters[key].kind == Parameter.VAR_KEYWORD:
            arguments.update(value)
        else:
            arguments[key] = value
    return OrderedDict(
        (key, value)
        for key, value in arguments.items()
        if value is not None and value != {}
    )


def _llm_input_messages(arguments: Mapping[str, Any]) -> Iterator[Tuple[str, Any]]:
    def process_message(idx: int, message: Any) -> Iterator[Tuple[str, Any]]:
        yield f"{LLM_INPUT_MESSAGES}.{idx}.{MESSAGE_ROLE}", message.role
        # tool_call_id links a role=tool result message back to the original tool call request
        if message.tool_call_id:
            yield (
                f"{LLM_INPUT_MESSAGES}.{idx}.{MESSAGE_TOOL_CALL_ID}",
                message.tool_call_id,
            )
        if message.content:
            yield (
                f"{LLM_INPUT_MESSAGES}.{idx}.{MESSAGE_CONTENT}",
                message.get_content_string(),
            )
        if message.tool_calls:
            for tool_call_index, tool_call in enumerate(message.tool_calls):
                yield (
                    f"{LLM_INPUT_MESSAGES}.{idx}.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_ID}",
                    _get_attr(tool_call, "id"),
                )
                function_obj = _get_attr(tool_call, "function", {})
                yield (
                    f"{LLM_INPUT_MESSAGES}.{idx}.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_FUNCTION_NAME}",
                    _get_attr(function_obj, "name"),
                )
                arguments = _get_attr(function_obj, "arguments")
                yield (
                    f"{LLM_INPUT_MESSAGES}.{idx}.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                    json.dumps(arguments)
                    if isinstance(arguments, dict)
                    else (arguments or "{}"),
                )

    messages = arguments.get("messages", [])
    for i, message in enumerate(messages):
        if message.role in ["system", "user", "assistant", "tool"]:
            yield from process_message(i, message)
    tools = arguments.get("tools", [])
    for tool_index, tool in enumerate(tools):
        yield (
            f"{LLM_TOOLS}.{tool_index}.{TOOL_JSON_SCHEMA}",
            safe_json_dumps(_filter_sensitive_params(tool)),
        )


def _llm_invocation_parameters(
    model: Model,
    arguments: Optional[Mapping[str, Any]] = None,
    *,
    hide_inputs: bool = False,
) -> Iterator[Tuple[str, Any]]:
    # Provider parameters can contain prompts, tools, schemas, or opaque payloads.
    # No cross-provider denylist can reliably separate these from model settings.
    if hide_inputs:
        return
    request_kwargs = {}
    # TODO (v2.0.0): with the cleanup of the agno.models.base.Model class we will
    # handle these attributes in a more consistent way.
    if getattr(model, "request_kwargs", None):
        request_kwargs = model.request_kwargs  # type: ignore[attr-defined]
    if getattr(model, "request_params", None):
        request_kwargs = model.request_params  # type: ignore[attr-defined]
    if getattr(model, "get_request_kwargs", None):
        request_kwargs = model.get_request_kwargs()  # type: ignore[attr-defined]
    if getattr(model, "get_request_params", None):
        # Special handling for OpenAIResponses model
        if model.__class__.__name__ == "OpenAIResponses" and arguments:
            messages = arguments.get("messages", [])
            request_kwargs = model.get_request_params(messages=messages)  # type: ignore[attr-defined]
        else:
            request_kwargs = model.get_request_params()  # type: ignore[attr-defined]

    if request_kwargs:
        filtered_kwargs = _filter_sensitive_params(request_kwargs)
        yield LLM_INVOCATION_PARAMETERS, safe_json_dumps(filtered_kwargs)


def _filter_sensitive_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    """Redact credentials recursively without modifying the model's request.

    Header containers are omitted wholesale: gateway authentication can use
    arbitrary custom header names, not only Authorization or X-Api-Key.
    """
    sensitive = (
        "apikey",
        "apibase",
        "accesskey",
        "secret",
        "password",
        "credential",
        "authorization",
        "cookie",
        "accesstoken",
        "authtoken",
        "bearertoken",
        "refreshtoken",
        "azureadtoken",
        "headers",
        "azureendpoint",
        "azuredeployment",
    )

    def is_sensitive(key: Any) -> bool:
        normalized = str(key).lower().replace("_", "").replace("-", "")
        return normalized in ("token", "idtoken") or any(
            part in normalized for part in sensitive
        )

    ancestors: set[int] = set()

    def clean(value: Any) -> Any:
        if isinstance(value, Enum):
            return clean(value.value)
        if value is None or type(value) in (str, int, float, bool):
            return value
        identity = id(value)
        if identity in ancestors or len(ancestors) >= 32:
            return "[REDACTED]"
        ancestors.add(identity)
        try:
            if isinstance(value, BaseModel):
                # Unlike model_dump(mode="json"), iteration preserves raw nested
                # values and does not invoke serializers before credential filtering.
                return clean(dict(value))
            if is_dataclass(value) and not isinstance(value, type):
                return clean(
                    {field.name: getattr(value, field.name) for field in fields(value)}
                )
            if isinstance(value, Mapping):
                return {
                    key: "[REDACTED]" if is_sensitive(key) else clean(item)
                    for key, item in value.items()
                    if isinstance(key, str)
                }
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            # Never fall back to repr/str: opaque clients/configurations can embed
            # authentication in their string representation.
            return "[REDACTED]"
        finally:
            ancestors.remove(identity)

    return clean(params)


def _input_value_and_mime_type(
    arguments: Mapping[str, Any],
) -> Iterator[Tuple[str, Any]]:
    yield INPUT_MIME_TYPE, JSON

    cleaned_input = []
    for message in arguments.get("messages", []):
        message_dict = message.to_dict()
        message_dict = {k: v for k, v in message_dict.items() if v is not None}
        cleaned_input.append(message_dict)
    yield INPUT_VALUE, safe_json_dumps({"messages": cleaned_input})


def _message_content_attributes(
    message_index: int, content: Optional[str], reasoning_content: Optional[str]
) -> Iterator[Tuple[str, Any]]:
    """Yield attributes for content and reasoning_conent of a single output message."""
    if reasoning_content:
        content_index = 0
        yield (
            f"{LLM_OUTPUT_MESSAGES}.{message_index}.{MESSAGE_CONTENTS}.{content_index}.{MESSAGE_CONTENT_TYPE}",
            "reasoning",
        )
        yield (
            f"{LLM_OUTPUT_MESSAGES}.{message_index}.{MESSAGE_CONTENTS}.{content_index}.{MESSAGE_CONTENT_TEXT}",
            reasoning_content,
        )
        content_index += 1
        if content:
            yield (
                f"{LLM_OUTPUT_MESSAGES}.{message_index}.{MESSAGE_CONTENTS}.{content_index}.{MESSAGE_CONTENT_TYPE}",
                "text",
            )
            yield (
                f"{LLM_OUTPUT_MESSAGES}.{message_index}.{MESSAGE_CONTENTS}.{content_index}.{MESSAGE_CONTENT_TEXT}",
                content,
            )
        if content:
            yield f"{LLM_OUTPUT_MESSAGES}.{message_index}.{MESSAGE_CONTENT}", content
    elif content:
        yield f"{LLM_OUTPUT_MESSAGES}.{message_index}.{MESSAGE_CONTENT}", content


def _output_value_and_mime_type(output: str) -> Iterator[Tuple[str, Any]]:
    yield OUTPUT_MIME_TYPE, JSON

    # Try to parse the output and extract LLM_OUTPUT_MESSAGES
    try:
        output_data = json.loads(output)
        if isinstance(output_data, dict):
            # Extract message information for LLM_OUTPUT_MESSAGES (only core message fields)
            messages = []
            message = {}

            if role := output_data.get("role"):
                message["role"] = role

            if content := output_data.get("content"):
                message["content"] = content

            if reasoning_content := output_data.get("reasoning_content"):
                message["reasoning_content"] = reasoning_content

            # Only include tool_calls if they exist and are not empty
            if tool_calls := output_data.get("tool_calls"):
                if tool_calls:  # Only include if not empty list
                    message["tool_calls"] = tool_calls

            messages.append(message)
            for i, message in enumerate(messages):
                if role := _get_attr(message, "role"):
                    yield f"{LLM_OUTPUT_MESSAGES}.{i}.{MESSAGE_ROLE}", role

                yield from _message_content_attributes(
                    i,
                    _get_attr(message, "content"),
                    _get_attr(message, "reasoning_content"),
                )

                if tool_calls := _get_attr(message, "tool_calls"):
                    for tool_call_index, tool_call in enumerate(tool_calls):
                        yield (
                            f"{LLM_OUTPUT_MESSAGES}.{i}.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_ID}",
                            _get_attr(tool_call, "id"),
                        )
                        function_obj = _get_attr(tool_call, "function", {})
                        yield (
                            f"{LLM_OUTPUT_MESSAGES}.{i}.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_FUNCTION_NAME}",
                            _get_attr(function_obj, "name"),
                        )
                        arguments = _get_attr(function_obj, "arguments")
                        yield (
                            f"{LLM_OUTPUT_MESSAGES}.{i}.{MESSAGE_TOOL_CALLS}.{tool_call_index}.{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                            json.dumps(arguments)
                            if isinstance(arguments, dict)
                            else (arguments or "{}"),
                        )

            yield OUTPUT_VALUE, safe_json_dumps(messages)

    except (json.JSONDecodeError, TypeError):
        # Fall back to the original output if parsing fails
        yield OUTPUT_VALUE, output


def _parse_model_output(output: Any) -> str:
    if (
        hasattr(output, "role")
        or hasattr(output, "content")
        or hasattr(output, "tool_calls")
    ):
        try:
            result_dict = {
                "created_at": getattr(output, "created_at", None),
            }

            if hasattr(output, "role"):
                result_dict["role"] = output.role
            if hasattr(output, "content"):
                result_dict["content"] = output.content
            if hasattr(output, "reasoning_content") and output.reasoning_content:
                result_dict["reasoning_content"] = output.reasoning_content
            if hasattr(output, "tool_calls"):
                result_dict["tool_calls"] = output.tool_calls

            # Add response_usage if available
            if hasattr(output, "response_usage") and output.response_usage:
                result_dict["response_usage"] = {
                    "input_tokens": getattr(
                        output.response_usage, "input_tokens", None
                    ),
                    "output_tokens": getattr(
                        output.response_usage, "output_tokens", None
                    ),
                    "total_tokens": getattr(
                        output.response_usage, "total_tokens", None
                    ),
                }

            return json.dumps(result_dict)
        except Exception:
            pass

    return json.dumps(output) if isinstance(output, dict) else str(output)


def _parse_model_output_stream(output: Any) -> Dict[str, Any]:
    accumulated_content = ""
    accumulated_reasoning = ""
    indexed_tool_calls: Dict[int, Dict[str, Any]] = {}
    non_indexed_tool_calls: list[Dict[str, Any]] = []

    for chunk in output:
        if chunk.content is not None and chunk.content != "":
            accumulated_content += str(chunk.content)

        reasoning_chunk = getattr(chunk, "reasoning_content", None)
        if reasoning_chunk:
            accumulated_reasoning += str(reasoning_chunk)

        if chunk.tool_calls:
            for tool_call in chunk.tool_calls:
                index = _get_attr(tool_call, "index")

                if index is not None:
                    # OpenAI sends args as fragments keyed by index
                    if index not in indexed_tool_calls:
                        indexed_tool_calls[index] = {
                            "id": None,
                            "type": None,
                            "function": {"name": None, "arguments": ""},
                        }
                    entry = indexed_tool_calls[index]
                    if _get_attr(tool_call, "id"):
                        entry["id"] = _get_attr(tool_call, "id")
                    if _get_attr(tool_call, "type"):
                        entry["type"] = _get_attr(tool_call, "type")
                    function_obj = _get_attr(tool_call, "function")
                    if function_obj:
                        if _get_attr(function_obj, "name"):
                            entry["function"]["name"] = _get_attr(function_obj, "name")
                        entry["function"]["arguments"] += (
                            _get_attr(function_obj, "arguments") or ""
                        )
                else:
                    # Anthropic / Gemini send complete tool call in one chunk
                    if _get_attr(tool_call, "id"):
                        tool_call_dict: Dict[str, Any] = {
                            "id": _get_attr(tool_call, "id"),
                            "type": _get_attr(tool_call, "type"),
                        }
                        function_obj = _get_attr(tool_call, "function")
                        if function_obj:
                            tool_call_dict["function"] = {
                                "name": _get_attr(function_obj, "name"),
                                "arguments": _get_attr(function_obj, "arguments"),
                            }
                        non_indexed_tool_calls.append(tool_call_dict)

    all_tool_calls: list[Dict[str, Any]] = [
        indexed_tool_calls[i] for i in sorted(indexed_tool_calls)
    ]
    all_tool_calls.extend(non_indexed_tool_calls)

    messages: list[Dict[str, Any]] = []
    if accumulated_content or accumulated_reasoning or all_tool_calls:
        result_dict: Dict[str, Any] = {"role": "assistant"}

        if accumulated_content:
            result_dict["content"] = accumulated_content

        if accumulated_reasoning:
            result_dict["reasoning_content"] = accumulated_reasoning

        if all_tool_calls:
            result_dict["tool_calls"] = all_tool_calls

        messages.append(result_dict)

    return {"messages": messages}


def _stream_output_messages(output: Dict[str, Any]) -> Iterator[Tuple[str, Any]]:
    for i, message in enumerate(output.get("messages", [])):
        if role := _get_attr(message, "role"):
            yield f"{LLM_OUTPUT_MESSAGES}.{i}.{MESSAGE_ROLE}", role

        yield from _message_content_attributes(
            i,
            _get_attr(message, "content"),
            _get_attr(message, "reasoning_content"),
        )

        if tool_calls := _get_attr(message, "tool_calls"):
            for j, tool_call in enumerate(tool_calls):
                if tc_id := _get_attr(tool_call, "id"):
                    yield (
                        f"{LLM_OUTPUT_MESSAGES}.{i}.{MESSAGE_TOOL_CALLS}.{j}.{TOOL_CALL_ID}",
                        tc_id,
                    )
                function_obj = _get_attr(tool_call, "function", {})
                if fn_name := _get_attr(function_obj, "name"):
                    yield (
                        f"{LLM_OUTPUT_MESSAGES}.{i}.{MESSAGE_TOOL_CALLS}.{j}.{TOOL_CALL_FUNCTION_NAME}",
                        fn_name,
                    )
                fn_args = _get_attr(function_obj, "arguments")
                if fn_args is not None:
                    yield (
                        f"{LLM_OUTPUT_MESSAGES}.{i}.{MESSAGE_TOOL_CALLS}.{j}.{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                        fn_args,
                    )


def _usage_attributes(metrics: Any) -> Iterator[Tuple[str, Any]]:
    """Keep billing inputs consistent across sync/async and streaming calls.

    Preserve explicit zero values; absent fields stay absent. Cost is reported
    only when supplied by Agno, never estimated from a model's pricing table.
    """
    for field, attribute in _USAGE_ATTRIBUTES:
        if (value := getattr(metrics, field, None)) is not None:
            yield attribute, value


class _ModelWrapper:
    def __init__(
        self,
        tracer: trace_api.Tracer,
        *,
        hide_inputs: bool = False,
        attachments: Any = None,
        trace_configs: Tuple[Any, ...] = (),
    ) -> None:
        self._tracer = tracer
        self._hide_inputs = hide_inputs
        # The shared, provider-owned attachment manager (already checked active), or None.
        # trace_configs are the FI and OpenInference configs: either one hiding wins.
        self._attachments = attachments
        self._trace_configs = trace_configs

    def _capture_input(self, span: Any, arguments: Mapping[str, Any]) -> None:
        # Input media is captured before the provider call; this span owns its own occurrences.
        if self._attachments is None:
            return
        try:
            from traceai_agno._attachments import capture_agno_input

            capture_agno_input(
                self._attachments, span, arguments.get("messages"),
                trace_configs=self._trace_configs,
            )
        except Exception:
            pass

    def _owned(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """``values`` plus the owner mark while attachments are enabled (one call, one owner).

        span_scope attaches it only around the downstream call and each stream advance,
        so the provider instrumentor beneath Agno skips this call's media. Attachments
        off, or a call already owned further out: ``values`` unchanged.
        """
        if self._attachments is None:
            return values
        try:
            from fi_instrumentation.attachments.media_capture import OWNER_KEY, call_owner

            owner = call_owner(self._attachments)
        except Exception:
            return values
        return values if owner is None else {**values, OWNER_KEY: owner}

    def _capture_output(self, span: Any, response: Any) -> None:
        if self._attachments is None:
            return
        try:
            from traceai_agno._attachments import capture_agno_output

            capture_agno_output(
                self._attachments, span, response, trace_configs=self._trace_configs
            )
        except Exception:
            pass

    def run(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(
            context_api._SUPPRESS_INSTRUMENTATION_KEY
        ) or _is_delegated(instance, "invoke", wrapped):
            return wrapped(*args, **kwargs)

        arguments = _bind_arguments(wrapped, *args, **kwargs)

        model = instance
        model_name = model.name
        span_name = f"{model_name}.invoke"

        with self._tracer.start_as_current_span(
            span_name,
            attributes={
                OPENINFERENCE_SPAN_KIND: LLM,
                **dict(_input_value_and_mime_type(arguments)),
                **dict(_llm_input_messages(arguments)),
                **dict(
                    _llm_invocation_parameters(
                        model, arguments, hide_inputs=self._hide_inputs
                    )
                ),
                **dict(get_attributes_from_context()),
            },
        ) as span:
            span.set_attribute(LLM_MODEL_NAME, model.id)
            span.set_attribute(LLM_PROVIDER, model.provider)
            if llm_system := _get_llm_system(model):
                span.set_attribute(LLM_SYSTEM, llm_system)
            self._capture_input(span, arguments)

            with span_scope(span, self._owned(_invocation_context(instance, "invoke", wrapped))):
                response = wrapped(*args, **kwargs)
            output_message = _parse_model_output(response)
            self._capture_output(span, response)
            span.set_attributes(dict(_output_value_and_mime_type(output_message)))

            span.set_attributes(
                dict(_usage_attributes(getattr(response, "response_usage", None)))
            )

            span.set_status(trace_api.StatusCode.OK)
            return response

    def run_stream(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(
            context_api._SUPPRESS_INSTRUMENTATION_KEY
        ) or _is_delegated(instance, "invoke_stream", wrapped):
            yield from wrapped(*args, **kwargs)
            return

        arguments = _bind_arguments(wrapped, *args, **kwargs)
        model = instance
        model_name = model.name
        span_name = f"{model_name}.invoke_stream"

        with finish_span(
            self._tracer.start_span(
                span_name,
                attributes={
                    OPENINFERENCE_SPAN_KIND: LLM,
                    **dict(_input_value_and_mime_type(arguments)),
                    **dict(
                        _llm_invocation_parameters(
                            model, arguments, hide_inputs=self._hide_inputs
                        )
                    ),
                    **dict(_llm_input_messages(arguments)),
                    **dict(get_attributes_from_context()),
                },
            )
        ) as span:
            span.set_attribute(LLM_MODEL_NAME, model.id)
            span.set_attribute(LLM_PROVIDER, model.provider)
            if llm_system := _get_llm_system(model):
                span.set_attribute(LLM_SYSTEM, llm_system)
            self._capture_input(span, arguments)
            # Token usage will be set after streaming completes based on final response

            responses = []
            values = self._owned(_invocation_context(instance, "invoke_stream", wrapped))
            with span_scope(span, values):
                source = iter(wrapped(*args, **kwargs))
            with closing(iterate(source, span, values)) as iterator:
                for chunk in iterator:
                    responses.append(chunk)
                    yield chunk

            output_message_dict = _parse_model_output_stream(responses)
            output_message = json.dumps(output_message_dict)
            span.set_attribute(OUTPUT_MIME_TYPE, JSON)
            span.set_attribute(OUTPUT_VALUE, output_message)
            span.set_attributes(dict(_stream_output_messages(output_message_dict)))

            # Agno's final usage-bearing event contains cumulative metrics.
            for response in reversed(responses):
                if (metrics := getattr(response, "response_usage", None)) is not None:
                    span.set_attributes(dict(_usage_attributes(metrics)))
                    break

            span.set_status(trace_api.StatusCode.OK)

    async def arun(
        self,
        wrapped: Callable[..., Awaitable[Any]],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(
            context_api._SUPPRESS_INSTRUMENTATION_KEY
        ) or _is_delegated(instance, "ainvoke", wrapped):
            return await wrapped(*args, **kwargs)

        arguments = _bind_arguments(wrapped, *args, **kwargs)

        model = instance
        model_name = model.name
        span_name = f"{model_name}.ainvoke"

        with self._tracer.start_as_current_span(
            span_name,
            attributes={
                OPENINFERENCE_SPAN_KIND: LLM,
                **dict(_input_value_and_mime_type(arguments)),
                **dict(
                    _llm_invocation_parameters(
                        model, arguments, hide_inputs=self._hide_inputs
                    )
                ),
                **dict(_llm_input_messages(arguments)),
                **dict(get_attributes_from_context()),
            },
        ) as span:
            span.set_attribute(LLM_MODEL_NAME, model.id)
            span.set_attribute(LLM_PROVIDER, model.provider)
            if llm_system := _get_llm_system(model):
                span.set_attribute(LLM_SYSTEM, llm_system)
            self._capture_input(span, arguments)

            with span_scope(span, self._owned(_invocation_context(instance, "ainvoke", wrapped))):
                response = await wrapped(*args, **kwargs)
            output_message = _parse_model_output(response)
            self._capture_output(span, response)

            span.set_attributes(
                dict(_usage_attributes(getattr(response, "response_usage", None)))
            )

            span.set_attributes(dict(_output_value_and_mime_type(output_message)))
            span.set_status(trace_api.StatusCode.OK)
            return response

    async def arun_stream(
        self,
        wrapped: Callable[..., Awaitable[Any]],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(
            context_api._SUPPRESS_INSTRUMENTATION_KEY
        ) or _is_delegated(instance, "ainvoke_stream", wrapped):
            async for response in wrapped(*args, **kwargs):  # type: ignore[attr-defined]
                yield response
            return

        arguments = _bind_arguments(wrapped, *args, **kwargs)

        model = instance
        model_name = model.name
        span_name = f"{model_name}.ainvoke_stream"

        with finish_span(
            self._tracer.start_span(
                span_name,
                attributes={
                    OPENINFERENCE_SPAN_KIND: LLM,
                    **dict(_input_value_and_mime_type(arguments)),
                    **dict(
                        _llm_invocation_parameters(
                            model, arguments, hide_inputs=self._hide_inputs
                        )
                    ),
                    **dict(_llm_input_messages(arguments)),
                    **dict(get_attributes_from_context()),
                },
            )
        ) as span:
            span.set_attribute(LLM_MODEL_NAME, model.id)
            span.set_attribute(LLM_PROVIDER, model.provider)
            if llm_system := _get_llm_system(model):
                span.set_attribute(LLM_SYSTEM, llm_system)
            self._capture_input(span, arguments)
            # Token usage will be set after streaming completes based on final response

            responses = []
            values = self._owned(_invocation_context(instance, "ainvoke_stream", wrapped))
            with span_scope(span, values):
                source = wrapped(*args, **kwargs)
            async with aclosing(aiterate(source, span, values)) as iterator:
                async for chunk in iterator:
                    responses.append(chunk)
                    yield chunk

            output_message_dict = _parse_model_output_stream(responses)
            output_message = json.dumps(output_message_dict)
            span.set_attribute(OUTPUT_MIME_TYPE, JSON)
            span.set_attribute(OUTPUT_VALUE, output_message)
            span.set_attributes(dict(_stream_output_messages(output_message_dict)))

            # Agno's final usage-bearing event contains cumulative metrics.
            for response in reversed(responses):
                if (metrics := getattr(response, "response_usage", None)) is not None:
                    span.set_attributes(dict(_usage_attributes(metrics)))
                    break

            span.set_status(trace_api.StatusCode.OK)


# span attributes
INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
SESSION_ID = SpanAttributes.SESSION_ID
LLM_TOOLS = SpanAttributes.LLM_TOOLS
LLM_INPUT_MESSAGES = SpanAttributes.LLM_INPUT_MESSAGES
LLM_INVOCATION_PARAMETERS = SpanAttributes.LLM_INVOCATION_PARAMETERS
LLM_MODEL_NAME = SpanAttributes.LLM_MODEL_NAME
LLM_PROVIDER = SpanAttributes.LLM_PROVIDER
LLM_SYSTEM = SpanAttributes.LLM_SYSTEM
LLM_OUTPUT_MESSAGES = SpanAttributes.LLM_OUTPUT_MESSAGES
LLM_PROMPTS = SpanAttributes.LLM_PROMPTS
LLM_COST_TOTAL = SpanAttributes.LLM_COST_TOTAL
LLM_TOKEN_COUNT_COMPLETION = SpanAttributes.LLM_TOKEN_COUNT_COMPLETION
LLM_TOKEN_COUNT_PROMPT = SpanAttributes.LLM_TOKEN_COUNT_PROMPT
LLM_TOKEN_COUNT_TOTAL = SpanAttributes.LLM_TOKEN_COUNT_TOTAL
LLM_FUNCTION_CALL = SpanAttributes.LLM_FUNCTION_CALL
OPENINFERENCE_SPAN_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
USER_ID = SpanAttributes.USER_ID

# message attributes
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_CONTENT_TYPE = MessageContentAttributes.MESSAGE_CONTENT_TYPE
MESSAGE_CONTENT_TEXT = MessageContentAttributes.MESSAGE_CONTENT_TEXT
MESSAGE_CONTENTS = MessageAttributes.MESSAGE_CONTENTS
MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = (
    MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON
)
MESSAGE_FUNCTION_CALL_NAME = MessageAttributes.MESSAGE_FUNCTION_CALL_NAME
MESSAGE_NAME = MessageAttributes.MESSAGE_NAME
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE
MESSAGE_TOOL_CALL_ID = MessageAttributes.MESSAGE_TOOL_CALL_ID
MESSAGE_TOOL_CALLS = MessageAttributes.MESSAGE_TOOL_CALLS

# mime types
TEXT = OpenInferenceMimeTypeValues.TEXT.value
JSON = OpenInferenceMimeTypeValues.JSON.value

# tool attributes
TOOL_JSON_SCHEMA = ToolAttributes.TOOL_JSON_SCHEMA
TOOL_CALL_FUNCTION_ARGUMENTS_JSON = ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON
TOOL_CALL_FUNCTION_NAME = ToolCallAttributes.TOOL_CALL_FUNCTION_NAME
TOOL_CALL_ID = ToolCallAttributes.TOOL_CALL_ID

# span kinds
LLM = OpenInferenceSpanKindValues.LLM.value

LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ = (
    SpanAttributes.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ
)
LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE = (
    SpanAttributes.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE
)


_USAGE_ATTRIBUTES = (
    ("input_tokens", LLM_TOKEN_COUNT_PROMPT),
    ("output_tokens", LLM_TOKEN_COUNT_COMPLETION),
    ("total_tokens", LLM_TOKEN_COUNT_TOTAL),
    ("cache_read_tokens", LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ),
    ("cache_write_tokens", LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE),
    ("audio_input_tokens", SpanAttributes.LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO),
    ("audio_output_tokens", SpanAttributes.LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO),
    ("reasoning_tokens", SpanAttributes.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING),
    ("cost", LLM_COST_TOTAL),
)
