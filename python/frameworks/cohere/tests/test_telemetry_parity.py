"""Real SDK -> OTLP regression contracts; synthetic credentials and HTTP only."""

import copy
import json

import cohere
import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cohere import CohereInstrumentor

from .test_sdk_coverage import (
    client_class,
    events,
    payload,
    request_args,
    wire_attributes,
)

PRIVATE = "SYNTHETIC_PRIVATE_CONTENT_346_PARITY"
CACHE_KEY = "gen_ai.usage.input_tokens.cache_read"
TOOL_KEY = "gen_ai.tool.definitions.0.tool.json_schema"
INPUT_DOC = "reranker.input_documents.0.document.content"
OUTPUT_DOC = "reranker.output_documents.0.document.content"
TTFT_KEY = "gen_ai.server.time_to_first_token"


@pytest.fixture
def capture(monkeypatch):
    # Keep each contract independent of the shell's configuration.
    for flag in (
        "hide_inputs",
        "hide_input_text",
        "hide_input_messages",
        "hide_outputs",
        "hide_output_text",
        "hide_output_messages",
        "hide_llm_invocation_parameters",
    ):
        monkeypatch.setenv("FI_" + flag.upper(), "false")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CohereInstrumentor()

    def start(flag=None, hidden=False, configuration="explicit"):
        options = {}
        if flag:
            # Prove that explicit config wins over the opposite environment.
            monkeypatch.setenv(
                "FI_" + flag.upper(),
                str(hidden if configuration == "environment" else not hidden).lower(),
            )
            if configuration == "explicit":
                options[flag] = hidden
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**options))
        return exporter

    yield start
    instrumentor.uninstrument()
    provider.shutdown()


async def invoke(version, asynchronous, operation, args, body=None, chunks=None):
    cls = client_class(version, asynchronous)
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        if chunks is not None:
            text = "".join(
                ("data: " if version == 2 else "")
                + json.dumps(event)
                + ("\n\n" if version == 2 else "\n")
                for event in chunks
            )
            return httpx.Response(
                200, text=text, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=body)

    if asynchronous:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = cls(api_key="synthetic-not-a-provider-key", httpx_client=http)
            if chunks is not None:
                result = [event async for event in client.chat_stream(**args)]
            else:
                result = await getattr(client, operation)(**args)
    else:
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = cls(api_key="synthetic-not-a-provider-key", httpx_client=http)
            result = getattr(client, operation)(**args)
            if chunks is not None:
                result = list(result)
    assert len(seen) == 1
    return result, seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("configuration", ["environment", "explicit"])
@pytest.mark.parametrize(
    "flag", ["hide_inputs", "hide_input_messages", "hide_input_text"]
)
@pytest.mark.parametrize("hidden", [False, True])
async def test_embedding_selective_privacy(
    capture, version, asynchronous, configuration, flag, hidden
):
    client_class(version, asynchronous)
    exporter = capture(flag, hidden, configuration)
    args = request_args(version, "embed")
    args["texts"] = [PRIVATE]
    original = copy.deepcopy(args)
    result, seen = await invoke(
        version, asynchronous, "embed", args, body=payload(version, "embed")
    )
    assert result.embeddings is not None
    assert seen["texts"] == [PRIVATE] and args == original
    _, wire, attrs = wire_attributes(exporter)
    assert attrs["cohere.texts_count"] == 1
    assert attrs["cohere.embeddings_count"] == 1
    assert (PRIVATE.encode() not in wire) == hidden
    if not hidden:
        assert attrs["embedding.embeddings.0.embedding.text"] == PRIVATE


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cached", [0, 6])
async def test_cached_usage_survives_content_privacy(
    capture, asynchronous, streaming, cached
):
    client_class(2, asynchronous)
    # Cache accounting is an SDK field only in newer releases.
    usage_type = getattr(cohere, "Usage", None)
    fields = getattr(usage_type, "model_fields", None) or getattr(
        usage_type, "__fields__", {}
    )
    if "cached_tokens" not in fields:
        pytest.skip("installed Cohere SDK predates usage.cached_tokens")
    exporter = capture("hide_outputs", True)
    body = copy.deepcopy(payload(2, "chat"))
    body["usage"]["cached_tokens"] = cached
    chunks = events(2) if streaming else None
    if chunks:
        chunks[-1]["delta"]["usage"] = body["usage"]
    await invoke(
        2,
        asynchronous,
        "chat_stream" if streaming else "chat",
        request_args(2, "chat"),
        body=body,
        chunks=chunks,
    )
    _, _, attrs = wire_attributes(exporter)
    assert attrs[CACHE_KEY] == cached
    # Billing and actual usage are deliberately different in the fixture.
    assert attrs["gen_ai.usage.input_tokens"] == 7
    assert attrs["gen_ai.usage.output_tokens"] == 3
    assert attrs["gen_ai.usage.total_tokens"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("sdk_object", [False, True])
@pytest.mark.parametrize("configuration", ["environment", "explicit"])
@pytest.mark.parametrize(
    "flag",
    [
        None,
        "hide_inputs",
        "hide_input_messages",
        "hide_input_text",
        "hide_llm_invocation_parameters",
    ],
)
async def test_tool_schema_capture(
    capture, version, asynchronous, sdk_object, configuration, flag
):
    client_class(version, asynchronous)
    exporter = capture(flag, flag is not None, configuration)
    if version == 1:
        schema = {
            "name": "lookup",
            "description": PRIVATE,
            "parameter_definitions": {
                "city": {"type": "str", "description": "city name", "required": True},
            },
        }
        tool = cohere.Tool(**schema) if sdk_object else schema
    else:
        schema = {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": PRIVATE,
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
        tool = cohere.ToolV2(**schema) if sdk_object else schema
    original = copy.deepcopy(tool)
    args = request_args(version, "chat")
    args["tools"] = [tool]
    _, seen = await invoke(
        version, asynchronous, "chat", args, body=payload(version, "chat")
    )
    sent = seen["tools"][0] if version == 1 else seen["tools"][0]["function"]
    assert sent["description"] == PRIVATE
    assert tool == original
    _, wire, attrs = wire_attributes(exporter)
    assert attrs["cohere.tools_count"] == 1
    if flag:
        assert PRIVATE.encode() not in wire
    else:
        captured = json.loads(attrs[TOOL_KEY])
        if version == 1:
            assert captured["description"] == PRIVATE
            assert (
                captured["parameter_definitions"]["city"]["description"] == "city name"
            )
        else:
            assert captured["function"]["description"] == PRIVATE
            assert (
                captured["function"]["parameters"] == schema["function"]["parameters"]
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("configuration", ["environment", "explicit"])
@pytest.mark.parametrize(
    "flag",
    [
        None,
        "hide_inputs",
        "hide_input_messages",
        "hide_input_text",
        "hide_outputs",
        "hide_output_text",
        "hide_output_messages",
    ],
)
async def test_rerank_document_capture(
    capture, version, asynchronous, configuration, flag
):
    client_class(version, asynchronous)
    exporter = capture(flag, flag is not None, configuration)
    args = dict(
        model="rerank-v3.5",
        query="public query",
        documents=["first candidate", PRIVATE],
    )
    original = copy.deepcopy(args)
    body = payload(version, "rerank")
    body["results"] = [
        {"index": 1, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.2},
    ]
    result, seen = await invoke(version, asynchronous, "rerank", args, body=body)
    assert seen["documents"] == args["documents"] and args == original
    assert result.results[0].index == 1
    _, wire, attrs = wire_attributes(exporter)
    assert attrs["cohere.documents_count"] == 2
    assert attrs["cohere.rerank.0.index"] == 1
    assert attrs["cohere.rerank.0.relevance_score"] == 0.9
    if flag in ("hide_inputs", "hide_input_text", "hide_input_messages"):
        assert PRIVATE.encode() not in wire
    else:
        assert attrs[INPUT_DOC] == "first candidate"
        assert attrs[INPUT_DOC.replace(".0.", ".1.")] == PRIVATE
        if flag:
            assert PRIVATE not in str(attrs.get(OUTPUT_DOC, ""))
        else:
            assert attrs[OUTPUT_DOC] == PRIVATE


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_large_rerank_preserves_sdk_payload(capture, asynchronous):
    client_class(2, asynchronous)
    exporter = capture()
    documents = ["x" * 5000 + PRIVATE for _ in range(25)]
    args = dict(model="rerank-v3.5", query="public query", documents=documents)
    body = payload(2, "rerank")
    body["results"] = [{"index": i, "relevance_score": 0.9} for i in range(25)]
    result, seen = await invoke(2, asynchronous, "rerank", args, body=body)
    assert seen["documents"] == documents
    assert len(result.results) == 25 and result.results[-1].index == 24
    _, wire, attrs = wire_attributes(exporter)
    assert attrs["cohere.documents_count"] == attrs["cohere.results_count"] == 25
    assert attrs[INPUT_DOC] == attrs[OUTPUT_DOC] == "x" * 4096
    assert attrs["cohere.rerank.input_documents_truncated"] is True
    assert attrs["cohere.rerank.output_documents_truncated"] is True
    assert PRIVATE.encode() not in wire
    assert len(wire) < 100_000


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_stream_first_token_metric(capture, version, asynchronous):
    client_class(version, asynchronous)
    exporter = capture("hide_outputs", True)
    await invoke(
        version,
        asynchronous,
        "chat_stream",
        request_args(version, "chat"),
        chunks=events(version),
    )
    _, _, attrs = wire_attributes(exporter)
    assert isinstance(attrs[TTFT_KEY], float) and attrs[TTFT_KEY] >= 0
