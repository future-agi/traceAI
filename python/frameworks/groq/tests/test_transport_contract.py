"""Public SDK contracts, using the real client and only an in-memory HTTP transport."""

import asyncio
import json
import socket

import groq
import httpx
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_groq import GroqInstrumentor


MESSAGES = [{"role": "user", "content": "private-request-text"}]
OUTPUT = "private-response-text"
SYNTHETIC_KEY = "synthetic-not-a-credential"
COMPLETION = {
    "id": "synthetic",
    "object": "chat.completion",
    "created": 1,
    "model": "synthetic-model",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": OUTPUT,
    }}],
}
CHUNK = {
    "id": "synthetic",
    "object": "chat.completion.chunk",
    "created": 1,
    "model": "synthetic-model",
    "choices": [{"index": 0, "finish_reason": None, "delta": {
        "role": "assistant", "content": OUTPUT,
    }}],
}
SSE = ("data: " + json.dumps(CHUNK) + "\n\n").encode()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Only MockTransport is permitted")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(params=["uninstrumented", "default", "hidden"])
def telemetry(request):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    mode = request.param
    instrumentor = GroqInstrumentor()
    if mode != "uninstrumented":
        instrumentor.instrument(
            tracer_provider=provider,
            config=TraceConfig(hide_inputs=mode == "hidden", hide_outputs=mode == "hidden"),
        )
    try:
        yield mode, exporter
    finally:
        if mode != "uninstrumented":
            instrumentor.uninstrument()
        provider.shutdown()


def assert_spans(telemetry, status=None):
    mode, exporter = telemetry
    spans = exporter.get_finished_spans()
    if mode == "uninstrumented":
        assert not spans
        return
    assert len(spans) == 1
    assert spans[0].end_time is not None
    if status is not None:
        assert spans[0].status.status_code == status
    encoded = json.dumps(dict(spans[0].attributes), default=str)
    assert SYNTHETIC_KEY not in encoded
    if mode == "hidden":
        assert MESSAGES[0]["content"] not in encoded
        assert OUTPUT not in encoded


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("kind", ["list", "iterator", "generator"])
def test_messages_reach_transport(telemetry, asynchronous, kind):
    wire = []

    def handle(request):
        wire.append(json.loads(request.content))
        return httpx.Response(200, json=COMPLETION)

    messages = list(MESSAGES)
    if kind == "iterator":
        messages = iter(messages)
    elif kind == "generator":
        messages = (message for message in messages)
    kwargs = {"model": "synthetic-model", "messages": messages}
    if asynchronous:
        async def run():
            async with groq.AsyncGroq(
                api_key=SYNTHETIC_KEY, max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            ) as client:
                return await client.chat.completions.create(**kwargs)
        response = asyncio.run(run())
    else:
        with groq.Groq(
            api_key=SYNTHETIC_KEY, max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ) as client:
            response = client.chat.completions.create(**kwargs)
    assert wire[0]["messages"] == MESSAGES
    assert response.choices[0].message.content == OUTPUT
    assert_spans(telemetry, StatusCode.OK)
    mode, exporter = telemetry
    if kind == "list" and mode == "default":
        assert json.loads(exporter.get_finished_spans()[0].attributes[SpanAttributes.INPUT_VALUE]) == MESSAGES


class SyncBody(httpx.SyncByteStream):
    def __init__(self):
        self.pulls = 0
        self.closes = 0

    def __iter__(self):
        self.pulls += 1
        yield SSE
        self.pulls += 1
        yield b"data: [DONE]\n\n"

    def close(self):
        self.closes += 1


class AsyncBody(httpx.AsyncByteStream):
    def __init__(self):
        self.pulls = 0
        self.closes = 0

    async def __aiter__(self):
        self.pulls += 1
        yield SSE
        self.pulls += 1
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closes += 1


class ConsumerError(Exception):
    pass


ACTIONS = ["close_empty", "close_one", "context_empty", "context_one", "context_error", "exhaust"]


@pytest.mark.parametrize("action", ACTIONS)
def test_sync_stream_lifecycle(telemetry, action):
    body = SyncBody()
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
    with groq.Groq(
        api_key=SYNTHETIC_KEY, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: response)),
    ) as client:
        stream = client.chat.completions.create(model="synthetic-model", messages=MESSAGES, stream=True)
        try:
            assert body.pulls == 0
            assert not telemetry[1].get_finished_spans()
            if action.startswith("context"):
                error = ConsumerError("synthetic consumer failure")
                try:
                    with stream as opened:
                        assert opened is stream
                        if action != "context_empty":
                            assert next(iter(opened)).choices[0].delta.content == OUTPUT
                        if action == "context_error":
                            raise error
                except ConsumerError as caught:
                    assert caught is error
            elif action == "exhaust":
                assert [item.choices[0].delta.content for item in stream] == [OUTPUT]
            else:
                if action == "close_one":
                    assert next(iter(stream)).choices[0].delta.content == OUTPUT
                stream.close()
            assert response.is_closed
            assert body.pulls == (2 if action == "exhaust" else 0 if action.endswith("empty") else 1)
            assert body.closes == 1
            assert_spans(telemetry)
            stream.close()
            assert_spans(telemetry)
            assert body.closes == 1
        finally:
            stream.close()
            response.close()


@pytest.mark.parametrize("action", ACTIONS)
def test_async_stream_lifecycle(telemetry, action):
    async def run():
        body = AsyncBody()
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
        async with groq.AsyncGroq(
            api_key=SYNTHETIC_KEY, max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)),
        ) as client:
            stream = await client.chat.completions.create(model="synthetic-model", messages=MESSAGES, stream=True)
            try:
                assert body.pulls == 0
                assert not telemetry[1].get_finished_spans()
                if action.startswith("context"):
                    error = ConsumerError("synthetic consumer failure")
                    try:
                        async with stream as opened:
                            assert opened is stream
                            if action != "context_empty":
                                assert (await opened.__aiter__().__anext__()).choices[0].delta.content == OUTPUT
                            if action == "context_error":
                                raise error
                    except ConsumerError as caught:
                        assert caught is error
                elif action == "exhaust":
                    assert [item.choices[0].delta.content async for item in stream] == [OUTPUT]
                else:
                    if action == "close_one":
                        assert (await stream.__aiter__().__anext__()).choices[0].delta.content == OUTPUT
                    await stream.close()
                assert response.is_closed
                assert body.pulls == (2 if action == "exhaust" else 0 if action.endswith("empty") else 1)
                assert body.closes == 1
                assert_spans(telemetry)
                # The old wrapper exposed aclose(); retain that compatibility too.
                if telemetry[0] != "uninstrumented":
                    await stream.aclose()
                else:
                    await stream.close()
                assert_spans(telemetry)
                assert body.closes == 1
            finally:
                if hasattr(stream, "close"):
                    await stream.close()
                else:
                    await stream.aclose()
                await response.aclose()
    asyncio.run(run())
