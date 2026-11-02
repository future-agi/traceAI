"""Citation-start parity and privacy, exercised through the real v2 SDK."""

import json

import cohere
import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_cohere import CohereInstrumentor


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("source_kind", ["document", "tool"])
@pytest.mark.parametrize("citation_count", [1, 2])
@pytest.mark.parametrize(
    "hide_flag", [None, "hide_inputs", "hide_input_messages", "hide_input_text"]
)
async def test_v2_stream_citations(
    asynchronous, source_kind, citation_count, hide_flag
):
    client_name = "AsyncClientV2" if asynchronous else "ClientV2"
    if not hasattr(cohere, client_name):
        pytest.skip("v2 unavailable at the declared SDK floor")
    secret = "SYNTHETIC_PRIVATE_CITATION_SOURCE_346"
    source = {"type": source_kind, "id": "source-1"}
    source["document" if source_kind == "document" else "tool_output"] = {
        "text": secret
    }
    citation = {"start": 0, "end": 5, "text": "hello", "sources": [source]}
    events = [
        {
            "type": "message-start",
            "id": "synthetic",
            "delta": {"message": {"role": "assistant"}},
        },
        {
            "type": "content-delta",
            "index": 0,
            "delta": {"message": {"content": {"text": "hello"}}},
        },
    ]
    for index in range(citation_count):
        events.extend(
            [
                {
                    "type": "citation-start",
                    "index": index,
                    "delta": {"message": {"citations": citation}},
                },
                {"type": "citation-end", "index": index},
            ]
        )
    events.append({"type": "message-end", "delta": {"finish_reason": "COMPLETE"}})
    original_events = json.loads(json.dumps(events))
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CohereInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(**({hide_flag: True} if hide_flag else {})),
    )

    def handle(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join("data: " + json.dumps(event) + "\n\n" for event in events),
        )

    try:
        request = {
            "model": "synthetic-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        if asynchronous:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                client = cohere.AsyncClientV2(
                    api_key="synthetic-only", httpx_client=http
                )
                returned = [event async for event in client.chat_stream(**request)]
        else:
            with httpx.Client(transport=httpx.MockTransport(handle)) as http:
                client = cohere.ClientV2(api_key="synthetic-only", httpx_client=http)
                returned = list(client.chat_stream(**request))
        assert events == original_events
        returned_citations = [
            event for event in returned if event.type == "citation-start"
        ]
        assert len(returned_citations) == citation_count
        assert all(secret in str(event) for event in returned_citations)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        attributes = spans[0].attributes
        assert attributes["cohere.citations_count"] == citation_count
        output = json.loads(attributes["output.value"])
        assert len(output["message"]["citations"]) == citation_count
        assert all(
            item["sources"][0]["id"] == "source-1"
            for item in output["message"]["citations"]
        )
        wire = encode_spans(spans).SerializeToString()
        assert (secret.encode() not in wire) == bool(hide_flag)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
