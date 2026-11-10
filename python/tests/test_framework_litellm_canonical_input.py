"""Regression coverage for T3-LITELLM-007 using offline completion fakes."""

import asyncio
from copy import deepcopy
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.mark.parametrize(
    "function_name",
    [
        "completion",
        "acompletion",
        "completion_with_retries",
        "acompletion_with_retries",
    ],
)
@pytest.mark.parametrize(
    "messages",
    [
        pytest.param(
            [{"role": "user", "content": "CURRENT_USER_QUESTION_MARKER"}],
            id="single-user",
        ),
        pytest.param(
            [
                {"role": "system", "content": "SYSTEM_INSTRUCTION_MARKER"},
                {"role": "user", "content": "CURRENT_USER_QUESTION_MARKER"},
            ],
            id="system-plus-user",
        ),
        pytest.param(
            [
                {"role": "user", "content": "PREVIOUS_USER_QUESTION_MARKER"},
                {"role": "assistant", "content": "PREVIOUS_ASSISTANT_ANSWER_MARKER"},
                {"role": "user", "content": "CURRENT_USER_QUESTION_MARKER"},
            ],
            id="multi-turn",
        ),
    ],
)
def test_completion_preserves_canonical_conversation(monkeypatch, function_name, messages):
    # Avoid LiteLLM's remote cost-map lookup when importing the real wrappers.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from fi_instrumentation.fi_types import SpanAttributes
    from traceai_litellm import LiteLLMInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    response = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
    is_async = function_name.startswith("a")
    original = (AsyncMock if is_async else Mock)(return_value=response)
    # Exercise the exact wrapper without installing global instrumentation.
    instrumentor = SimpleNamespace(
        _tracer=provider.get_tracer(__name__),
        original_litellm_funcs={function_name: original},
    )
    if hasattr(LiteLLMInstrumentor, "_get_call_arguments"):
        instrumentor._get_call_arguments = MethodType(
            LiteLLMInstrumentor._get_call_arguments, instrumentor
        )
    original_messages = deepcopy(messages)
    kwargs = {"model": "gpt-4o-mini", "messages": messages}

    try:
        wrapper = getattr(LiteLLMInstrumentor, f"_{function_name}_wrapper")
        result = wrapper(instrumentor, **kwargs)
        if is_async:
            result = asyncio.run(result)
            original.assert_awaited_once_with(**kwargs)
        else:
            original.assert_called_once_with(**kwargs)

        assert result is response
        assert messages == original_messages
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        # Assert the final exported value, not an earlier set_attribute call.
        assert spans[0].attributes[SpanAttributes.INPUT_VALUE] == json.dumps(
            original_messages
        )
    finally:
        provider.shutdown()
