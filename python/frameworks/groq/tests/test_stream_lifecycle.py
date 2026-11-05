"""Exercise the real Groq SDK against synthetic SSE, never a model endpoint."""

import asyncio
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import httpx
import pytest
from fi_instrumentation import TraceConfig
from groq import AsyncGroq, AsyncStream, Groq, Stream
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_groq import GroqInstrumentor


PROMPT = "private-prompt-marker"
OUTPUT = "private-output-marker"
ERROR = "private-error-marker"
PARAMS = {
    "model": "synthetic-model",
    "messages": [{"role": "user", "content": PROMPT}],
    "stream": True,
}


def sse(delta, *, usage=None):
    chunk = {
        "id": "synthetic-id",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "synthetic-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    if usage is not None:
        chunk["x_groq"] = {"usage": usage}
    return ("data: " + json.dumps(chunk) + "\n\n").encode()


EVENTS = [
    sse({"role": "assistant", "content": OUTPUT}),
    sse(
        {"content": "-tail"},
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    ),
    b"data: [DONE]\n\n",
]


class SyncBody(httpx.SyncByteStream):
    def __init__(self, events=EVENTS, error=None, close_error=None):
        self.events = events
        self.error = error
        self.close_error = close_error
        self.closes = 0

    def __iter__(self):
        yield from self.events
        if self.error is not None:
            raise self.error

    def close(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


class AsyncBody(httpx.AsyncByteStream):
    def __init__(self, events=EVENTS, error=None, close_error=None):
        self.events = events
        self.error = error
        self.close_error = close_error
        self.closes = 0

    async def __aiter__(self):
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


def sync_client(body):
    return Groq(
        api_key="offline-placeholder",
        max_retries=0,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={
                        "content-type": "text/event-stream",
                        "x-request-id": "synthetic-request",
                    },
                    stream=body,
                )
            )
        ),
    )


def async_client(body):
    return AsyncGroq(
        api_key="offline-placeholder",
        max_retries=0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={
                        "content-type": "text/event-stream",
                        "x-request-id": "synthetic-request",
                    },
                    stream=body,
                )
            )
        ),
    )


@contextmanager
def instrumented(config=None, exporter=None):
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = GroqInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
    try:
        yield memory, provider
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.fixture
def tracing():
    with instrumented() as pair:
        yield pair


def single_span(memory, error=False):
    spans = memory.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == (
        trace.StatusCode.ERROR if error else trace.StatusCode.OK
    )
    return span


@pytest.mark.parametrize("consume", [0, 1, 2])
def test_sync_context_preserves_sdk_interface_and_closes(tracing, consume):
    memory, _ = tracing
    body = SyncBody()
    with sync_client(body) as client:
        stream = client.chat.completions.create(**PARAMS)
        assert isinstance(stream, Stream)
        assert stream.response.headers["x-request-id"] == "synthetic-request"
        assert not memory.get_finished_spans()
        with stream as entered:
            assert entered is stream
            for _ in range(consume):
                next(entered)
        assert stream.response.is_closed
        stream.close()
    assert body.closes == 1
    span = single_span(memory)
    assert span.attributes["output.value"] == (
        OUTPUT + ("-tail" if consume == 2 else "") if consume else ""
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("consume", [0, 1, 2])
async def test_async_context_preserves_sdk_interface_and_closes(tracing, consume):
    memory, _ = tracing
    body = AsyncBody()
    async with async_client(body) as client:
        stream = await client.chat.completions.create(**PARAMS)
        assert isinstance(stream, AsyncStream)
        assert stream.response.headers["x-request-id"] == "synthetic-request"
        assert not memory.get_finished_spans()
        async with stream as entered:
            assert entered is stream
            for _ in range(consume):
                await entered.__anext__()
        assert stream.response.is_closed
        await stream.close()
        # Keep the former generator's explicit close spelling too.
        await stream.aclose()
    assert body.closes == 1
    span = single_span(memory)
    assert span.attributes["output.value"] == (
        OUTPUT + ("-tail" if consume == 2 else "") if consume else ""
    )


@pytest.mark.parametrize("consume", [False, True])
def test_sync_explicit_close_finishes_once(tracing, consume):
    memory, _ = tracing
    body = SyncBody()
    with sync_client(body) as client:
        stream = client.chat.completions.create(**PARAMS)
        if consume:
            next(stream)
        stream.close()
        stream.close()
    assert body.closes == 1
    span = single_span(memory)
    assert span.attributes["output.value"] == (OUTPUT if consume else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["close", "aclose"])
@pytest.mark.parametrize("consume", [False, True])
async def test_async_explicit_close_finishes_once(tracing, method, consume):
    memory, _ = tracing
    body = AsyncBody()
    async with async_client(body) as client:
        stream = await client.chat.completions.create(**PARAMS)
        if consume:
            await stream.__anext__()
        await getattr(stream, method)()
        await getattr(stream, method)()
    assert body.closes == 1
    span = single_span(memory)
    assert span.attributes["output.value"] == (OUTPUT if consume else "")


def test_sync_exhaustion_and_parent_context(tracing):
    memory, provider = tracing
    with sync_client(SyncBody()) as client:
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            stream = client.chat.completions.create(**PARAMS)
            assert trace.get_current_span() is parent
            assert [chunk.choices[0].delta.content for chunk in stream] == [
                OUTPUT,
                "-tail",
            ]
            assert trace.get_current_span() is parent
            span = single_span(memory)
            assert span.parent.span_id == parent.get_span_context().span_id
            assert span.attributes["gen_ai.usage.total_tokens"] == 5
            stream.close()
        assert len(memory.get_finished_spans()) == 2


@pytest.mark.asyncio
async def test_async_exhaustion(tracing):
    memory, _ = tracing
    async with async_client(AsyncBody()) as client:
        stream = await client.chat.completions.create(**PARAMS)
        assert [chunk.choices[0].delta.content async for chunk in stream] == [
            OUTPUT,
            "-tail",
        ]
        await stream.close()
    span = single_span(memory)
    assert span.attributes["gen_ai.usage.total_tokens"] == 5


@pytest.mark.parametrize("where", ["iteration", "close", "body"])
def test_sync_error_propagates_and_finishes_once(tracing, where):
    memory, _ = tracing
    error = RuntimeError(ERROR)
    body = SyncBody(
        events=EVENTS[:1],
        error=error if where == "iteration" else None,
        close_error=error if where == "close" else None,
    )
    with sync_client(body) as client:
        stream = client.chat.completions.create(**PARAMS)
        with pytest.raises(RuntimeError) as raised:
            with stream:
                if where == "body":
                    next(stream)
                    raise error
                list(stream)
        assert raised.value is error
    span = single_span(memory, error=True)
    assert span.attributes["output.value"] == OUTPUT
    assert len(span.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["iteration", "close", "body", "cancel"])
async def test_async_error_propagates_and_finishes_once(tracing, where):
    memory, _ = tracing
    error = asyncio.CancelledError() if where == "cancel" else RuntimeError(ERROR)
    body = AsyncBody(
        events=EVENTS[:1],
        error=error if where in ("iteration", "cancel") else None,
        close_error=error if where == "close" else None,
    )
    async with async_client(body) as client:
        stream = await client.chat.completions.create(**PARAMS)
        with pytest.raises(type(error)) as raised:
            async with stream:
                if where == "body":
                    await stream.__anext__()
                    raise error
                _ = [chunk async for chunk in stream]
        assert raised.value is error
    span = single_span(memory, error=True)
    assert span.attributes["output.value"] == OUTPUT
    assert len(span.events) == 1


@pytest.mark.asyncio
async def test_cancellation_before_response_finishes_span(tracing):
    memory, _ = tracing
    error = asyncio.CancelledError()

    async def handler(request):
        raise error

    async with AsyncGroq(
        api_key="offline-placeholder",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        with pytest.raises(asyncio.CancelledError) as raised:
            await client.chat.completions.create(**PARAMS)
        assert raised.value is error
    single_span(memory, error=True)


@pytest.mark.asyncio
async def test_interleaved_streams_do_not_mix_output(tracing):
    memory, _ = tracing
    async with (
        async_client(AsyncBody([sse({"content": "first"})])) as first_client,
        async_client(AsyncBody([sse({"content": "second"})])) as second_client,
    ):
        first = await first_client.chat.completions.create(**PARAMS)
        second = await second_client.chat.completions.create(**PARAMS)
        await first.__anext__()
        await second.__anext__()
        await second.close()
        await first.close()
    assert [
        span.attributes["output.value"] for span in memory.get_finished_spans()
    ] == ["second", "first"]


@pytest.fixture
def collector():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/v1/traces" % server.server_port, requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
async def test_partial_error_stream_otlp_wire_and_privacy(
    collector, hidden, asynchronous
):
    endpoint, requests = collector
    body_type = AsyncBody if asynchronous else SyncBody
    body = body_type(EVENTS[:1], error=httpx.ReadError(ERROR))
    config = TraceConfig(hide_inputs=hidden, hide_outputs=hidden)
    with instrumented(config, OTLPSpanExporter(endpoint=endpoint)):
        if asynchronous:
            async with async_client(body) as client:
                with pytest.raises(httpx.ReadError):
                    async with await client.chat.completions.create(**PARAMS) as stream:
                        _ = [chunk async for chunk in stream]
        else:
            with sync_client(body) as client:
                with pytest.raises(httpx.ReadError):
                    with client.chat.completions.create(**PARAMS) as stream:
                        list(stream)
    assert len(requests) == 1
    wire = ExportTraceServiceRequest.FromString(requests[0])
    spans = [
        span
        for resource in wire.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]
    assert len(spans) == 1
    assert spans[0].status.code == 2
    attributes = {entry.key: entry.value.string_value for entry in spans[0].attributes}
    assert attributes["gen_ai.response.model"] == "synthetic-model"
    for marker in (PROMPT, OUTPUT):
        assert (marker.encode() in requests[0]) is not hidden
    # Older core releases predate error-details redaction; content hiding must
    # still work there, while newer cores also scrub status and exception text.
    assert (ERROR.encode() in requests[0]) is not getattr(
        config, "hide_error_details", False
    )
    assert body.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_explicit_close_failure_before_iteration(tracing, asynchronous):
    memory, _ = tracing
    error = RuntimeError(ERROR)
    body = (AsyncBody if asynchronous else SyncBody)(close_error=error)
    if asynchronous:
        async with async_client(body) as client:
            stream = await client.chat.completions.create(**PARAMS)
            with pytest.raises(RuntimeError) as raised:
                await stream.close()
            await stream.close()
    else:
        with sync_client(body) as client:
            stream = client.chat.completions.create(**PARAMS)
            with pytest.raises(RuntimeError) as raised:
                stream.close()
            stream.close()
    assert raised.value is error
    single_span(memory, error=True)
    assert body.closes == 1


@pytest.mark.asyncio
async def test_task_cancellation_during_read_finishes_before_close(tracing):
    memory, _ = tracing
    reading = asyncio.Event()

    class WaitingBody(AsyncBody):
        async def __aiter__(self):
            yield EVENTS[0]
            reading.set()
            await asyncio.Event().wait()

    body = WaitingBody()
    async with async_client(body) as client:
        stream = await client.chat.completions.create(**PARAMS)
        await stream.__anext__()
        task = asyncio.create_task(stream.__anext__())
        await reading.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert single_span(memory, error=True).attributes["output.value"] == OUTPUT
        await stream.close()
    single_span(memory, error=True)
    assert body.closes == 1
