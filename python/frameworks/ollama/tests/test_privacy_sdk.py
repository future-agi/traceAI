"""Independent embedding-output flags through real Ollama HTTP clients."""
import json

import httpx
import ollama
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_ollama import OllamaInstrumentor


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("endpoint", ["embed", "embeddings"])
@pytest.mark.parametrize("hide_outputs", [False, True])
@pytest.mark.parametrize("hide_vectors", [False, True])
@pytest.mark.parametrize("configuration", ["explicit", "environment"])
async def test_independent_embedding_privacy(
    monkeypatch, asynchronous, endpoint, hide_outputs, hide_vectors, configuration
):
    vectors = [[0.314159, 0.271828, 0.141421] * 4, [0.577215] * 12]
    body = ({"model": "offline-embed", "embeddings": vectors, "prompt_eval_count": 3, "total_duration": 123}
            if endpoint == "embed" else {"embedding": vectors[0]})
    requests = []

    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=body)

    monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hide_outputs).lower())
    monkeypatch.setenv("FI_HIDE_EMBEDDING_VECTORS", str(hide_vectors).lower())
    config = None
    if configuration == "explicit":
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(not hide_outputs).lower())
        monkeypatch.setenv("FI_HIDE_EMBEDDING_VECTORS", str(not hide_vectors).lower())
        config = TraceConfig(hide_outputs=hide_outputs, hide_embedding_vectors=hide_vectors)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OllamaInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        kwargs = {"model": "offline-embed"}
        kwargs.update({"input": ["first", "second"]} if endpoint == "embed" else {"prompt": "first"})
        if asynchronous:
            client = ollama.AsyncClient(host="http://localhost:11434", transport=httpx.MockTransport(respond))
            try:
                response = await getattr(client, endpoint)(**kwargs)
            finally:
                await client._client.aclose()
        else:
            client = ollama.Client(host="http://localhost:11434", transport=httpx.MockTransport(respond))
            try:
                response = getattr(client, endpoint)(**kwargs)
            finally:
                client._client.close()
        assert len(requests) == 1
        assert requests[0][0] == "/api/" + endpoint
        received = response.model_dump()
        key = "embeddings" if endpoint == "embed" else "embedding"
        assert received[key] == body[key]  # Redaction must not mutate the SDK result.
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attributes = dict(spans[0].attributes)
        vector_key = "embedding.embeddings.0.embedding.vector"
        if hide_vectors:
            assert not any("embedding.vector" in key for key in attributes)
            for value in ("0.314159", "0.271828", "0.141421", "0.577215"):
                assert value not in json.dumps(attributes)
        else:
            # hide_outputs and hide_embedding_vectors have independent semantics.
            assert json.loads(attributes[vector_key]) == vectors[0][:10]
            if endpoint == "embed":
                assert json.loads(attributes["embedding.embeddings.1.embedding.vector"]) == vectors[1][:10]
        if hide_outputs:
            assert attributes["output.value"] == REDACTED_VALUE
        else:
            payload = json.loads(attributes["output.value"])
            expected = dict(received)
            if hide_vectors:
                expected[key] = REDACTED_VALUE
            assert payload == expected
        if endpoint == "embed":
            assert attributes["gen_ai.usage.input_tokens"] == 3
            assert attributes["ollama.total_duration_ns"] == 123
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
