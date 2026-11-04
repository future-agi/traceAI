"""Offline tests exercise the native SDK over its real HTTP transport interface."""
import asyncio
import json

import anthropic
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_anthropic import AnthropicInstrumentor

try:
    import httpx2 as httpx
except ImportError:
    import httpx


MESSAGE = {
    "id": "msg_local", "type": "message", "role": "assistant",
    "content": [{"type": "text", "text": "offline result"}],
    "model": "local-test", "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 4, "output_tokens": 2},
}


def fixture_provider():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def transport_handler(request):
    assert json.loads(request.content)["model"] == "local-test"
    return httpx.Response(200, json=MESSAGE)


def test_real_sync_sdk_matches_untraced_and_exports_one_span():
    provider, exporter = fixture_provider()
    client = anthropic.Anthropic(api_key="local-test", http_client=httpx.Client(transport=httpx.MockTransport(transport_handler)))
    params = dict(model="local-test", max_tokens=16, messages=[{"role": "user", "content": "hello"}])
    expected = client.messages.create(**params).model_dump()
    instrumentor = AnthropicInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        actual = client.messages.create(**params).model_dump()
        assert actual == expected
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
    finally:
        instrumentor.uninstrument()
        client.close()
        provider.shutdown()


def test_real_async_sdk_matches_untraced_and_exports_one_span():
    async def run():
        provider, exporter = fixture_provider()
        client = anthropic.AsyncAnthropic(api_key="local-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport_handler)))
        params = dict(model="local-test", max_tokens=16, messages=[{"role": "user", "content": "hello"}])
        expected = (await client.messages.create(**params)).model_dump()
        instrumentor = AnthropicInstrumentor()
        instrumentor.instrument(tracer_provider=provider)
        try:
            actual = (await client.messages.create(**params)).model_dump()
            assert actual == expected
            assert len(exporter.get_finished_spans()) == 1
        finally:
            instrumentor.uninstrument()
            await client.close()
            provider.shutdown()
    asyncio.run(run())


def test_real_sdk_error_is_not_swallowed():
    def fail(request):
        return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "offline error"}})
    provider, exporter = fixture_provider()
    client = anthropic.Anthropic(api_key="local-test", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(fail)))
    instrumentor = AnthropicInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        with pytest.raises(anthropic.BadRequestError, match="offline error"):
            client.messages.create(model="local-test", max_tokens=16, messages=[{"role": "user", "content": "hello"}])
        assert len(exporter.get_finished_spans()) == 1
    finally:
        instrumentor.uninstrument()
        client.close()
        provider.shutdown()


def test_real_async_sdk_error_is_not_swallowed():
    async def run():
        def fail(request):
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "offline error"}})
        provider, exporter = fixture_provider()
        client = anthropic.AsyncAnthropic(api_key="local-test", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)))
        instrumentor = AnthropicInstrumentor()
        instrumentor.instrument(tracer_provider=provider)
        try:
            with pytest.raises(anthropic.BadRequestError, match="offline error"):
                await client.messages.create(model="local-test", max_tokens=16, messages=[{"role": "user", "content": "hello"}])
            assert len(exporter.get_finished_spans()) == 1
        finally:
            instrumentor.uninstrument()
            await client.close()
            provider.shutdown()
    asyncio.run(run())
