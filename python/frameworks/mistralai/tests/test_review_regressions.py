"""Regressions exercised through real SDK serialization and synthetic HTTP/SSE."""

import asyncio
import importlib
import json
from contextlib import contextmanager
from importlib.metadata import version

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_mistralai import MistralAIInstrumentor

NATIVE = version("mistralai").startswith("3.")
Mistral = importlib.import_module("mistralai.client" if NATIVE else "mistralai").Mistral
httpx = importlib.import_module("httpx2" if NATIVE else "httpx")

CHAT = {
    "id": "synthetic",
    "object": "chat.completion",
    "created": 1,
    "model": "mistral-test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
}
EMBED = {
    "id": "synthetic",
    "object": "list",
    "model": "mistral-embed",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
    "usage": {"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2},
}


class CaptureStarts(SpanProcessor):
    def __init__(self):
        self.spans = []

    def on_start(self, span, parent_context=None):
        self.spans.append(span)


@contextmanager
def tracing(**flags):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    starts = CaptureStarts()
    provider.add_span_processor(starts)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MistralAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**flags))
    try:
        yield exporter, starts
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


class EventStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, events, named=False):
        self.events, self.named = events, named

    def __iter__(self):
        for event in self.events:
            name = f"event: {event['type']}\n" if self.named else ""
            yield (name + "data: " + json.dumps(event) + "\n\n").encode()

    async def __aiter__(self):
        for item in self:
            yield item

    def close(self):
        pass

    async def aclose(self):
        pass


@pytest.mark.skipif(NATIVE, reason="New legacy embedding path")
@pytest.mark.parametrize(
    "flag", ["hide_input_text", "hide_input_messages", "hide_inputs"]
)
def test_legacy_embedding_content_privacy(flag):
    marker = "synthetic-private-embedding-input"
    with tracing(**{flag: True}) as (exporter, starts):
        with httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=EMBED))
        ) as transport:
            client = Mistral(api_key="synthetic-key", client=transport)
            client.embeddings.create(model="mistral-embed", inputs=[marker])
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert marker not in spans[0].to_json()


@pytest.mark.skipif(NATIVE, reason="New legacy embedding path")
def test_legacy_embedding_cancellation_finishes_span():
    with tracing() as (exporter, starts):

        async def exercise():
            async def respond(request):
                raise asyncio.CancelledError("synthetic-cancel")

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as transport:
                client = Mistral(api_key="synthetic-key", async_client=transport)
                with pytest.raises(asyncio.CancelledError):
                    await client.embeddings.create_async(
                        model="mistral-embed", inputs=["hello"]
                    )

        asyncio.run(exercise())
        assert len(starts.spans) == 1
        assert not starts.spans[0].is_recording()
        assert exporter.get_finished_spans()[0].status.status_code == StatusCode.ERROR


@pytest.mark.skipif(not NATIVE, reason="Native adapter")
def test_native_custom_endpoint_redacts_url_credentials():
    with tracing(hide_inputs=True, hide_outputs=True) as (exporter, starts):
        with httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=CHAT))
        ) as transport:
            client = Mistral(
                api_key="synthetic-key",
                client=transport,
                server_url="https://synthetic-user:synthetic-password@proxy.invalid",
            )
            client.chat.complete(
                model="mistral-test", messages=[{"role": "user", "content": "hello"}]
            )
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert "synthetic-password" not in spans[0].to_json()


@pytest.mark.skipif(not NATIVE, reason="Native conversation aggregation")
@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_conversation_accumulates_tool_arguments(asynchronous):
    events = [
        {"type": "conversation.response.started", "conversation_id": "conv-synthetic"},
        {
            "type": "function.call.delta",
            "id": "entry-1",
            "tool_call_id": "call-1",
            "output_index": 0,
            "name": "weather",
            "arguments": '{"city":',
        },
        {
            "type": "function.call.delta",
            "id": "entry-1",
            "tool_call_id": "call-1",
            "output_index": 0,
            "name": "weather",
            "arguments": '"Paris"}',
        },
        {
            "type": "conversation.response.done",
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        },
    ]

    def respond(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=EventStream(events, named=True),
        )

    with tracing() as (exporter, starts):
        if asynchronous:

            async def exercise():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    client = Mistral(api_key="synthetic-key", async_client=transport)
                    async with await client.beta.conversations.start_stream_async(
                        model="mistral-test", inputs="hello"
                    ) as stream:
                        assert len([event async for event in stream]) == 4

            asyncio.run(exercise())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                client = Mistral(api_key="synthetic-key", client=transport)
                with client.beta.conversations.start_stream(
                    model="mistral-test", inputs="hello"
                ) as stream:
                    assert len(list(stream)) == 4
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        parts = json.loads(spans[0].attributes["gen_ai.output.messages"])[0]["parts"]
        assert parts == [
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "weather",
                "arguments": '{"city":"Paris"}',
            }
        ]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_stream_decode_failure_is_error(asynchronous):
    def respond(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=EventStream([{"error": {"message": "synthetic-provider-failure"}}]),
        )

    with tracing() as (exporter, starts):
        if asynchronous:

            async def exercise():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    client = Mistral(api_key="synthetic-key", async_client=transport)
                    with pytest.raises(Exception) as raised:
                        async with await client.chat.stream_async(
                            model="mistral-test",
                            messages=[{"role": "user", "content": "hello"}],
                        ) as stream:
                            [event async for event in stream]
                    assert "Validation" in type(raised.value).__name__

            asyncio.run(exercise())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                client = Mistral(api_key="synthetic-key", client=transport)
                with pytest.raises(Exception) as raised:
                    with client.chat.stream(
                        model="mistral-test",
                        messages=[{"role": "user", "content": "hello"}],
                    ) as stream:
                        list(stream)
                assert "Validation" in type(raised.value).__name__
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR


@pytest.mark.skipif(not NATIVE, reason="Native kind classification")
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_agent_completion_kind(streaming, asynchronous):
    chunk = {
        "id": "synthetic",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mistral-test",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }

    def respond(request):
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=EventStream([chunk]),
            )
        return httpx.Response(200, json=CHAT)

    with tracing() as (exporter, starts):
        params = dict(
            agent_id="synthetic-agent", messages=[{"role": "user", "content": "hello"}]
        )
        if asynchronous:

            async def exercise():
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond)
                ) as transport:
                    client = Mistral(api_key="synthetic-key", async_client=transport)
                    if streaming:
                        async with await client.agents.stream_async(**params) as stream:
                            [event async for event in stream]
                    else:
                        await client.agents.complete_async(**params)

            asyncio.run(exercise())
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                client = Mistral(api_key="synthetic-key", client=transport)
                if streaming:
                    with client.agents.stream(**params) as stream:
                        list(stream)
                else:
                    client.agents.complete(**params)
        spans = exporter.get_finished_spans()
        assert len(spans) == len(starts.spans) == 1
        assert spans[0].attributes["gen_ai.span.kind"] == "LLM"


@pytest.mark.skipif(not NATIVE, reason="Previously reported native tool image issue")
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize(
    "image",
    ["https://example.invalid/private-image", "data:image/png;base64," + "A" * 80],
)
def test_previous_tool_image_privacy_fix(structured, image):
    chunks = [
        {"type": "text", "text": "allowed-tool-text"},
        {
            "type": "image_url",
            "image_url": {"url": image, "detail": "low"} if structured else image,
        },
    ]
    observed = []

    def respond(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=CHAT)

    with tracing(hide_input_images=True) as (exporter, starts):
        with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
            client = Mistral(api_key="synthetic-key", client=transport)
            client.chat.complete(
                model="mistral-test",
                messages=[
                    {"role": "tool", "tool_call_id": "call-image", "content": chunks}
                ],
            )
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        wire = spans[0].to_json()
        assert image not in wire
        assert "allowed-tool-text" in wire
        assert "call-image" in wire
        assert observed[0]["messages"][0]["content"] == chunks
