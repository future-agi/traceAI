"""Research lifecycle through register() and its real OTLP processor/exporter."""

import asyncio

import pytest
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_tavily import TavilyInstrumentor

from test_extended_operations import CHUNKS, QUESTION, Transport
from test_extended_wire import receiver


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("termination", [
    "unstarted-close", "partial-close", "unstarted-throw", "partial-throw", "exhausted",
])
def test_research_stream_outcome_survives_register(monkeypatch, is_async, batch, termination):
    transport = Transport()
    monkeypatch.setenv("FI_API_KEY", "synthetic-fi-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-fi-secret-key")
    with receiver() as (endpoint, records):
        monkeypatch.setenv("FI_BASE_URL", endpoint.rsplit("/v1/traces", 1)[0])
        provider = register(
            project_name="tavily-stream-tests", project_type=ProjectType.OBSERVE,
            verbose=False, batch=batch,
        )
        instrumentor = TavilyInstrumentor()
        instrumentor.instrument(tracer_provider=provider)
        try:
            async def consume():
                client = await transport.async_client()
                try:
                    stream = await client.research(QUESTION, stream=True)
                    if termination == "exhausted":
                        assert [chunk async for chunk in stream] == CHUNKS
                    elif termination.startswith("partial"):
                        assert await stream.__anext__() == CHUNKS[0]
                    if termination.endswith("throw"):
                        with pytest.raises(GeneratorExit):
                            await stream.athrow(GeneratorExit())
                    await stream.aclose()
                    await stream.aclose()
                finally:
                    await client.close()

            if is_async:
                asyncio.run(consume())
            else:
                client = transport.sync_client(monkeypatch)
                try:
                    stream = client.research(QUESTION, stream=True)
                    if termination == "exhausted":
                        assert list(stream) == CHUNKS
                    elif termination.startswith("partial"):
                        assert next(stream) == CHUNKS[0]
                    if termination.endswith("throw"):
                        with pytest.raises(GeneratorExit):
                            stream.throw(GeneratorExit())
                    stream.close()
                    stream.close()
                finally:
                    client.close()
            assert provider.force_flush()
        finally:
            instrumentor.uninstrument()
            provider.shutdown()
    spans = [
        span for _, payload in records for resource in payload["resourceSpans"]
        for scope in resource["scopeSpans"] for span in scope["spans"]
    ]
    assert len(spans) == 1
    span = spans[0]
    assert span["name"] == "tavily.research"
    attrs = {attr["key"]: attr["value"] for attr in span["attributes"]}
    if termination == "exhausted":
        assert "tavily.stream.closed" not in attrs
        assert span["status"]["code"] == "STATUS_CODE_OK"
    else:
        assert attrs["tavily.stream.closed"] == {"boolValue": True}
        assert span["status"]["code"] == "STATUS_CODE_ERROR"
    # Early close is incomplete work, not a vendor exception.
    assert not span.get("events")
