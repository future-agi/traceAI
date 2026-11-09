"""Typed SDK tool calls obey input-text privacy without changing the request."""
import copy
import json

import httpx
import ollama
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_ollama import OllamaInstrumentor


MARKER = "SYNTHETIC_TYPED_TOOL_ARGUMENT_426"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("shape", ["message", "tool_call", "function"])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("configuration", ["environment", "explicit"])
async def test_typed_tool_input_text_privacy(
    monkeypatch, asynchronous, stream, shape, hidden, configuration
):
    message = ollama.Message(
        role="assistant",
        content="previous tool invocation",
        tool_calls=[{"function": {"name": "lookup", "arguments": {"query": MARKER}}}],
    )
    # Current SDKs return models; the SDK floor represents these as dictionaries.
    # Also exercise mappings containing either a typed call or a typed function.
    if shape != "message":
        message = dict(message)
    if shape == "function":
        message["tool_calls"] = [dict(message["tool_calls"][0])]
    before = copy.deepcopy(message)
    requests = []
    response = {
        "model": "synthetic-model", "done": True,
        "message": {"role": "assistant", "content": "complete"},
    }

    def respond(request):
        requests.append(json.loads(request.content))
        if stream:
            return httpx.Response(200, content=json.dumps(response) + "\n")
        return httpx.Response(200, json=response)

    monkeypatch.setenv("FI_HIDE_INPUT_TEXT", str(hidden).lower())
    config = None
    if configuration == "explicit":
        monkeypatch.setenv("FI_HIDE_INPUT_TEXT", str(not hidden).lower())
        config = TraceConfig(hide_input_text=hidden)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OllamaInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    client_type = ollama.AsyncClient if asynchronous else ollama.Client
    client = client_type(
        host="http://offline.invalid", transport=httpx.MockTransport(respond), trust_env=False
    )
    try:
        result = client.chat(model="synthetic-model", messages=[message], stream=stream)
        if asynchronous:
            result = await result
            if stream:
                result = [chunk async for chunk in result][-1]
        elif stream:
            result = list(result)[-1]
        received = result if isinstance(result, dict) else result.model_dump()
        assert received["message"]["content"] == "complete"
        assert len(requests) == 1
        assert requests[0]["messages"][0]["tool_calls"][0]["function"]["arguments"] == {
            "query": MARKER
        }
        assert message == before
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        # Scan the complete protobuf, including resource fields and events.
        wire = encode_spans(spans).SerializeToString()
        attributes = dict(spans[0].attributes)
        if hidden:
            assert MARKER.encode() not in wire
            tool = json.loads(attributes["input.value"])["messages"][0]["tool_calls"][0]
            assert tool["function"]["name"] == "lookup"
            assert tool["function"]["arguments"] == REDACTED_VALUE
        else:
            assert MARKER.encode() in wire
            assert MARKER in attributes["input.value"]
    finally:
        if asynchronous:
            await client._client.aclose()
        else:
            client._client.close()
        instrumentor.uninstrument()
        provider.shutdown()
