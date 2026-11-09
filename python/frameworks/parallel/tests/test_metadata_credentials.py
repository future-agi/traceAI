"""Provider metadata still obeys the credential and PII policy."""

import asyncio

import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from parallel import AsyncParallel, Parallel
from traceai_parallel import ParallelInstrumentor


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("pii", [False, True])
def test_metadata_redacts_before_capping_without_mutating_response(async_, pii):
    key = "synthetic-metadata-key"
    email = "synthetic.person@example.com"
    text = "x" * 240 + key + " " + email
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = ParallelInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(pii_redaction=pii))

    def handler(request):
        assert request.headers["x-api-key"] == key
        return httpx.Response(200, json={"search_id": "synthetic-search", "results": [],
            "usage": [{"name": text, "count": 1}, {"name": email, "count": 2}],
            "warnings": [{"type": text, "message": "safe"}, {"type": email, "message": "safe"}]})

    def check(result):
        assert result.usage[0].name == text
        assert result.warnings[0].type == text

    async def run():
        async with AsyncParallel(api_key=key, max_retries=0, http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), trust_env=False)) as client:
            check(await client.search(search_queries=["safe"]))

    try:
        if async_:
            asyncio.run(run())
        else:
            with Parallel(api_key=key, max_retries=0, http_client=httpx.Client(
                    transport=httpx.MockTransport(handler), trust_env=False)) as client:
                check(client.search(search_queries=["safe"]))
        span, = exporter.get_finished_spans()
        # A cap at 256 bytes would leave only the beginning of the credential.
        assert "synthetic-meta" not in span.to_json()
        assert (email not in span.to_json()) is pii
        assert span.attributes["parallel.usage.counts"] == (1, 2)
        assert len(span.attributes["parallel.usage.names"][0].encode()) <= 256
        assert len(span.events[0].attributes["parallel.warning.type"].encode()) <= 256
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
