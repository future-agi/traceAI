"""Real SDK dispatch and OTLP serialization, with synthetic HTTP only."""

import asyncio
import json

import cohere
import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cohere import CohereInstrumentor

INPUT = "PRIVATE_INPUT_4829"
OUTPUT = "PRIVATE_OUTPUT_7251"
TOOL_ARGUMENT = "PRIVATE_TOOL_6723"
USAGE = {
    "tokens": {"input_tokens": 7, "output_tokens": 3},
    "billed_units": {"input_tokens": 5, "output_tokens": 2},
}


def payload(version, operation):
    if operation == "embed":
        embeddings = [[0.1, 0.2]] if version == 1 else {"float": [[0.1, 0.2]]}
        return {
            "id": "offline",
            "texts": [INPUT],
            "embeddings": embeddings,
            "response_type": "embeddings_floats"
            if version == 1
            else "embeddings_by_type",
            "meta": {"billed_units": {"input_tokens": 7}},
        }
    if operation == "rerank":
        return {
            "id": "offline",
            "results": [{"index": 0, "relevance_score": 0.9}],
            "meta": {"billed_units": {"search_units": 1}},
        }
    if version == 1:
        return {
            "text": OUTPUT,
            "generation_id": "00000000-0000-0000-0000-000000000001",
            "meta": USAGE,
            "finish_reason": "COMPLETE",
            "tool_calls": [{"name": "lookup", "parameters": {"query": TOOL_ARGUMENT}}],
        }
    return {
        "id": "offline",
        "finish_reason": "COMPLETE",
        "usage": USAGE,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": OUTPUT}],
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "arguments": json.dumps({"query": TOOL_ARGUMENT}),
                    },
                }
            ],
        },
    }


def events(version):
    if version == 1:
        return [
            {"event_type": "text-generation", "text": OUTPUT},
            {
                "event_type": "stream-end",
                "finish_reason": "COMPLETE",
                "response": payload(1, "chat"),
            },
        ]
    return [
        {
            "type": "message-start",
            "id": "offline",
            "delta": {"message": {"role": "assistant"}},
        },
        {
            "type": "content-start",
            "index": 0,
            "delta": {"message": {"content": {"type": "text", "text": ""}}},
        },
        {
            "type": "content-delta",
            "index": 0,
            "delta": {"message": {"content": {"text": OUTPUT}}},
        },
        {
            "type": "tool-call-start",
            "index": 0,
            "delta": {
                "message": {
                    "tool_calls": {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": ""},
                    }
                }
            },
        },
        {
            "type": "tool-call-delta",
            "index": 0,
            "delta": {
                "message": {"tool_calls": {"function": {"arguments": '{"query": '}}}
            },
        },
        {
            "type": "tool-call-delta",
            "index": 0,
            "delta": {
                "message": {
                    "tool_calls": {
                        "function": {"arguments": json.dumps(TOOL_ARGUMENT) + "}"}
                    }
                }
            },
        },
        {"type": "message-end", "delta": {"finish_reason": "COMPLETE", "usage": USAGE}},
    ]


def request_args(version, operation):
    if operation == "embed":
        args = dict(model="embed-v4.0", texts=[INPUT], input_type="search_document")
        if version == 2:
            args["embedding_types"] = ["float"]
        return args
    if operation == "rerank":
        return dict(model="rerank-v3.5", query=INPUT, documents=["offline"])
    if version == 1:
        return dict(model="command-r", message=INPUT)
    return dict(model="command-r", messages=[{"role": "user", "content": INPUT}])


def client_class(version, asynchronous):
    name = ("AsyncClient" if asynchronous else "Client") + (
        "V2" if version == 2 else ""
    )
    if not hasattr(cohere, name):
        pytest.skip("Cohere 5.0.0 predates v2 clients")
    return getattr(cohere, name)


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


def wire_attributes(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    wire = encode_spans(spans).SerializeToString()
    decoded = ExportTraceServiceRequest.FromString(wire)
    span = decoded.resource_spans[0].scope_spans[0].spans[0]
    attrs = {
        a.key: getattr(a.value, a.value.WhichOneof("value")) for a in span.attributes
    }
    return spans[0], wire, attrs


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["chat", "chat_stream", "embed", "rerank"])
@pytest.mark.parametrize("hidden", [False, True])
async def test_sdk_surfaces_and_wire(tracing, version, asynchronous, operation, hidden):
    cls = client_class(version, asynchronous)
    _, exporter = tracing(hide_inputs=hidden, hide_outputs=hidden)
    requests = []

    def respond(request):
        requests.append(request)
        if operation == "chat_stream":
            chunks = events(version)
            text = "".join(
                ("data: " if version == 2 else "")
                + json.dumps(e)
                + ("\n\n" if version == 2 else "\n")
                for e in chunks
            )
            return httpx.Response(
                200, text=text, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=payload(version, operation))

    args = request_args(version, operation)
    if asynchronous:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = cls(api_key="offline-placeholder", httpx_client=http)
            if operation == "chat_stream":
                response = [e async for e in client.chat_stream(**args)]
            else:
                response = await getattr(client, operation)(**args)
    else:
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = cls(api_key="offline-placeholder", httpx_client=http)
            response = getattr(client, operation)(**args)
            if operation == "chat_stream":
                response = list(response)
    assert response is not None
    assert len(requests) == 1
    span, wire, attrs = wire_attributes(exporter)
    assert span.name == "cohere." + operation
    assert span.status.status_code == trace.StatusCode.OK
    assert attrs["gen_ai.provider.name"] == "cohere"
    if hidden:
        for private in (INPUT, OUTPUT, TOOL_ARGUMENT):
            assert private.encode() not in wire
    elif operation.startswith("chat"):
        assert attrs["gen_ai.input.messages.0.message.content"] == INPUT
        assert attrs["gen_ai.output.messages.0.message.content"] == OUTPUT
        assert (
            TOOL_ARGUMENT
            in attrs[
                "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments"
            ]
        )
    if operation.startswith("chat"):
        assert attrs["gen_ai.request.model"] == "command-r"
        assert attrs["gen_ai.usage.input_tokens"] == 7
        assert attrs["gen_ai.usage.output_tokens"] == 3
        assert attrs["gen_ai.usage.total_tokens"] == 10
    elif operation == "embed":
        assert attrs["cohere.embeddings_count"] == 1
        assert attrs["cohere.embedding_dimensions"] == 2
    else:
        assert attrs["cohere.results_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_suppression_and_uninstrument(tracing, version, asynchronous):
    cls = client_class(version, asynchronous)
    originals = {
        method: getattr(cls, method)
        for method in ("chat", "chat_stream", "embed", "rerank")
    }
    _, exporter = tracing()
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=payload(version, "chat"))
    )
    if asynchronous:
        async with httpx.AsyncClient(transport=transport) as http:
            client = cls(api_key="offline", httpx_client=http)
            with suppress_instrumentation():
                await client.chat(**request_args(version, "chat"))
    else:
        with httpx.Client(transport=transport) as http:
            client = cls(api_key="offline", httpx_client=http)
            with suppress_instrumentation():
                client.chat(**request_args(version, "chat"))
    assert not exporter.get_finished_spans()
    CohereInstrumentor().uninstrument()
    for method, original in originals.items():
        assert getattr(cls, method) is original
    # A second cycle must neither retain wrappers nor double-instrument inherited methods.
    tracing()
    CohereInstrumentor().uninstrument()
    for method, original in originals.items():
        assert getattr(cls, method) is original


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "termination", ["exhaust", "close", "unstarted_close", "error", "cancel"]
)
async def test_stream_lifecycle_context_and_passthrough(
    tracing, asynchronous, termination
):
    from traceai_cohere._wrappers import _AsyncChatStreamWrapper, _ChatStreamWrapper

    provider, exporter = tracing(hide_outputs=True)
    seen = []
    closed = []
    event = {"event_type": "text-generation", "text": OUTPUT}
    error = (
        asyncio.CancelledError(OUTPUT)
        if termination == "cancel"
        else RuntimeError(OUTPUT)
    )

    def source():
        try:
            seen.append(trace.get_current_span().get_span_context().span_id)
            yield event
            if termination in ("error", "cancel"):
                raise error
        finally:
            closed.append(True)

    async def async_source():
        try:
            seen.append(trace.get_current_span().get_span_context().span_id)
            yield event
            if termination in ("error", "cancel"):
                raise error
        finally:
            closed.append(True)

    tracer = CohereInstrumentor()._tracer
    wrapper = (_AsyncChatStreamWrapper if asynchronous else _ChatStreamWrapper)(
        tracer=tracer
    )
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        parent_id = parent.get_span_context().span_id
        stream = wrapper(async_source if asynchronous else source, None, (), {})
        assert trace.get_current_span().get_span_context().span_id == parent_id
        assert not exporter.get_finished_spans()
        if termination != "unstarted_close":
            got = await stream.__anext__() if asynchronous else next(stream)
            assert got is event
            assert trace.get_current_span().get_span_context().span_id == parent_id
            assert not exporter.get_finished_spans()
        if termination == "exhaust":
            with pytest.raises(StopAsyncIteration if asynchronous else StopIteration):
                if asynchronous:
                    await stream.__anext__()
                else:
                    next(stream)
        elif termination in ("error", "cancel"):
            with pytest.raises(type(error)) as caught:
                if asynchronous:
                    await stream.__anext__()
                else:
                    next(stream)
            assert caught.value is error
        if asynchronous:
            await stream.aclose()
            await stream.aclose()
        else:
            stream.close()
            stream.close()
        assert trace.get_current_span().get_span_context().span_id == parent_id
        span, wire, _ = wire_attributes(exporter)
        assert span.parent.span_id == parent_id
        if seen:
            assert seen == [span.context.span_id]
            assert closed == [True]
        assert OUTPUT.encode() not in wire
        expected = (
            trace.StatusCode.ERROR
            if termination in ("error", "cancel")
            else (
                trace.StatusCode.OK
                if termination == "exhaust"
                else trace.StatusCode.UNSET
            )
        )
        assert span.status.status_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_sdk_errors_are_preserved_and_private(
    tracing, version, asynchronous, streaming
):
    cls = client_class(version, asynchronous)
    _, exporter = tracing(hide_inputs=True, hide_outputs=True)
    error = httpx.ConnectError(OUTPUT)
    requests = []

    def respond(request):
        requests.append(request)
        raise error

    args = request_args(version, "chat")
    # Cohere 5.0.0 configures retries per request; no real network is used.
    args["request_options"] = {"max_retries": 0}
    with pytest.raises(httpx.ConnectError) as caught:
        if asynchronous:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as http:
                client = cls(api_key="offline", httpx_client=http)
                if streaming:
                    _ = [e async for e in client.chat_stream(**args)]
                else:
                    await client.chat(**args)
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                client = cls(api_key="offline", httpx_client=http)
                if streaming:
                    list(client.chat_stream(**args))
                else:
                    client.chat(**args)
    assert caught.value is error
    assert len(requests) == 1
    span, wire, _ = wire_attributes(exporter)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert OUTPUT.encode() not in wire


def test_v2_typed_messages_and_null_usage(tracing):
    from traceai_cohere._request_attributes_extractor import (
        _ChatRequestAttributesExtractor,
    )
    from traceai_cohere._response_attributes_extractor import (
        _ChatResponseAttributesExtractor,
    )

    if not hasattr(cohere, "UserChatMessageV2"):
        pytest.skip("Typed v2 messages are absent at the SDK floor")
    _, exporter = tracing()
    request = {
        "messages": [
            cohere.UserChatMessageV2(role="user", content=INPUT),
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "lookup", "arguments": TOOL_ARGUMENT},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
        ],
        "documents": None,
        "tools": None,
    }
    extractor = _ChatRequestAttributesExtractor()
    with CohereInstrumentor()._tracer.start_as_current_span("typed-message") as span:
        span.set_attributes(dict(extractor.get_extra_attributes_from_request(request)))
    _, wire, attrs = wire_attributes(exporter)
    assert attrs["gen_ai.input.messages.0.message.content"] == INPUT
    assert (
        attrs["gen_ai.input.messages.1.message.tool_calls.0.tool_call.id"] == "call-1"
    )
    assert attrs["gen_ai.input.messages.2.message.tool_call_id"] == "call-1"
    assert TOOL_ARGUMENT.encode() in wire
    response = _ChatResponseAttributesExtractor()
    assert dict(response.get_attributes({"usage": None, "meta": None})) == {}
    zero = dict(
        response.get_attributes(
            {"usage": {"tokens": {"input_tokens": 0, "output_tokens": 0}}}
        )
    )
    assert zero["gen_ai.usage.total_tokens"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_abandoned_stream_finishes_span(tracing, asynchronous):
    import gc
    from traceai_cohere._wrappers import _AsyncChatStreamWrapper, _ChatStreamWrapper

    _, exporter = tracing()

    def source():
        yield {"event_type": "text-generation", "text": OUTPUT}

    async def async_source():
        yield {"event_type": "text-generation", "text": OUTPUT}

    wrapper = (_AsyncChatStreamWrapper if asynchronous else _ChatStreamWrapper)(
        tracer=CohereInstrumentor()._tracer
    )
    stream = wrapper(async_source if asynchronous else source, None, (), {})
    if asynchronous:
        await stream.__anext__()
    else:
        next(stream)
    assert not exporter.get_finished_spans()
    del stream
    gc.collect()
    assert len(exporter.get_finished_spans()) == 1


def test_unknown_event_extraction_does_not_break_stream(tracing):
    from traceai_cohere._wrappers import _ChatStreamWrapper

    _, exporter = tracing()

    class UnknownEvent:
        def model_dump(self):
            raise ValueError("unrecognized event")

    event = UnknownEvent()
    wrapper = _ChatStreamWrapper(tracer=CohereInstrumentor()._tracer)
    stream = wrapper(lambda: iter([event]), None, (), {})
    assert list(stream) == [event]
    span, _, _ = wire_attributes(exporter)
    assert span.status.status_code == trace.StatusCode.OK
