import inspect
import json
from enum import Enum
from inspect import signature
from typing import Any, Callable, Iterator, List, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.util.types import AttributeValue
from pydantic import BaseModel


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
    output regardless of whether those inputs are passed as positional or
    keyword arguments.
    """
    try:
        method_signature = signature(method)
        bound_arguments = method_signature.bind(*args, **kwargs)
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
    except TypeError:
        return safe_json_dumps(kwargs, cls=SafeJSONEncoder)


class _PatchWrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        span_name = f"{wrapped.__module__}.{wrapped.__name__}"
        raw_input = _raw_input(args, **kwargs)
        attributes = dict(
            _flatten(
                {
                    FI_SPAN_KIND: FiSpanKindValues.TOOL,
                    INPUT_VALUE_MIME_TYPE: "application/json",
                    RAW_INPUT: safe_json_dumps(raw_input),
                    INPUT_VALUE: safe_json_dumps(raw_input.get("messages", "")),
                }
            )
        )

        if inspect.iscoroutinefunction(wrapped):

            async def wrapper():
                with self._tracer.start_as_current_span(
                    span_name,
                    attributes=attributes,
                    record_exception=False,
                    set_status_on_exception=False,
                ) as span:
                    try:
                        resp = await wrapped(*args, **kwargs)
                        if resp:
                            span.set_attribute(OUTPUT_VALUE, safe_json_dumps(resp))
                            span.set_attribute(OUTPUT_MIME_TYPE, "application/json")
                            span.set_attribute(RAW_OUTPUT, _raw_output(resp))
                        span.set_status(trace_api.StatusCode.OK)
                        return resp
                    except Exception as e:
                        span.set_status(
                            trace_api.Status(trace_api.StatusCode.ERROR, str(e))
                        )
                        span.record_exception(e)
                        raise

            return wrapper()
        else:
            with self._tracer.start_as_current_span(
                span_name,
                attributes=attributes,
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                try:
                    resp = wrapped(*args, **kwargs)
                    if resp:
                        span.set_attribute(OUTPUT_VALUE, safe_json_dumps(resp))
                        span.set_attribute(OUTPUT_MIME_TYPE, "application/json")
                        span.set_attribute(RAW_OUTPUT, _raw_output(resp))
                    span.set_status(trace_api.StatusCode.OK)
                    return resp
                except Exception as e:
                    span.set_status(
                        trace_api.Status(trace_api.StatusCode.ERROR, str(e))
                    )
                    span.record_exception(e)
                    raise


class _HandleResponseWrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        span_name = f"{wrapped.__module__}.{wrapped.__name__}"
        raw_input = _raw_input(args, **kwargs)
        attributes = dict(
            _flatten(
                {
                    FI_SPAN_KIND: FiSpanKindValues.TOOL,
                    INPUT_VALUE_MIME_TYPE: "application/json",
                    RAW_INPUT: safe_json_dumps(raw_input),
                    INPUT_VALUE: safe_json_dumps(raw_input.get("messages", "")),
                }
            )
        )

        if inspect.iscoroutinefunction(wrapped):

            async def wrapper():
                with self._tracer.start_as_current_span(
                    span_name,
                    attributes=attributes,
                    record_exception=False,
                    set_status_on_exception=False,
                ) as span:
                    try:
                        resp = await wrapped(*args, **kwargs)

                        if resp:
                            span.set_attribute(OUTPUT_VALUE, json.dumps(resp))
                            span.set_attribute(OUTPUT_MIME_TYPE, "application/json")
                            span.set_attribute(RAW_OUTPUT, _raw_output(resp))
                        span.set_status(trace_api.StatusCode.OK)
                        return resp
                    except Exception as e:
                        span.set_status(
                            trace_api.Status(trace_api.StatusCode.ERROR, str(e))
                        )
                        span.record_exception(e)
                        raise

            return wrapper()
        else:
            with self._tracer.start_as_current_span(
                span_name,
                attributes=attributes,
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                try:
                    resp = wrapped(*args, **kwargs)

                    if resp:
                        span.set_attribute(
                            OUTPUT_VALUE, safe_json_dumps(resp[0].model_json_schema())
                        )
                        span.set_attribute(OUTPUT_MIME_TYPE, "application/json")
                        span.set_attribute(RAW_OUTPUT, safe_json_dumps(resp))
                    span.set_status(trace_api.StatusCode.OK)
                    return resp
                except Exception as e:
                    span.set_status(
                        trace_api.Status(trace_api.StatusCode.ERROR, str(e))
                    )
                    span.record_exception(e)
                    raise


def _to_dict(value: Any) -> Any:
    """
    Recursively converts objects to dictionaries.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {k: _to_dict(v) for k, v in value.items()}
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


def _raw_input(*args: Any, **kwargs: Any) -> str:
    processed_input = {
        **{f"arg_{i}": _to_dict(arg) for i, arg in enumerate(args)},
        **{k: _to_dict(v) for k, v in kwargs.items()},
    }
    return processed_input


def _raw_output(response: Any) -> str:
    if not response:
        return ""
    return safe_json_dumps(_to_dict(response), cls=SafeJSONEncoder)


INPUT_VALUE = SpanAttributes.INPUT_VALUE
INPUT_VALUE_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
RAW_INPUT = SpanAttributes.RAW_INPUT
RAW_OUTPUT = SpanAttributes.RAW_OUTPUT
QUERY = SpanAttributes.QUERY
EVAL_INPUT = SpanAttributes.EVAL_INPUT
