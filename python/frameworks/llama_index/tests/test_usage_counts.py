"""Native Anthropic input excludes cache counts; OpenAI input includes them."""

from types import SimpleNamespace

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_llamaindex import LlamaIndexInstrumentor
from traceai_llamaindex._usage import token_counts


@pytest.mark.parametrize("as_object", [False, True])
@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 3, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}, 18),
    ({"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, 0),
    ({"input_tokens": 0, "cache_creation_input_tokens": 5}, 5),
    ({"input_tokens": 3}, 3),
    ({"input_tokens": 3, "cache_read_input_tokens": -10, "cache_creation_input_tokens": True}, 3),
    ({"input_tokens": 3, "cache_read_input_tokens": "10", "cache_creation_input_tokens": 5.5}, 3),
    ({"cache_read_input_tokens": 10}, None),
    ({"input_tokens": None, "cache_read_input_tokens": 10}, None),
    ({"input_tokens": False, "cache_read_input_tokens": 10}, None),
    ({"input_tokens": -3, "cache_read_input_tokens": 10}, None),
    ({"input_tokens": "3", "cache_read_input_tokens": 10}, None),
    ({"prompt_tokens": 18, "prompt_tokens_details": {"cached_tokens": 10}}, 18),
    ({"input_tokens": 18, "input_tokens_details": {"cached_tokens": 10}}, 18),
    ({"prompt_tokens": 18, "input_tokens": 3, "cache_read_input_tokens": 10}, 18),
    ({}, None),
])
def test_input_total_only_adds_valid_native_cache_counts(as_object, usage, expected):
    if as_object:
        usage = SimpleNamespace(**usage)
    result = dict(token_counts(usage))
    assert result.get("gen_ai.usage.input_tokens") == expected
    assert "gen_ai.usage.total_tokens" not in result


@pytest.mark.parametrize("as_mapping", [False, True])
def test_actual_anthropic_sdk_usage_object(as_mapping):
    Usage = pytest.importorskip("anthropic.types").Usage
    usage = Usage(input_tokens=3, output_tokens=2, cache_read_input_tokens=10,
                  cache_creation_input_tokens=5)
    if as_mapping:
        usage = usage.model_dump()
    assert dict(token_counts(usage)) == {
        "gen_ai.usage.input_tokens": 18, "gen_ai.usage.output_tokens": 2,
        "gen_ai.usage.cache_read_tokens": 10, "gen_ai.usage.cache_write_tokens": 5,
    }


def test_actual_anthropic_adapter_cached_usage(monkeypatch):
    adapter = pytest.importorskip("llama_index.llms.anthropic.base")
    from llama_index.core.llms import ChatMessage

    def respond(request):
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "synthetic-only"
        return httpx.Response(200, json={
            "id": "msg_synthetic", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5-20250929",
            "content": [{"type": "text", "text": "synthetic answer"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 3, "output_tokens": 2,
                      "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5},
        })

    real_client = adapter.anthropic.Anthropic
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LlamaIndexInstrumentor()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def make_client(**kwargs):
            kwargs["http_client"] = client
            return real_client(**kwargs)
        monkeypatch.setattr(adapter.anthropic, "Anthropic", make_client)
        instrumentor.instrument(tracer_provider=provider)
        try:
            llm = adapter.Anthropic(model="claude-sonnet-4-5-20250929",
                                    api_key="synthetic-only", max_retries=0)
            response = llm.chat([ChatMessage(role="user", content="synthetic question")])
            assert response.message.content == "synthetic answer"
            result, = [s for s in exporter.get_finished_spans() if s.name.endswith(".chat")]
            assert result.attributes["gen_ai.usage.input_tokens"] == 18
            assert result.attributes["gen_ai.usage.cache_read_tokens"] == 10
            assert result.attributes["gen_ai.usage.cache_write_tokens"] == 5
        finally:
            instrumentor.uninstrument()
            provider.shutdown()
