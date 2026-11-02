"""Independent round-3 SDK and OTLP probes. Synthetic data and transport only."""

import copy
import inspect
import json

import cohere
import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_cohere import CohereInstrumentor

SECRET = "SYNTHETIC_PRIVATE_DOCUMENT_ROUND3_346"
API_KEY = "synthetic-only-not-a-real-api-key-346"


@pytest.fixture
def tracing():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CohereInstrumentor()

    def setup(**privacy):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
        return provider, exporter

    yield setup
    instrumentor.uninstrument()
    provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("configuration", ["explicit", "environment"])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("version", [1, 2])
async def test_rerank_return_documents_respects_input_privacy(
    tracing, monkeypatch, asynchronous, configuration, hidden, version
):
    name = ("AsyncClient" if asynchronous else "Client") + (
        "V2" if version == 2 else ""
    )
    if not hasattr(cohere, name):
        pytest.skip("v2 not available at SDK floor")
    cls = getattr(cohere, name)
    if "return_documents" not in inspect.signature(cls.rerank).parameters:
        pytest.skip("this SDK removed v2 return_documents")
    # This is an SDK-declared input echo, not generated output text.
    monkeypatch.setenv(
        "FI_HIDE_INPUTS",
        str(hidden if configuration == "environment" else not hidden).lower(),
    )
    provider, exporter = tracing(
        **({"hide_inputs": hidden} if configuration == "explicit" else {})
    )
    document = {"text": SECRET, "title": "synthetic document"}
    original = copy.deepcopy(document)
    body = {
        "id": "synthetic",
        "results": [{"index": 0, "relevance_score": 0.9, "document": document}],
        "meta": {"billed_units": {"search_units": 1}},
    }
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer " + API_KEY
        return httpx.Response(200, json=body)

    request = dict(
        model="rerank-v3.5",
        query="Find the relevant document",
        documents=[document if version == 1 else SECRET],
        return_documents=True,
    )
    with provider.get_tracer("review").start_as_current_span("parent") as parent:
        if asynchronous:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                client = cls(api_key=API_KEY, httpx_client=http)
                result = await client.rerank(**request)
        else:
            with httpx.Client(transport=httpx.MockTransport(handle)) as http:
                client = cls(api_key=API_KEY, httpx_client=http)
                result = client.rerank(**request)
        assert trace.get_current_span() is parent
        assert len(seen) == 1 and seen[0]["documents"] == request["documents"]
        assert result.results[0].document.text == SECRET
        assert document == original
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.attributes["cohere.results_count"] == 1
        assert span.attributes["cohere.rerank.0.relevance_score"] == 0.9
        wire = encode_spans(spans).SerializeToString()
        assert API_KEY.encode() not in wire
        leaked = [key for key, value in span.attributes.items() if SECRET in str(value)]
        assert (SECRET.encode() not in wire) == hidden, (
            f"hide_inputs={hidden}; copied input leaked via {leaked}"
        )
