"""Deferred tracing for Bedrock InvokeModel streams outside the Messages API."""

from __future__ import annotations

import json
from typing import Any, Mapping, TypeVar

from opentelemetry.trace import Span
from opentelemetry.util.types import AttributeValue

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiLLMProviderValues,
    FiMimeTypeValues,
    FiSpanKindValues,
    SpanAttributes,
)
from traceai_bedrock._proxy import _no_err
from traceai_bedrock.utils import _EventStream, _finish

_AnyT = TypeVar("_AnyT")


class _InvokeModelEventStream(_EventStream):
    def close(self) -> Any:
        # Closing is not iteration: explicitly finish without draining the provider.
        callback = _no_err(self._self_callback)
        try:
            result = self.__wrapped__.close()
        except BaseException as exc:
            callback(exc)
            raise
        callback(StopIteration())
        return result


class _InvokeModelStreamCallback:
    def __init__(self, span: Span, request: Mapping[str, Any]) -> None:
        self._span = span
        self._finished = False
        self._text: list[str] = []
        self._payloads: list[dict[str, Any]] = []
        self._attributes: dict[str, AttributeValue] = {
            SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.LLM.value,
            SpanAttributes.GEN_AI_PROVIDER_NAME: FiLLMProviderValues.AWS.value,
        }
        if isinstance(model := request.get("modelId"), str):
            self._attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = model
        if (body := request.get("body")) is not None:
            serialized = safe_json_dumps(body)
            self._attributes[SpanAttributes.INPUT_VALUE] = serialized
            self._attributes[SpanAttributes.INPUT_MIME_TYPE] = FiMimeTypeValues.JSON.value
            self._attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = serialized

    def __call__(self, obj: _AnyT) -> _AnyT:
        if self._finished:
            return obj
        if isinstance(obj, dict):
            chunk = obj.get("chunk")
            if not isinstance(chunk, dict):
                return obj
            try:
                payload = json.loads(chunk.get("bytes"))
            except (TypeError, ValueError):
                return obj
            if isinstance(payload, dict):
                self._accumulate(payload)
        elif isinstance(obj, BaseException):
            self._finished = True
            if self._text:
                self._attributes[SpanAttributes.OUTPUT_VALUE] = "".join(self._text)
                self._attributes[SpanAttributes.OUTPUT_MIME_TYPE] = FiMimeTypeValues.TEXT.value
            elif self._payloads:
                self._attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(self._payloads)
                self._attributes[SpanAttributes.OUTPUT_MIME_TYPE] = FiMimeTypeValues.JSON.value
            input_tokens = self._attributes.get(SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS)
            output_tokens = self._attributes.get(SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS)
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                self._attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = input_tokens + output_tokens
            error = None if isinstance(obj, (StopIteration, StopAsyncIteration)) else obj
            _finish(self._span, error, self._attributes)
        return obj

    def _accumulate(self, payload: dict[str, Any]) -> None:
        # Match response schemas rather than model-id prefixes: inference-profile
        # IDs and ARNs also carry the same Meta, Titan, Cohere and Mistral chunks.
        for key in ("generation", "outputText", "completion", "text"):
            if isinstance(text := payload.get(key), str):
                self._text.append(text)
                break
        else:
            outputs = payload.get("outputs")
            if isinstance(outputs, list):
                self._text.extend(
                    output["text"] for output in outputs
                    if isinstance(output, dict) and isinstance(output.get("text"), str)
                )
            else:
                # Preserve structured output for schemas without a text adapter.
                self._payloads.append(payload)

        # Native counts and invocation metrics are running/final totals, not
        # per-chunk deltas. Keep the latest non-null value, including zero.
        for key in ("prompt_token_count", "inputTextTokenCount"):
            self._set_count(SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, payload.get(key))
        for key in ("generation_token_count", "totalOutputTextTokenCount"):
            self._set_count(SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, payload.get(key))
        metrics = payload.get("amazon-bedrock-invocationMetrics")
        if isinstance(metrics, dict):
            self._set_count(SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, metrics.get("inputTokenCount"))
            self._set_count(SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, metrics.get("outputTokenCount"))

    def _set_count(self, name: str, value: Any) -> None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            self._attributes[name] = value
