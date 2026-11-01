"""Real Cohere rerank calls with offline HTTP and exported-span assertions."""
import json

import cohere
import httpx
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cohere import CohereInstrumentor


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hide_inputs", [False, True])
@pytest.mark.parametrize("configuration", ["explicit", "environment"])
async def test_rerank_query_privacy(monkeypatch, asynchronous, hide_inputs, configuration):
    query = "PRIVATE_RERANK_QUERY_7219"
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "offline-rerank", "results": [{"index": 0, "relevance_score": 0.9}],
            "meta": {"api_version": {"version": "1"}},
        })

    monkeypatch.setenv("FI_HIDE_INPUTS", str(hide_inputs).lower())
    # Explicit configuration must win over the opposite environment setting.
    config = None
    if configuration == "explicit":
        monkeypatch.setenv("FI_HIDE_INPUTS", str(not hide_inputs).lower())
        config = TraceConfig(hide_inputs=hide_inputs)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CohereInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        kwargs = dict(model="rerank-v3.5", query=query, documents=["offline document"])
        if asynchronous:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                client = cohere.AsyncClient(api_key="offline-placeholder", httpx_client=http)
                response = await client.rerank(**kwargs)
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                client = cohere.Client(api_key="offline-placeholder", httpx_client=http)
                response = client.rerank(**kwargs)
        assert len(requests) == 1
        assert requests[0]["query"] == query
        assert response.results[0].relevance_score == 0.9
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attributes = dict(spans[0].attributes)
        assert attributes["cohere.documents_count"] == 1
        assert attributes["reranker.model_name"] == "rerank-v3.5"
        if hide_inputs:
            assert attributes["input.value"] == REDACTED_VALUE
            assert attributes["reranker.query"] == REDACTED_VALUE
            assert query not in json.dumps(attributes)
        else:
            assert attributes["input.value"] == query
            assert attributes["reranker.query"] == query
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
