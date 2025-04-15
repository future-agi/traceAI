import json
from abc import ABC
from enum import Enum
from inspect import signature
from typing import Any, Callable, Iterator, List, Mapping, Optional, Tuple

import opentelemetry.context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue

from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import SpanAttributes, FiSpanKindValues, MessageAttributes


class SafeJSONEncoder(json.JSONEncoder):
    """
    Safely encodes non-JSON-serializable objects.
    """

    def default(self, o: Any) -> Any:
        try:
            return super().default(o)
        except TypeError:
            if hasattr(o, "dict") and callable(o.dict):  # pydantic v1 models, e.g., from Cohere
                return o.dict()
            return repr(o)


def _flatten(mapping: Optional[Mapping[str, Any]]) -> Iterator[Tuple[str, AttributeValue]]:
    if not mapping:
        return
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            for sub_key, sub_value in _flatten(value):
                yield f"{key}.{sub_key}", sub_value
        elif isinstance(value, List) and any(isinstance(item, Mapping) for item in value):
            for index, sub_mapping in enumerate(value):
                for sub_key, sub_value in _flatten(sub_mapping):
                    yield f"{key}.{index}.{sub_key}", sub_value
        else:
            if isinstance(value, Enum):
                value = value.value
            yield key, value


def _get_input_value(method: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """
    Parses a method call's inputs into a JSON string. Ensures a consistent
    output regardless of whether the those inputs are passed as positional or
    keyword arguments.
    """

    # For typical class methods, the corresponding instance of inspect.Signature
    # does not include the self parameter. However, the inspect.Signature
    # instance for __call__ does include the self parameter.
    method_signature = signature(method)
    first_parameter_name = next(iter(method_signature.parameters), None)
    signature_contains_self_parameter = first_parameter_name in ["self"]
    bound_arguments = method_signature.bind(
        *(
            [None]  # the value bound to the method's self argument is discarded below, so pass None
            if signature_contains_self_parameter
            else []  # no self parameter, so no need to pass a value
        ),
        *args,
        **kwargs,
    )
    return safe_json_dumps(
        {
            **{
                argument_name: argument_value
                for argument_name, argument_value in bound_arguments.arguments.items()
                if argument_name not in ["self", "kwargs"]
            },
            **bound_arguments.arguments.get("kwargs", {}),
        },
        cls=SafeJSONEncoder,
    )


def _get_raw_input(args: Any, **kwargs: Any) -> str:
    """
    Parses a method call's inputs into a JSON string. Ensures a consistent
    output regardless of whether the those inputs are passed as positional or
    keyword arguments.
    """
    return safe_json_dumps(
        {
            "args": _to_dict(args),
            **_to_dict(kwargs)
        },
        cls=SafeJSONEncoder,
    )


def _get_raw_output(response: Any) -> str:
    """
    Parses a method call's inputs into a JSON string. Ensures a consistent
    output regardless of whether the those inputs are passed as positional or
    keyword arguments.
    """
    return safe_json_dumps(
        {
            "response": _to_dict(response),
        },
        cls=SafeJSONEncoder,
    )

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
    

def _get_llm_input_messages(messages: List[Any]) -> Iterator[Tuple[str, AttributeValue]]:
    for index, message in enumerate(messages):
        if isinstance(message, dict):
            content = message.get("content", "")
            if content and isinstance(content, str):
                yield f"{LLM_INPUT_MESSAGES}.{index}.{MESSAGE_ROLE}", message["role"]
                yield f"{LLM_INPUT_MESSAGES}.{index}.{MESSAGE_CONTENT}", content


def _get_llm_output_messages(output: Any) -> Iterator[Tuple[str, AttributeValue]]:
    if output and isinstance(output, str):
            yield f"{LLM_OUTPUT_MESSAGES}.{0}.{MESSAGE_ROLE}", "assistant"
            yield f"{LLM_OUTPUT_MESSAGES}.{0}.{MESSAGE_CONTENT}", output

class _WithTracer(ABC):
    """
    Base class for wrappers that need a tracer. Acts as a trait for the wrappers
    """

    def __init__(self, tracer: trace_api.Tracer, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tracer = tracer


class _GuardCallWrapper(_WithTracer):
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        span_name = "parse"
        with self._tracer.start_as_current_span(
            span_name,
            attributes=dict(
                _flatten(
                    {
                        FI_SPAN_KIND: GUARDRAIL,
                        INPUT_VALUE: _get_input_value(
                            wrapped,
                            *args,
                            **kwargs,
                        ),
                        RAW_INPUT: _get_raw_input(
                            args,
                            **kwargs,
                        )
                    }
                )
            ),
        ) as span:
            span.set_attributes(dict(get_attributes_from_context()))
            try:
                response = wrapped(*args, **kwargs)
                span.set_attribute(RAW_OUTPUT, _get_raw_output(response))
            except Exception as exception:
                span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exception)))
                span.record_exception(exception)
                raise
            span.set_status(trace_api.StatusCode.OK)
        return response


class _PromptCallableWrapper(_WithTracer):
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        if instance:
            span_name = f"{instance.__class__.__name__}.{wrapped.__name__}"
        else:
            span_name = wrapped.__name__
        with self._tracer.start_as_current_span(
            span_name,
            attributes=dict(
                _flatten(
                    {
                        FI_SPAN_KIND: LLM,
                        INPUT_VALUE: _get_input_value(
                            wrapped,
                            *args,
                            **kwargs,
                        ),
                        RAW_INPUT: _get_raw_input(
                            args,
                            **kwargs,
                        )
                    }
                )
            ),
        ) as span:
            span.set_attributes(dict(get_attributes_from_context()))
            try:
                if messages := kwargs.get("msg_history"):
                    for k, v in _get_llm_input_messages(messages):
                        span.set_attribute(k, v)

                response = wrapped(*args, **kwargs)
                if response.output:
                    span.set_attribute(OUTPUT_VALUE, response.output)
                    for k, v in _get_llm_output_messages(response.output):
                        span.set_attribute(k, v)
                
                if hasattr(response, 'prompt_token_count') and response.prompt_token_count is not None:
                    span.set_attribute(PROMPT_TOKENS, response.prompt_token_count)

                if hasattr(response, 'response_token_count') and response.response_token_count is not None:
                    span.set_attribute(COMPLETION_TOKENS, response.response_token_count)

                span.set_attribute(RAW_OUTPUT, _get_raw_output(response))
            except Exception as exception:
                span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exception)))
                span.record_exception(exception)
                raise
            span.set_status(trace_api.StatusCode.OK)
        return response


class _ParseCallableWrapper(_WithTracer):
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        if instance:
            span_name = f"{instance.__class__.__name__}.{wrapped.__name__}"
        else:
            span_name = wrapped.__name__
        with self._tracer.start_as_current_span(
            span_name,
            attributes=dict(
                _flatten(
                    {
                        FI_SPAN_KIND: GUARDRAIL,
                        INPUT_VALUE: _get_input_value(
                            wrapped,
                            *args,
                            **kwargs,
                        ),
                        RAW_INPUT: _get_raw_input(
                            args,
                            **kwargs,
                        )
                    }
                )
            ),
        ) as span:
            span.set_attributes(dict(get_attributes_from_context()))
            try:
                response = wrapped(*args, **kwargs)
                span.set_attribute(RAW_OUTPUT, _get_raw_output(response))
            except Exception as exception:
                span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exception)))
                span.record_exception(exception)
                raise
            span.set_status(trace_api.StatusCode.OK)
        return response


class _PostValidationWrapper(_WithTracer):
    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        if instance:
            span_name = f"{instance.__class__.__name__}.{wrapped.__name__}"
        else:
            span_name = wrapped.__name__
        with self._tracer.start_as_current_span(
            span_name,
            attributes=dict(
                _flatten(
                    {
                        RAW_INPUT: _get_raw_input(args, **kwargs),
                    }   
                )
            ),
        ) as span:
            span.set_attributes(dict(get_attributes_from_context()))
            try:
                validator = args[0]
                span.set_attribute("validator_name", validator.rail_alias)
                span.set_attribute("validator_on_fail", validator.on_fail_descriptor.name)

                validation_result = args[2]
                if validator.rail_alias == "fi/dataset_embeddings":
                    span.set_attribute(
                        INPUT_VALUE,
                        validation_result.metadata.get("user_message")
                        if validation_result.metadata
                        else "",
                    )
                span.set_attribute(OUTPUT_VALUE, validation_result.outcome)
                span.set_attributes(
                    dict(
                        _flatten(
                            validation_result.metadata if validation_result.metadata else {},
                        )
                    )
                )
                response = wrapped(*args, **kwargs)
                span.set_attribute(RAW_OUTPUT, _get_raw_output(response))
            except Exception as exception:
                span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exception)))
                span.record_exception(exception)
                raise
            span.set_status(trace_api.StatusCode.OK)
        return response


INPUT_VALUE = SpanAttributes.INPUT_VALUE
FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
RAW_INPUT = SpanAttributes.RAW_INPUT
RAW_OUTPUT = SpanAttributes.RAW_OUTPUT
GUARDRAIL = FiSpanKindValues.GUARDRAIL
LLM = FiSpanKindValues.LLM
LLM_INPUT_MESSAGES = SpanAttributes.LLM_INPUT_MESSAGES
LLM_OUTPUT_MESSAGES = SpanAttributes.LLM_OUTPUT_MESSAGES
PROMPT_TOKENS = SpanAttributes.LLM_TOKEN_COUNT_PROMPT
COMPLETION_TOKENS = SpanAttributes.LLM_TOKEN_COUNT_COMPLETION
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE