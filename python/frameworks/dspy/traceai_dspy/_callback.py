"""DSPy's lifecycle callbacks adapted to FITracer (no provider calls or patches)."""

import sys
from asyncio import CancelledError
from dataclasses import dataclass
from threading import RLock
from typing import Any

import dspy
from dspy.utils.callback import BaseCallback
from fi_instrumentation import FITracer, safe_json_dumps
from fi_instrumentation.fi_types import (
    DocumentAttributes,
    FiSpanKindValues,
    SpanAttributes,
)
from opentelemetry import context, trace
from opentelemetry.trace import StatusCode

from traceai_dspy import (
    DSPyJSONEncoder,
    _flatten,
    _get_predict_span_name,
    _llm_input_messages,
    _llm_output_messages,
)


@dataclass
class _Call:
    span: trace.Span
    token: Any
    kind: str


def _payload(value: Any) -> str:
    # DSPyJSONEncoder handles Predictions without serializing Module/LM __dict__
    # (which can contain clients, callbacks and credentials).
    return safe_json_dumps(value, cls=DSPyJSONEncoder)


class _DSPyCallback(BaseCallback):
    def __init__(self, tracer: FITracer) -> None:
        self._tracer = tracer
        self._calls: dict[str, _Call] = {}
        self._lock = RLock()
        self._enabled = True

    def disable(self) -> None:
        with self._lock:
            self._enabled = False

    def _start(
        self,
        call_id: str,
        instance: Any,
        inputs: dict[str, Any],
        operation: str,
        kind: str = FiSpanKindValues.CHAIN.value,
    ) -> None:
        with self._lock:
            if (
                not self._enabled
                or call_id in self._calls
                or context.get_value(context._SUPPRESS_INSTRUMENTATION_KEY)
            ):
                return
            name = f"{type(instance).__name__}.{operation}"
            if operation == "forward":
                name = _get_predict_span_name(instance)
                if isinstance(instance, dspy.Retrieve):
                    kind = FiSpanKindValues.RETRIEVER.value
            attributes = {
                SpanAttributes.GEN_AI_SPAN_KIND: kind,
                SpanAttributes.INPUT_VALUE: _payload(inputs),
                SpanAttributes.INPUT_MIME_TYPE: "application/json",
            }
            if kind == FiSpanKindValues.TOOL.value and isinstance(instance, dspy.Tool):
                attributes[SpanAttributes.GEN_AI_TOOL_NAME] = instance.name
            if kind == FiSpanKindValues.LLM.value:
                attributes.update(_llm_input_messages(inputs))
                model = getattr(instance, "model", None) or getattr(
                    instance, "model_name", None
                )
                if isinstance(model, str):
                    attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = model
                # LM kwargs may hold provider credentials: record only generation
                # controls, never arbitrary client configuration.
                parameters = dict(getattr(instance, "kwargs", {}) or {})
                parameters.update(inputs.get("kwargs", {}) or {})
                allowed = {
                    "temperature",
                    "max_tokens",
                    "max_completion_tokens",
                    "top_p",
                    "top_k",
                    "n",
                    "seed",
                    "stop",
                    "frequency_penalty",
                    "presence_penalty",
                }
                attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = _payload(
                    {key: value for key, value in parameters.items() if key in allowed}
                )
            span = self._tracer.start_span(name, attributes=attributes)
            token = context.attach(trace.set_span_in_context(span))
            self._calls[call_id] = _Call(span, token, kind)

    def _end(
        self, call_id: str, outputs: Any, exception: BaseException | None = None
    ) -> None:
        with self._lock:
            call = self._calls.pop(call_id, None)
        if call is None:
            return
        try:
            # Older DSPy callbacks catch Exception rather than BaseException,
            # but still invoke the end handler in finally during cancellation.
            active_exception = sys.exc_info()[1]
            if exception is None and isinstance(
                active_exception, (CancelledError, KeyboardInterrupt, SystemExit)
            ):
                exception = active_exception
            if exception is not None:
                # FITracer applies hide_error_details to both events and status.
                call.span.record_exception(exception)
                call.span.set_status(StatusCode.ERROR)
            else:
                call.span.set_attribute(SpanAttributes.OUTPUT_VALUE, _payload(outputs))
                call.span.set_attribute(
                    SpanAttributes.OUTPUT_MIME_TYPE, "application/json"
                )
                if call.kind == FiSpanKindValues.LLM.value:
                    call.span.set_attributes(dict(_llm_output_messages(outputs)))
                elif call.kind == FiSpanKindValues.RETRIEVER.value and hasattr(
                    outputs, "get"
                ):
                    call.span.set_attributes(
                        dict(
                            _flatten(
                                {
                                    SpanAttributes.RETRIEVAL_DOCUMENTS: [
                                        {DocumentAttributes.DOCUMENT_CONTENT: text}
                                        for text in outputs.get("passages", [])
                                    ]
                                }
                            )
                        )
                    )
                call.span.set_status(StatusCode.OK)
        finally:
            # End callbacks execute in the same task/thread as their starts,
            # including DSPy stream workers and coroutine cancellation.
            context.detach(call.token)
            call.span.end()

    def on_module_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        # DSPy 2.x routes BaseLM subclasses other than LM to module callbacks.
        if isinstance(instance, dspy.BaseLM):
            self.on_lm_start(call_id, instance, inputs)
        else:
            self._start(call_id, instance, inputs, "forward")

    on_module_end = _end

    def on_lm_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
        self._start(call_id, instance, inputs, "__call__", FiSpanKindValues.LLM.value)

    on_lm_end = _end

    def on_tool_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        self._start(call_id, instance, inputs, "__call__", FiSpanKindValues.TOOL.value)

    on_tool_end = _end

    def on_adapter_format_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        self._start(call_id, instance, inputs, "format")

    on_adapter_format_end = _end

    def on_adapter_parse_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        self._start(call_id, instance, inputs, "parse")

    on_adapter_parse_end = _end

    def on_evaluate_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        self._start(call_id, instance, inputs, "evaluate")

    on_evaluate_end = _end

    def on_compile_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        self._start(call_id, instance, inputs, "compile")

    on_compile_end = _end

    def on_interpreter_execute_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        self._start(call_id, instance, inputs, "execute", FiSpanKindValues.TOOL.value)

    on_interpreter_execute_end = _end

    def on_interpreter_tool_call_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        self._start(call_id, instance, inputs, "tool_call", FiSpanKindValues.TOOL.value)

    on_interpreter_tool_call_end = _end

    def on_interpreter_startup_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        self._start(call_id, instance, inputs, "startup", FiSpanKindValues.TOOL.value)

    on_interpreter_startup_end = _end

    def on_interpreter_shutdown_start(
        self, call_id: str, instance: Any, inputs: dict[str, Any]
    ) -> None:
        self._start(call_id, instance, inputs, "shutdown", FiSpanKindValues.TOOL.value)

    on_interpreter_shutdown_end = _end
