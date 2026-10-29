import json
import logging
from contextlib import contextmanager
from abc import ABC
from enum import Enum
from inspect import signature
from typing import Any, Callable, Iterator, List, Mapping, Optional, Tuple

import opentelemetry.context as context_api
from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import (
    FiSpanKindValues,
    MessageAttributes,
    SpanAttributes,
)
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)


class SafeJSONEncoder(json.JSONEncoder):
    """
    Safely encodes non-JSON-serializable objects.
    """

    def default(self, o: Any) -> Any:
        try:
            return super().default(o)
        except TypeError:
            if hasattr(o, "dict") and callable(
                o.dict
            ):  # pydantic v1 models, e.g., from Cohere
                return o.dict()
            return repr(o)


def _flatten(
    mapping: Optional[Mapping[str, Any]]
) -> Iterator[Tuple[str, AttributeValue]]:
    if not mapping:
        return
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            for sub_key, sub_value in _flatten(value):
                yield f"{key}.{sub_key}", sub_value
        elif isinstance(value, List) and any(
            isinstance(item, Mapping) for item in value
        ):
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
            [
                None
            ]  # the value bound to the method's self argument is discarded below, so pass None
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
    kwargs_dict = _to_dict(kwargs)
    if not isinstance(kwargs_dict, dict):
        kwargs_dict = {}

    return safe_json_dumps(
        {"args": _to_dict(args), **kwargs_dict},
        cls=SafeJSONEncoder,
    )


def _get_step_input(method: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """Capture current Runner.step inputs without traversing its Call history."""
    from guardrails.prompt.base_prompt import BasePrompt

    arguments = signature(method).bind(*args, **kwargs).arguments
    # call_log is a mutable diagnostic accumulator, not an application input.
    # On a reask its serializers include earlier outputs, validator logs and
    # arbitrary result metadata. The API callable also owns internal SDK state.
    inputs = {
        name: arguments[name]
        for name in (
            "index", "output_schema", "instructions", "prompt", "msg_history",
            "prompt_params", "output",
        )
        if name in arguments
    }
    if (call_log := arguments.get("call_log")) is not None:
        # Preserve explicitly supplied application metadata, not the runner's
        # metadata accumulator (which validators can augment during execution).
        inputs["metadata"] = call_log.inputs.metadata

    def project(value: Any) -> Any:
        if isinstance(value, BasePrompt):
            return value.source
        if isinstance(value, dict):
            return {key: project(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [project(item) for item in value]
        return value

    return safe_json_dumps(project(inputs), cls=SafeJSONEncoder)


def _get_raw_output(response: Any) -> str:
    """Serialize output payloads, never the mixed input/output step history."""
    from guardrails.classes.history.iteration import Iteration

    if isinstance(response, Iteration):
        # Iteration.to_dict() includes prompt parameters and input metadata.
        # Even response.outputs.to_dict() includes arbitrary validator metadata,
        # reask diagnostics and potentially consuming LLMResponse serializers.
        # Project only the materialized application outputs: input privacy must
        # work independently of output privacy, and validator metadata is not
        # an output payload. Do not mutate the history returned to the caller.
        output = {
            "rawOutput": response.raw_output,
            "guardedOutput": response.guarded_output,
        }
    else:
        output = _to_dict(response)
    return safe_json_dumps({"response": output}, cls=SafeJSONEncoder)


def _to_dict(result: Any) -> Any:
    from guardrails.classes.llm.llm_response import LLMResponse

    if isinstance(result, LLMResponse):
        # LLMResponse.to_dict() calls to_interface(), which drains stream_output
        # and tries to synchronously consume async_stream_output. Never invoke
        # SDK serializers on these responses, even when nested in call inputs.
        return {
            "output": result.output,
            "promptTokenCount": result.prompt_token_count,
            "responseTokenCount": result.response_token_count,
        }
    if not result:
        return
    if hasattr(result, "to_dict"):
        return result.to_dict()
    elif hasattr(result, "__dict__"):
        return result.__dict__
    elif isinstance(result, list):
        return [_to_dict(item) for item in result]
    elif isinstance(result, tuple):
        return tuple(_to_dict(item) for item in result)
    elif isinstance(result, dict):
        return {key: _to_dict(value) for key, value in result.items()}
    else:
        return result


def _get_llm_input_messages(
    messages: List[Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    for index, message in enumerate(messages):
        if isinstance(message, dict):
            content = message.get("content", "")
            if content and isinstance(content, str):
                yield f"{GEN_AI_INPUT_MESSAGES}.{index}.{MESSAGE_ROLE}", message["role"]
                yield f"{GEN_AI_INPUT_MESSAGES}.{index}.{MESSAGE_CONTENT}", content


def _get_llm_output_messages(output: Any) -> Iterator[Tuple[str, AttributeValue]]:
    if output and isinstance(output, str):
        yield f"{GEN_AI_OUTPUT_MESSAGES}.{0}.{MESSAGE_ROLE}", "assistant"
        yield f"{GEN_AI_OUTPUT_MESSAGES}.{0}.{MESSAGE_CONTENT}", output


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
                        GEN_AI_SPAN_KIND: GUARDRAIL,
                        INPUT_VALUE: _get_input_value(
                            wrapped,
                            *args,
                            **kwargs,
                        ),
                        INPUT_VALUE: _get_raw_input(
                            args,
                            **kwargs,
                        ),
                    }
                )
            ),
        ) as span:
            span.set_attributes(dict(get_attributes_from_context()))
            try:
                response = wrapped(*args, **kwargs)
                span.set_attribute(OUTPUT_VALUE, _get_raw_output(response))
            except Exception as exception:
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR, str(exception))
                )
                span.record_exception(exception)
                raise
            span.set_status(trace_api.StatusCode.OK)
        return response


class _PromptCallableWrapper(_WithTracer):
    @contextmanager
    def _trace_call(self, wrapped, instance, args, kwargs):
        span_name = (
            f"{instance.__class__.__name__}.{wrapped.__name__}"
            if instance is not None else wrapped.__name__
        )
        with self._tracer.start_as_current_span(
            span_name, attributes={GEN_AI_SPAN_KIND: LLM.value},
        ) as span:
            try:
                span.set_attributes(dict(get_attributes_from_context()))
                span.set_attribute(INPUT_VALUE, _get_raw_input(args, **kwargs))
                if messages := kwargs.get("msg_history"):
                    for key, value in _get_llm_input_messages(messages):
                        span.set_attribute(key, value)
            except Exception:
                logger.debug("Failed to extract prompt attributes", exc_info=True)
            try:
                yield span
            except Exception as exception:
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR, str(exception))
                )
                span.record_exception(exception)
                raise
            else:
                span.set_status(trace_api.StatusCode.OK)

    def _record_response(self, span, response):
        try:
            if response.output:
                span.set_attribute(OUTPUT_VALUE, response.output)
                for key, value in _get_llm_output_messages(response.output):
                    span.set_attribute(key, value)
            for field, key in (
                ("prompt_token_count", PROMPT_TOKENS),
                ("response_token_count", COMPLETION_TOKENS),
            ):
                if (value := getattr(response, field, None)) is not None:
                    span.set_attribute(key, value)
            span.set_attribute(OUTPUT_VALUE, _get_raw_output(response))
        except Exception:
            # Telemetry must not change the callable's result or consume streams.
            logger.debug("Failed to extract prompt response attributes", exc_info=True)

    def __call__(self, wrapped, instance, args, kwargs):
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        with self._trace_call(wrapped, instance, args, kwargs) as span:
            response = wrapped(*args, **kwargs)
            self._record_response(span, response)
        return response


class _AsyncPromptCallableWrapper(_PromptCallableWrapper):
    async def __call__(self, wrapped, instance, args, kwargs):
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)
        with self._trace_call(wrapped, instance, args, kwargs) as span:
            response = await wrapped(*args, **kwargs)
            self._record_response(span, response)
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
                        GEN_AI_SPAN_KIND: GUARDRAIL,
                        INPUT_VALUE: _get_step_input(wrapped, *args, **kwargs),
                    }
                )
            ),
        ) as span:
            span.set_attributes(dict(get_attributes_from_context()))
            try:
                response = wrapped(*args, **kwargs)
                span.set_attribute(OUTPUT_VALUE, _get_raw_output(response))
            except Exception as exception:
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR, str(exception))
                )
                span.record_exception(exception)
                raise
            span.set_status(trace_api.StatusCode.OK)
        return response


class _PostValidationWrapper(_WithTracer):
    def __call__(self, wrapped, instance, args, kwargs):
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        span_name = (
            f"{instance.__class__.__name__}.{wrapped.__name__}"
            if instance is not None else wrapped.__name__
        )
        with self._tracer.start_as_current_span(
            span_name, attributes={GEN_AI_SPAN_KIND: GUARDRAIL.value},
        ) as span:
            try:
                span.set_attributes(dict(get_attributes_from_context()))
                validator = args[0] if args else kwargs["validator"]
                logs = args[1] if len(args) > 1 else kwargs.get("validator_logs")
                result = args[2] if len(args) > 2 else kwargs["result"]
                span.set_attribute("validator_name", validator.rail_alias)
                descriptor = validator.on_fail_descriptor
                # Guardrails uses an Enum for built-ins, but 'custom' for callable
                # handlers. Never assume .name exists or stringify the callback.
                descriptor = getattr(descriptor, "name", descriptor)
                if isinstance(descriptor, str):
                    span.set_attribute("validator_on_fail", descriptor)
                value = getattr(logs, "value_before_validation", None)
                if validator.rail_alias == "fi/dataset_embeddings" and result.metadata:
                    value = result.metadata.get("user_message", value)
                if value is not None:
                    span.set_attribute(
                        INPUT_VALUE, value if isinstance(value, str) else safe_json_dumps(value)
                    )
                span.set_attribute(OUTPUT_VALUE, result.outcome)
                # Only the validator identity and failure policy are public
                # metadata. Arbitrary result.metadata can contain user text or
                # secrets. Do not flatten it, or serialize ValidatorLogs (which
                # mixes input, output and metadata) into an ungoverned attribute.
            except Exception:
                logger.debug("Failed to extract validator attributes", exc_info=True)
            try:
                response = wrapped(*args, **kwargs)
            except Exception as exception:
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR, str(exception))
                )
                span.record_exception(exception)
                raise
            span.set_status(trace_api.StatusCode.OK)
        return response


INPUT_VALUE = SpanAttributes.INPUT_VALUE
GEN_AI_SPAN_KIND = SpanAttributes.GEN_AI_SPAN_KIND
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
GUARDRAIL = FiSpanKindValues.GUARDRAIL
LLM = FiSpanKindValues.LLM
GEN_AI_INPUT_MESSAGES = SpanAttributes.GEN_AI_INPUT_MESSAGES
GEN_AI_OUTPUT_MESSAGES = SpanAttributes.GEN_AI_OUTPUT_MESSAGES
PROMPT_TOKENS = SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS
COMPLETION_TOKENS = SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE
