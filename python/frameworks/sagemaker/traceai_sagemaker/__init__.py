"""OpenTelemetry instrumentation for Amazon SageMaker Runtime endpoint calls made through boto3.

Traces ``invoke_endpoint``, ``invoke_endpoint_async`` and ``invoke_endpoint_with_response_stream``
on ``sagemaker-runtime`` clients as LLM spans. Only that service is wrapped, so it can run next to
``traceAI-bedrock``, which hooks the same boto3 client creation for ``bedrock-*`` services.
"""
from __future__ import annotations

import io
import json
import logging
from functools import wraps
from importlib import import_module
from typing import Any, Callable, Collection, Dict, Optional

from botocore.response import StreamingBody
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.trace import Status, StatusCode
from wrapt import wrap_function_wrapper

from fi_instrumentation import FITracer, TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes

from . import _payload as payload
from ._stream import StreamAccumulator, StreamBody
from .package import _instruments
from .version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "botocore.client"
_SERVICE = "sagemaker-runtime"
_PROVIDER = "aws.sagemaker"
_OPERATIONS = ("invoke_endpoint", "invoke_endpoint_async", "invoke_endpoint_with_response_stream")

_ENDPOINT = "aws.sagemaker.endpoint_name"
_COMPONENT = "aws.sagemaker.inference_component"
_TARGET_VARIANT = "aws.sagemaker.target_variant"
_INVOKED_VARIANT = "aws.sagemaker.invoked_variant"
_INFERENCE_ID = "aws.sagemaker.inference_id"


class _State:
    """Per-instrument switch, so a wrapper that cannot be unhooked becomes a pass-through."""

    def __init__(self, tracer: FITracer, config: TraceConfig) -> None:
        self.tracer = tracer
        self.config = config
        self.active = True

    @property
    def capture_input(self) -> bool:
        c = self.config
        return not (c.hide_inputs or c.hide_input_text or c.hide_input_messages or c.hide_input_images)

    @property
    def capture_output(self) -> bool:
        c = self.config
        return not (c.hide_outputs or c.hide_output_text or c.hide_output_messages)


def _set(span: Any, key: str, value: Any) -> None:
    if value is not None and value != "":
        span.set_attribute(key, value)


def _fail(span: Any, exc: BaseException) -> None:
    # Exception text can carry request/response content or credentials: record the class only.
    try:
        span.set_attribute("error.type", type(exc).__name__)
        span.set_status(Status(StatusCode.ERROR))
    finally:
        span.end()


def _start(state: _State, operation: str, kwargs: Dict[str, Any]) -> Any:
    attrs: Dict[str, Any] = {
        SpanAttributes.GEN_AI_PROVIDER_NAME: _PROVIDER,
        SpanAttributes.GEN_AI_OPERATION_NAME: operation,
    }
    for key, name in (
        (SpanAttributes.GEN_AI_REQUEST_MODEL, "EndpointName"),
        (_ENDPOINT, "EndpointName"),
        (_COMPONENT, "InferenceComponentName"),
        (_TARGET_VARIANT, "TargetVariant"),
        (_INFERENCE_ID, "InferenceId"),
    ):
        value = kwargs.get(name)
        if isinstance(value, str) and value:
            attrs[key] = value
    span = state.tracer.start_span(
        f"sagemaker.{operation}", attributes=attrs, fi_span_kind=FiSpanKindValues.LLM
    )
    try:
        _record_request(state, span, operation, kwargs)
    except Exception:
        logger.debug("sagemaker request capture failed", exc_info=True)
    return span


def _record_request(state: _State, span: Any, operation: str, kwargs: Dict[str, Any]) -> None:
    if operation == "invoke_endpoint_async":
        # The payload lives in S3 (InputLocation); there is no body to capture.
        return
    captured = payload.capture_body(kwargs.get("Body"), kwargs.get("ContentType"))
    if captured is None:
        return
    if state.capture_input:
        span.set_attribute(SpanAttributes.INPUT_VALUE, captured.value)
        span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, captured.mime_type)
    params = payload.request_parameters(captured.parsed)
    if params:
        span.set_attribute(SpanAttributes.GEN_AI_REQUEST_PARAMETERS, payload.clip(safe_json_dumps(params)))


def _record_usage(span: Any, usage: payload.Usage) -> None:
    _set(span, SpanAttributes.GEN_AI_RESPONSE_MODEL, usage.model)
    _set(span, SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, usage.input_tokens)
    _set(span, SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens)
    _set(span, SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, usage.total_tokens)


def _record_common(span: Any, response: Any) -> None:
    if not isinstance(response, dict):
        return
    _set(span, _INVOKED_VARIANT, response.get("InvokedProductionVariant"))
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, dict):
        _set(span, "aws.request_id", metadata.get("RequestId"))


def _finish_invoke(state: _State, span: Any, response: Dict[str, Any]) -> None:
    """Record the response of a blocking call; replaces the body with an equivalent readable one."""
    _record_common(span, response)
    body = response.get("Body")
    content_type = response.get("ContentType")
    if body is None or payload.classify(content_type) == payload.BINARY or not hasattr(body, "read"):
        # Binary output is only noted; leave the caller's body unread.
        if state.capture_output and body is not None:
            length = getattr(body, "_content_length", None)
            _set(span, SpanAttributes.OUTPUT_VALUE, payload.binary_note(content_type, length if isinstance(length, int) else None))
            _set(span, SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
        return
    try:
        data = body.read()
    except BaseException as exc:
        # The caller's own network read failed; surface exactly what they would have seen.
        raise _BodyReadError(exc) from exc
    response["Body"] = StreamingBody(io.BytesIO(data), len(data))
    captured = payload.capture_body(data, content_type)
    if captured is None:
        return
    usage = payload.usage_of(captured.parsed)
    _record_usage(span, usage)
    if state.capture_output:
        text = payload.text_of(captured.parsed)
        if text:
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, payload.clip(text))
            span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
        else:
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, captured.value)
            span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, captured.mime_type)


def _finish_async(state: _State, span: Any, response: Dict[str, Any]) -> None:
    _record_common(span, response)
    if isinstance(response, dict):
        _set(span, _INFERENCE_ID, response.get("InferenceId"))


def _stream_end(state: _State) -> Callable[[Any, StreamAccumulator], None]:
    def end(span: Any, acc: StreamAccumulator) -> None:
        _record_usage(span, acc.usage)
        output = acc.output()
        if output:
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, output)
            span.set_attribute(
                SpanAttributes.OUTPUT_MIME_TYPE, "text/plain"
            )

    return end


def _finish_stream(state: _State, span: Any, response: Dict[str, Any]) -> bool:
    """Wrap the event stream. Returns True when the span's end is now owned by the stream."""
    _record_common(span, response)
    body = response.get("Body")
    if body is None or not hasattr(body, "__iter__"):
        return False
    accumulator = StreamAccumulator(response.get("ContentType"), state.capture_output)
    response["Body"] = StreamBody(body, span, accumulator, _stream_end(state))
    return True


_FINISHERS = {
    "invoke_endpoint": _finish_invoke,
    "invoke_endpoint_async": _finish_async,
}


def _wrap_operation(state: _State, operation: str, original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def instrumented(*args: Any, **kwargs: Any) -> Any:
        if not state.active or context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return original(*args, **kwargs)
        try:
            span = _start(state, operation, kwargs)
        except Exception:
            logger.debug("sagemaker span start failed", exc_info=True)
            return original(*args, **kwargs)
        try:
            with trace_api.use_span(
                span, end_on_exit=False, record_exception=False, set_status_on_exception=False
            ):
                response = original(*args, **kwargs)
        except BaseException as exc:
            _fail(span, exc)
            raise
        try:
            if operation == "invoke_endpoint_with_response_stream":
                if _finish_stream(state, span, response):
                    return response
            else:
                _FINISHERS[operation](state, span, response)
        except _BodyReadError as wrapper:
            _fail(span, wrapper.original)
            raise wrapper.original
        except Exception:
            logger.debug("sagemaker response capture failed", exc_info=True)
        span.end()
        return response

    return instrumented


class _BodyReadError(Exception):
    """Internal carrier for a failed response-body read, so it is re-raised unchanged."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


def _create_client_wrapper(state: _State) -> Callable[..., Any]:
    def wrapper(wrapped: Callable[..., Any], instance: Any, args: Any, kwargs: Any) -> Any:
        client = wrapped(*args, **kwargs)
        if not state.active:
            return client
        try:
            if client.meta.service_model.service_name != _SERVICE:
                return client
            for operation in _OPERATIONS:
                original = getattr(client, operation, None)
                if original is None:
                    continue  # older service models lack some operations
                setattr(client, f"_unwrapped_{operation}", original)
                setattr(client, operation, _wrap_operation(state, operation, original))
        except Exception:
            logger.debug("sagemaker client instrumentation failed", exc_info=True)
        return client

    return wrapper


class SageMakerInstrumentor(BaseInstrumentor):  # type: ignore
    """Instrument boto3 ``sagemaker-runtime`` clients created after ``instrument()``."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        elif not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        tracer = FITracer(trace_api.get_tracer(__name__, __version__, tracer_provider), config=config)
        self._state = _State(tracer, config)

        client_module = import_module(_MODULE)
        self._creator = client_module.ClientCreator
        self._original = self._creator.__dict__["create_client"]
        wrap_function_wrapper(_MODULE, "ClientCreator.create_client", _create_client_wrapper(self._state))
        self._wrapper = self._creator.__dict__["create_client"]

    def _uninstrument(self, **kwargs: Any) -> None:
        state: Optional[_State] = getattr(self, "_state", None)
        if state is not None:
            # Wrappers that stay reachable (another instrumentor wrapped on top, or clients that
            # already exist) become pass-throughs instead of clobbering that other wrapper.
            state.active = False
        current = self._creator.__dict__.get("create_client")
        if current is self._wrapper or getattr(current, "_self_parent", None) is self._wrapper:
            self._creator.create_client = self._original


__all__ = ["SageMakerInstrumentor", "__version__"]
