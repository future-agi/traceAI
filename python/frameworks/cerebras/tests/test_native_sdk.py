"""Real SDK + MockTransport contracts. No API keys or network are used."""

import json
from importlib.util import find_spec

import httpx
import pytest
from cerebras.cloud.sdk import AsyncCerebras, Cerebras
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_cerebras import CerebrasInstrumentor

HAS_TEXT = find_spec("cerebras.cloud.sdk.resources.completions") is not None
KINDS = [
    "chat",
    pytest.param(
        "text",
        marks=pytest.mark.skipif(
            not HAS_TEXT, reason="SDK 1.0.0 has no text completions"
        ),
    ),
]
USAGE = {
    "prompt_tokens": 2,
    "completion_tokens": 3,
    "total_tokens": 5,
    "prompt_tokens_details": {"cached_tokens": 1},
    "completion_tokens_details": {"reasoning_tokens": 2},
}
TIMING = {
    "queue_time": 0.1,
    "prompt_time": 0.2,
    "completion_time": 0.3,
    "total_time": 0.6,
}
CALL = {
    "id": "call_fixture",
    "type": "function",
    "function": {"name": "lookup", "arguments": '{"city":"private-city"}'},
}


def response_body(kind="chat", streaming=False):
    base = {
        "id": "fixture",
        "model": "resolved-model",
        "created": 1,
        "object": "chat.completion" if kind == "chat" else "text_completion",
        "system_fingerprint": "fixture",
        "usage": USAGE,
        "time_info": TIMING,
    }
    message = {
        "role": "assistant",
        "content": "private-answer",
        "reasoning": "private-reasoning",
        "tool_calls": [CALL],
    }
    choice = {"index": 0, "finish_reason": "tool_calls" if kind == "chat" else "stop"}
    choice.update(
        {"message": message} if kind == "chat" else {"text": "private-answer"}
    )
    base["choices"] = [choice]
    if not streaming:
        return base
    chunks = []
    for index, text in enumerate(["private-", "answer"]):
        chunk = {
            k: v for k, v in base.items() if k not in ("choices", "usage", "time_info")
        }
        chunk["object"] = (
            "chat.completion.chunk" if kind == "chat" else "text_completion"
        )
        choice = {"index": 0, "finish_reason": None}
        if kind == "chat":
            tool = {
                "index": 0,
                "type": "function",
                "function": {"arguments": ['{"city":', '"private-city"}'][index]},
            }
            if index == 0:
                tool.update(id="call_fixture", type="function")
                tool["function"]["name"] = "lookup"
            choice["delta"] = {
                "content": text,
                "reasoning": ["private-", "reasoning"][index],
                "tool_calls": [tool],
            }
        else:
            choice["text"] = text
        chunk["choices"] = [choice]
        chunks.append(chunk)
    # Usage need not be on the final chunk. An empty trailing chunk must not erase it.
    chunks.append(
        {
            **base,
            "object": chunks[0]["object"],
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    **({"delta": {}} if kind == "chat" else {"text": ""}),
                }
            ],
        }
    )
    chunks.append(
        {
            "id": "fixture",
            "model": "resolved-model",
            "created": 1,
            "object": chunks[0]["object"],
            "choices": [],
            "system_fingerprint": "fixture",
        }
    )
    return chunks


def transport(kind, streaming=False, requests=None):
    def handle(request):
        if requests is not None:
            requests.append(json.loads(request.content))
        if streaming:
            data = "".join(
                f"data: {json.dumps(chunk)}\n\n" for chunk in response_body(kind, True)
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=data + "data: [DONE]\n\n",
            )
        return httpx.Response(200, json=response_body(kind))

    return httpx.MockTransport(handle)


def params(kind, stream=False):
    return {
        "model": "requested-model",
        "stream": stream,
        **(
            {"messages": [{"role": "user", "content": "private-question"}]}
            if kind == "chat"
            else {"prompt": "private-question"}
        ),
    }


def resource(client, kind):
    return client.chat.completions if kind == "chat" else client.completions


@pytest.fixture
def tracing(request):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CerebrasInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider, config=TraceConfig(**getattr(request, "param", {}))
    )
    yield exporter
    instrumentor.uninstrument()
    provider.shutdown()


def attributes(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.OK
    # Verify OTLP protobuf serialization, not only mock attribute assignment.
    wire = encode_spans(spans).SerializeToString()
    decoded = ExportTraceServiceRequest.FromString(wire)
    span = decoded.resource_spans[0].scope_spans[0].spans[0]
    assert span.end_time_unix_nano >= span.start_time_unix_nano
    attrs = {
        a.key: (
            a.value.string_value
            if a.value.WhichOneof("value") == "string_value"
            else a.value.int_value
            if a.value.WhichOneof("value") == "int_value"
            else a.value.double_value
        )
        for a in span.attributes
    }
    return attrs, wire


def assert_completion(exporter, kind):
    attrs, _ = attributes(exporter)
    assert attrs["gen_ai.provider.name"] == "cerebras"
    assert attrs["gen_ai.request.model"] == "requested-model"
    assert attrs["gen_ai.response.model"] == "resolved-model"
    assert attrs["gen_ai.operation.name"] == (
        "chat" if kind == "chat" else "text_completion"
    )
    assert attrs["gen_ai.output.messages.0.message.content"] == "private-answer"
    assert attrs["gen_ai.usage.total_tokens"] == 5
    assert attrs["gen_ai.usage.input_tokens.cache_read"] == 1
    assert attrs["gen_ai.usage.output_tokens.reasoning"] == 2
    for key, value in TIMING.items():
        assert attrs[f"cerebras.{key}"] == value
    if kind == "chat":
        assert (
            attrs[
                "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"
            ]
            == "lookup"
        )
        assert json.loads(
            attrs[
                "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments"
            ]
        ) == {"city": "private-city"}
        assert (
            attrs["gen_ai.output.messages.0.message.contents.0.message_content.text"]
            == "private-reasoning"
        )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("streaming", [False, True])
def test_sync_native_completion(tracing, kind, streaming):
    with Cerebras(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.Client(transport=transport(kind, streaming)),
    ) as client:
        result = resource(client, kind).create(**params(kind, streaming))
        if streaming:
            assert not tracing.get_finished_spans()
            chunks = list(result)
            assert len(chunks) == 4
        else:
            assert result.model == "resolved-model"
    assert_completion(tracing, kind)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_async_native_completion(tracing, kind, streaming):
    async with AsyncCerebras(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=transport(kind, streaming)),
    ) as client:
        result = await resource(client, kind).create(**params(kind, streaming))
        if streaming:
            assert not tracing.get_finished_spans()
            chunks = [chunk async for chunk in result]
            assert len(chunks) == 4
        else:
            assert result.model == "resolved-model"
    assert_completion(tracing, kind)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("consume", [False, True])
def test_sync_stream_context_and_close(tracing, kind, consume):
    with Cerebras(
        api_key="offline-fixture",
        http_client=httpx.Client(transport=transport(kind, True)),
    ) as client:
        stream = resource(client, kind).create(**params(kind, True))
        with stream as entered:
            assert entered is stream
            if consume:
                next(stream)
        assert stream.response.is_closed
        stream.close()
    attrs, _ = attributes(tracing)
    if consume:
        assert attrs["gen_ai.output.messages.0.message.content"] == "private-"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("consume", [False, True])
async def test_async_stream_context_and_close(tracing, kind, consume):
    async with AsyncCerebras(
        api_key="offline-fixture",
        http_client=httpx.AsyncClient(transport=transport(kind, True)),
    ) as client:
        stream = await resource(client, kind).create(**params(kind, True))
        async with stream as entered:
            assert entered is stream
            if consume:
                await stream.__anext__()
        assert stream.response.is_closed
        await stream.close()
    attrs, _ = attributes(tracing)
    if consume:
        assert attrs["gen_ai.output.messages.0.message.content"] == "private-"


@pytest.mark.parametrize(
    "tracing", [{"hide_inputs": True, "hide_outputs": True}], indirect=True
)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("streaming", [False, True])
def test_private_otlp(tracing, kind, streaming):
    with Cerebras(
        api_key="offline-fixture",
        http_client=httpx.Client(transport=transport(kind, streaming)),
    ) as client:
        result = resource(client, kind).create(**params(kind, streaming))
        if streaming:
            list(result)
    attrs, wire = attributes(tracing)
    for secret in (
        b"private-question",
        b"private-answer",
        b"private-reasoning",
        b"private-city",
    ):
        assert secret not in wire
    assert attrs["gen_ai.provider.name"] == "cerebras"
    assert attrs["gen_ai.usage.total_tokens"] == 5
    assert attrs["cerebras.total_time"] == 0.6


def test_request_tools_and_transport_metadata(tracing):
    sent = []
    with Cerebras(
        api_key="offline-fixture",
        http_client=httpx.Client(transport=transport("chat", requests=sent)),
    ) as client:
        client.chat.completions.create(
            model="requested-model",
            messages=iter(
                [
                    {"role": "assistant", "content": None, "tool_calls": iter([CALL])},
                    {
                        "role": "tool",
                        "tool_call_id": "call_fixture",
                        "content": "tool-result",
                    },
                ]
            ),
            tools=[
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
            extra_headers={"X-Fixture-Secret": "transport-secret"},
        )
    assert len(sent[0]["messages"]) == 2
    assert sent[0]["messages"][0]["tool_calls"] == [CALL]
    attrs, wire = attributes(tracing)
    assert (
        attrs["gen_ai.input.messages.0.message.tool_calls.0.tool_call.id"]
        == "call_fixture"
    )
    assert attrs["gen_ai.input.messages.1.message.tool_call_id"] == "call_fixture"
    assert "lookup" in attrs["gen_ai.tool.definitions.0.tool.json_schema"]
    assert b"transport-secret" not in wire


def test_uninstrument_restores_all_resources(tracing):
    instrumentor = CerebrasInstrumentor()
    instrumentor.uninstrument()
    for kind in ["chat", "text"] if HAS_TEXT else ["chat"]:
        with Cerebras(
            api_key="offline-fixture",
            http_client=httpx.Client(transport=transport(kind)),
        ) as client:
            resource(client, kind).create(**params(kind))
    assert not tracing.get_finished_spans()


@pytest.mark.parametrize("kind", KINDS)
def test_explicit_close_before_iteration(tracing, kind):
    with Cerebras(
        api_key="offline-fixture",
        http_client=httpx.Client(transport=transport(kind, True)),
    ) as client:
        stream = resource(client, kind).create(**params(kind, True))
        stream.close()
        stream.close()
        assert stream.response.is_closed
    attributes(tracing)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_async_explicit_close_before_iteration(tracing, kind):
    async with AsyncCerebras(
        api_key="offline-fixture",
        http_client=httpx.AsyncClient(transport=transport(kind, True)),
    ) as client:
        stream = await resource(client, kind).create(**params(kind, True))
        await stream.close()
        await stream.aclose()
        assert stream.response.is_closed
    attributes(tracing)


def failure_transport(streaming):
    def handle(request):
        error = {"error": {"message": "private-error", "type": "fixture_error"}}
        if streaming:
            data = f"data: {json.dumps(response_body('chat', True)[0])}\n\ndata: {json.dumps(error)}\n\n"
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=data
            )
        return httpx.Response(400, json=error)

    return httpx.MockTransport(handle)


def assert_error(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].attributes["gen_ai.request.model"] == "requested-model"
    wire = encode_spans(spans).SerializeToString()
    assert b"private-error" not in wire


@pytest.mark.parametrize(
    "tracing", [{"hide_inputs": True, "hide_outputs": True}], indirect=True
)
@pytest.mark.parametrize("streaming", [False, True])
def test_sync_errors_preserve_exception_and_end_once(tracing, streaming):
    from cerebras.cloud.sdk import APIStatusError

    with Cerebras(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.Client(transport=failure_transport(streaming)),
    ) as client:
        with pytest.raises(APIStatusError, match="private-error"):
            result = client.chat.completions.create(**params("chat", streaming))
            if streaming:
                list(result)
        if streaming:
            assert result.response.is_closed
            result.close()
    assert_error(tracing)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tracing", [{"hide_inputs": True, "hide_outputs": True}], indirect=True
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_async_errors_preserve_exception_and_end_once(tracing, streaming):
    from cerebras.cloud.sdk import APIStatusError

    async with AsyncCerebras(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=failure_transport(streaming)),
    ) as client:
        with pytest.raises(APIStatusError, match="private-error"):
            result = await client.chat.completions.create(**params("chat", streaming))
            if streaming:
                async for _ in result:
                    pass
        if streaming:
            assert result.response.is_closed
            await result.close()
    assert_error(tracing)


@pytest.mark.asyncio
async def test_async_cancellation_closes_stream(tracing):
    import asyncio

    class CancelledBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield f"data: {json.dumps(response_body('chat', True)[0])}\n\n".encode()
            raise asyncio.CancelledError()

        async def aclose(self):
            self.closed = True

    body = CancelledBody()

    def handle(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    async with AsyncCerebras(
        api_key="offline-fixture",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as client:
        result = await client.chat.completions.create(**params("chat", True))
        with pytest.raises(asyncio.CancelledError):
            async for _ in result:
                pass
        assert body.closed
        await result.close()
    spans = tracing.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].attributes["gen_ai.output.messages.0.message.content"] == "private-"


def test_suppression_and_duplicate_instrumentation(tracing):
    from opentelemetry import context as context_api
    from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY

    CerebrasInstrumentor().instrument()
    with Cerebras(
        api_key="offline-fixture", http_client=httpx.Client(transport=transport("chat"))
    ) as client:
        token = context_api.attach(
            context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True)
        )
        try:
            client.chat.completions.create(**params("chat"))
        finally:
            context_api.detach(token)
        assert not tracing.get_finished_spans()
        client.chat.completions.create(**params("chat"))
    assert_completion(tracing, "chat")


def test_multiple_choices_and_interleaved_tools():
    from traceai_cerebras._wrappers import _get_stream_output_attributes

    chunks = [
        {
            "choices": [
                {
                    "index": 1,
                    "delta": {
                        "content": "second",
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "second-call",
                                "function": {"name": "two", "arguments": "{"},
                            },
                            {
                                "index": 0,
                                "id": "first-call",
                                "function": {"name": "one", "arguments": "["},
                            },
                        ],
                    },
                },
                {"index": 0, "delta": {"content": "first"}},
            ]
        },
        {
            "choices": [
                {
                    "index": 1,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": "]"}},
                            {"index": 1, "function": {"arguments": "}"}},
                        ]
                    },
                }
            ]
        },
    ]
    attrs = _get_stream_output_attributes(chunks)
    assert attrs["gen_ai.output.messages.0.message.content"] == "first"
    assert attrs["gen_ai.output.messages.1.message.content"] == "second"
    assert (
        attrs[
            "gen_ai.output.messages.1.message.tool_calls.0.tool_call.function.arguments"
        ]
        == "[]"
    )
    assert (
        attrs[
            "gen_ai.output.messages.1.message.tool_calls.1.tool_call.function.arguments"
        ]
        == "{}"
    )


@pytest.mark.parametrize("kind", KINDS)
def test_raw_response_keeps_sdk_type_without_transport_headers(tracing, kind):
    with Cerebras(
        api_key="offline-fixture", http_client=httpx.Client(transport=transport(kind))
    ) as client:
        result = resource(client, kind).with_raw_response.create(
            **params(kind), extra_headers={"X-Fixture-Secret": "raw-transport-secret"}
        )
        assert result.parse().model == "resolved-model"
    _, wire = attributes(tracing)
    assert b"raw-transport-secret" not in wire
