"""Regression coverage for LiteLLM failures before an async stream is created."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def test_acompletion_stream_setup_preserves_provider_exception(monkeypatch):
    # Avoid LiteLLM's remote cost-map lookup when importing the provider library.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm
    from fi_instrumentation import FITracer, TraceConfig
    from traceai_litellm import LiteLLMInstrumentor

    failure = litellm.RateLimitError(
        message="synthetic rate limit",
        llm_provider="openai",
        model="gpt-4o-mini",
    )
    mock_acompletion = AsyncMock(side_effect=failure)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LiteLLMInstrumentor()
    monkeypatch.setattr(
        instrumentor,
        "_tracer",
        FITracer(provider.get_tracer(__name__), config=TraceConfig()),
        raising=False,
    )
    monkeypatch.setattr(
        instrumentor, "original_litellm_funcs", {"acompletion": mock_acompletion}
    )
    kwargs = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }

    async def invoke():
        token = context_api.attach(
            context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, False)
        )
        previous_span = trace_api.get_current_span()
        try:
            with pytest.raises(litellm.RateLimitError) as caught:
                await instrumentor._acompletion_wrapper(**kwargs)
            assert caught.value is failure
            assert trace_api.get_current_span() is previous_span
        finally:
            context_api.detach(token)

    try:
        asyncio.run(invoke())
        mock_acompletion.assert_awaited_once_with(**kwargs)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "acompletion"
        assert spans[0].status.status_code is trace_api.StatusCode.ERROR
    finally:
        provider.shutdown()
