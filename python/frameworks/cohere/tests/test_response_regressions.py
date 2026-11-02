"""Response privacy and native failure contracts using real SDKs and OTLP."""

from copy import deepcopy
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

SECRET = "SYNTHETIC_PRIVATE_SOURCE_346_ROUND2"
OUTPUT = "A safe short answer"


@pytest.fixture
def tracing():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CohereInstrumentor()

    def setup(**privacy):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
        return exporter

    yield setup
    instrumentor.uninstrument()
    provider.shutdown()


def client_cls(version, asynchronous):
    name = ("AsyncClient" if asynchronous else "Client") + (
        "V2" if version == 2 else ""
    )
    if not hasattr(cohere, name):
        pytest.skip("v2 not present at SDK floor")
    return getattr(cohere, name)


def args(version):
    return dict(
        model="synthetic-model",
        **(
            {"message": "Summarize the document"}
            if version == 1
            else {"messages": [{"role": "user", "content": "Summarize the document"}]}
        ),
    )


def response_body(version, finish="COMPLETE"):
    if version == 1:
        return {"text": OUTPUT, "finish_reason": finish}
    return {
        "id": "synthetic-response",
        "finish_reason": finish,
        "message": {"role": "assistant", "content": [{"type": "text", "text": OUTPUT}]},
    }


def response(version, body, streaming):
    if not streaming:
        return httpx.Response(200, json=body)
    if version == 1:
        events = [
            {"event_type": "text-generation", "text": OUTPUT},
            {
                "event_type": "stream-end",
                "finish_reason": body["finish_reason"],
                "response": body,
            },
        ]
    else:
        events = [
            {
                "type": "message-start",
                "id": "synthetic-response",
                "delta": {"message": {"role": "assistant"}},
            },
            {
                "type": "content-delta",
                "index": 0,
                "delta": {"message": {"content": {"text": OUTPUT}}},
            },
            {"type": "message-end", "delta": {"finish_reason": body["finish_reason"]}},
        ]
    text = "".join(
        ("data: " if version == 2 else "")
        + json.dumps(e)
        + ("\n\n" if version == 2 else "\n")
        for e in events
    )
    return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})


async def invoke(version, asynchronous, streaming, request, handler):
    cls = client_cls(version, asynchronous)
    transport = httpx.MockTransport(handler)
    if asynchronous:
        async with httpx.AsyncClient(transport=transport) as http:
            client = cls(api_key="synthetic-no-real-credential", httpx_client=http)
            return (
                [e async for e in client.chat_stream(**request)]
                if streaming
                else await client.chat(**request)
            )
    with httpx.Client(transport=transport) as http:
        client = cls(api_key="synthetic-no-real-credential", httpx_client=http)
        return (
            list(client.chat_stream(**request)) if streaming else client.chat(**request)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("version,streaming", [(1, False), (1, True), (2, False)])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize(
    "flag", ["hide_inputs", "hide_input_messages", "hide_input_text"]
)
@pytest.mark.parametrize("configuration", ["explicit", "environment"])
async def test_input_document_echo_is_hidden(
    tracing, monkeypatch, version, asynchronous, streaming, hidden, flag, configuration
):
    client_cls(version, asynchronous)
    monkeypatch.setenv(
        "FI_" + flag.upper(),
        str(hidden if configuration == "environment" else not hidden).lower(),
    )
    exporter = tracing(**({flag: hidden} if configuration == "explicit" else {}))
    request = args(version)
    document = {"id": "doc-1", "text": SECRET}
    request["documents"] = [
        document if version == 1 else cohere.Document(id="doc-1", data={"text": SECRET})
    ]
    body = response_body(version)
    if version == 1:
        body["documents"] = [document]
    else:
        body["message"]["citations"] = [
            {
                "start": 0,
                "end": len(OUTPUT),
                "text": OUTPUT,
                "sources": [{"type": "document", "id": "doc-1", "document": document}],
            }
        ]
    seen = []

    def handle(r):
        seen.append(json.loads(r.content))
        return response(version, body, streaming)

    result = await invoke(version, asynchronous, streaming, request, handle)
    assert len(seen) == 1 and SECRET in json.dumps(seen[0])
    assert SECRET in str(result)  # SDK-visible data must remain unchanged.
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    wire = encode_spans(spans).SerializeToString()
    assert OUTPUT.encode() in wire
    leaking = [k for k, v in spans[0].attributes.items() if SECRET in str(v)]
    assert (SECRET.encode() not in wire) == hidden, (
        f"hide_inputs={hidden}; echoed input leaked via {leaking}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize(
    "flag", ["hide_inputs", "hide_input_messages", "hide_input_text"]
)
@pytest.mark.parametrize("configuration", ["explicit", "environment"])
async def test_tool_result_citation_echo_is_hidden(
    tracing, monkeypatch, asynchronous, hidden, flag, configuration
):
    client_cls(2, asynchronous)
    monkeypatch.setenv(
        "FI_" + flag.upper(),
        str(hidden if configuration == "environment" else not hidden).lower(),
    )
    exporter = tracing(**({flag: hidden} if configuration == "explicit" else {}))
    request = args(2)
    request["messages"] = [
        {"role": "user", "content": "Look up the record"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "tool-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "tool-1",
            "content": json.dumps({"text": SECRET}),
        },
    ]
    body = response_body(2)
    body["message"]["citations"] = [
        {
            "start": 0,
            "end": len(OUTPUT),
            "text": OUTPUT,
            "sources": [
                {"type": "tool", "id": "tool-1", "tool_output": {"text": SECRET}}
            ],
        }
    ]
    seen = []

    def handle(r):
        seen.append(json.loads(r.content))
        return response(2, body, False)

    result = await invoke(2, asynchronous, False, request, handle)
    assert len(seen) == 1 and SECRET in json.dumps(seen[0])
    assert SECRET in str(result)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    wire = encode_spans(spans).SerializeToString()
    leaking = [k for k, v in spans[0].attributes.items() if SECRET in str(v)]
    assert (SECRET.encode() not in wire) == hidden, (
        f"hide_inputs={hidden}; input tool result leaked via {leaking}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize(
    "version,finish,failed",
    [
        (1, "ERROR", True),
        (1, "ERROR_TOXIC", True),
        (1, "ERROR_LIMIT", True),
        (2, "ERROR", True),
        (2, "TIMEOUT", True),
        (1, "COMPLETE", False),
        (2, "COMPLETE", False),
        (1, "MAX_TOKENS", False),
        (2, "MAX_TOKENS", False),
        (2, "STOP_SEQUENCE", False),
        (2, "TOOL_CALL", False),
    ],
)
async def test_native_failure_status(
    tracing, version, asynchronous, streaming, hidden, finish, failed
):
    client_cls(version, asynchronous)
    exporter = tracing(hide_error_details=hidden)
    body = response_body(version, finish)
    result = await invoke(
        version,
        asynchronous,
        streaming,
        args(version),
        lambda r: response(version, body, streaming),
    )
    terminal = result[-1] if streaming else result
    if streaming and version == 2:
        terminal = terminal.delta
    assert terminal.finish_reason == finish
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    expected = trace.StatusCode.ERROR if failed else trace.StatusCode.OK
    assert spans[0].status.status_code == expected, (
        f"{finish} => {spans[0].status.status_code}; error.type={spans[0].attributes.get('error.type')}"
    )
    assert spans[0].attributes.get("error.type") == (finish if failed else None)
    assert spans[0].attributes["cohere.finish_reason"] == finish
    assert not spans[0].events  # Native failures do not raise SDK exceptions.
    assert not trace.get_current_span().is_recording()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "flag", ["hide_inputs", "hide_input_messages", "hide_input_text"]
)
def test_input_echo_sanitization_never_mutates_response(version, flag):
    from traceai_cohere._response_attributes_extractor import (
        _ChatResponseAttributesExtractor,
    )

    body = response_body(version)
    body["documents"] = [{"text": SECRET}]
    body["chat_history"] = [{"role": "USER", "message": SECRET}]
    container = body if version == 1 else body["message"]
    container["citations"] = [
        {
            "start": 0,
            "end": len(OUTPUT),
            "text": OUTPUT,
            "sources": [
                {"type": "document", "id": "doc-1", "document": {"text": SECRET}},
                {"type": "tool", "id": "tool-1", "tool_output": {"text": SECRET}},
            ],
        }
    ]
    original = deepcopy(body)
    extractor = _ChatResponseAttributesExtractor(TraceConfig(**{flag: True}))
    attributes = dict(extractor.get_extra_attributes(body, {}))
    assert body == original
    assert SECRET not in json.dumps(attributes)
    output = json.loads(attributes["output.value"])
    assert "documents" not in output and "chat_history" not in output
    citation = (output if version == 1 else output["message"])["citations"][0]
    assert citation["text"] == OUTPUT
    assert citation["sources"] == [
        {"type": "document", "id": "doc-1"},
        {"type": "tool", "id": "tool-1"},
    ]
    assert attributes["cohere.citations_count"] == 1
