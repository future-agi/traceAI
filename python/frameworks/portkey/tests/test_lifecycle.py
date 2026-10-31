"""Real SDK resource methods with synthetic transports; no vendor API calls."""

import asyncio
import inspect
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from portkey_ai.api_resources.apis import chat_complete, generation

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from traceai_portkey import PortkeyInstrumentor


MODULES = {"chat": chat_complete, "prompt": generation}
CHUNKS = [
    {
        "model": "synthetic",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hello "}}],
    },
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "content": "world",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {"name": "weather", "arguments": '{"city":'},
                        }
                    ],
                },
            }
        ]
    },
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]
                },
            }
        ]
    },
    {
        "choices": [],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    },
]
RESPONSE = {
    "model": "synthetic",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hello world"}}
    ],
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("FI_", "OTEL_", "PORTKEY_", "OPENAI_")):
            monkeypatch.delenv(key)


@pytest.fixture
def tracing():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = PortkeyInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    yield provider, exporter, instrumentor
    instrumentor.uninstrument()
    provider.shutdown()


def resource(kind, response=None, error=None, asynchronous=False):
    module = MODULES[kind]
    name = "AsyncCompletions" if asynchronous else "Completions"
    if not hasattr(module, name):
        pytest.skip("This SDK predates async resources")
    cls = getattr(module, name)
    obj = cls.__new__(cls)

    def transport(*args, **kwargs):
        assert context.get_value(context._SUPPRESS_INSTRUMENTATION_KEY)
        assert trace.get_current_span().get_span_context().is_valid
        if error:
            raise error
        return response

    async def async_transport(*args, **kwargs):
        return transport(*args, **kwargs)

    seam = async_transport if asynchronous else transport
    obj._post = seam
    obj.normal_create = seam
    obj.stream_create = (
        transport
        if inspect.isasyncgenfunction(getattr(cls, "stream_create", None))
        else seam
    )
    params = {
        "model": "synthetic",
        "messages": [{"role": "user", "content": "input-canary"}],
    }
    if kind == "prompt":
        params = {
            "prompt_id": "synthetic",
            "variables": {"question": "input-canary"},
            "config": {},
        }
    return obj, params


class FakeStream:
    def __init__(self, error=None):
        self.chunks = iter(CHUNKS)
        self.error = error
        self.closed = False
        self.headers = {"request-id": "synthetic"}

    def __iter__(self):
        return self

    def __next__(self):
        assert context.get_value(context._SUPPRESS_INSTRUMENTATION_KEY)
        assert trace.get_current_span().get_span_context().is_valid
        try:
            return next(self.chunks)
        except StopIteration:
            if self.error:
                raise self.error
            raise

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeAsyncStream(FakeStream):
    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return super().__next__()
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


def assert_finished(exporter, status="OK", output=True):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code.name == status
    assert span.attributes[SpanAttributes.GEN_AI_PROVIDER_NAME] == "portkey"
    if output:
        assert "hello world" in span.attributes[SpanAttributes.OUTPUT_VALUE]
    return span


def assert_stream_shape(span):
    attrs = span.attributes
    assert attrs[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 5
    assert attrs[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 2
    assert attrs[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 3
    data = json.loads(attrs[SpanAttributes.OUTPUT_VALUE])
    tool = data["choices"][0]["message"]["tool_calls"][0]
    assert tool["id"] == "call-1"
    assert tool["function"] == {"name": "weather", "arguments": '{"city":"Paris"}'}
    assert any(v == '{"city":"Paris"}' for k, v in attrs.items() if "arguments" in k)


@pytest.mark.parametrize("kind", MODULES)
def test_sync_nonstream(tracing, kind):
    _, exporter, _ = tracing
    obj, params = resource(kind, RESPONSE)
    assert obj.create(**params) is RESPONSE
    assert_finished(exporter)


@pytest.mark.parametrize("kind", MODULES)
@pytest.mark.asyncio
async def test_async_nonstream(tracing, kind):
    _, exporter, _ = tracing
    obj, params = resource(kind, RESPONSE, asynchronous=True)
    assert await obj.create(**params) is RESPONSE
    assert_finished(exporter)


@pytest.mark.parametrize("kind", MODULES)
@pytest.mark.parametrize("failure", [False, True])
def test_sync_stream_lifecycle(tracing, kind, failure):
    provider, exporter, _ = tracing
    original = FakeStream(RuntimeError("stream-canary") if failure else None)
    obj, params = resource(kind, original)
    with provider.get_tracer("parent").start_as_current_span("parent") as parent:
        stream = obj.create(**params, stream=True)
        assert stream.headers == original.headers
        assert not exporter.get_finished_spans()
        assert trace.get_current_span() is parent
        for chunk in CHUNKS:
            assert next(stream) is chunk
            assert trace.get_current_span() is parent
            assert not context.get_value(context._SUPPRESS_INSTRUMENTATION_KEY)
            assert not exporter.get_finished_spans()
        with pytest.raises(RuntimeError if failure else StopIteration):
            next(stream)
        span = assert_finished(exporter, "ERROR" if failure else "OK")
        assert span.parent.span_id == parent.get_span_context().span_id
        assert_stream_shape(span)
        stream.close()
        assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("kind", MODULES)
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.asyncio
async def test_async_stream_lifecycle(tracing, kind, failure):
    _, exporter, _ = tracing
    original = FakeAsyncStream(RuntimeError("stream-canary") if failure else None)
    obj, params = resource(kind, original, asynchronous=True)
    stream = await obj.create(**params, stream=True)
    assert not exporter.get_finished_spans()
    for chunk in CHUNKS:
        assert await stream.__anext__() is chunk
        assert not context.get_value(context._SUPPRESS_INSTRUMENTATION_KEY)
        assert not trace.get_current_span().get_span_context().is_valid
        assert not exporter.get_finished_spans()
    with pytest.raises(RuntimeError if failure else StopAsyncIteration):
        await stream.__anext__()
    assert_stream_shape(assert_finished(exporter, "ERROR" if failure else "OK"))
    await stream.close()
    assert original.closed
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("kind", MODULES)
@pytest.mark.parametrize("consume", [False, True])
def test_close_and_context_manager(tracing, kind, consume):
    _, exporter, _ = tracing
    original = FakeStream()
    obj, params = resource(kind, original)
    with obj.create(**params, stream=True) as stream:
        if consume:
            next(stream)
    assert original.closed
    assert_finished(exporter, "UNSET", output=False)
    stream.close()
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("kind", MODULES)
@pytest.mark.asyncio
async def test_async_context_manager(tracing, kind):
    _, exporter, _ = tracing
    original = FakeAsyncStream()
    obj, params = resource(kind, original, asynchronous=True)
    async with await obj.create(**params, stream=True) as stream:
        await stream.__anext__()
    assert original.closed
    assert_finished(exporter, "UNSET", output=False)


@pytest.mark.parametrize("kind", MODULES)
@pytest.mark.asyncio
async def test_async_cancel_during_iteration(tracing, kind):
    _, exporter, _ = tracing
    started = asyncio.Event()

    async def chunks():
        yield CHUNKS[0]
        started.set()
        await asyncio.Event().wait()

    obj, params = resource(kind, chunks(), asynchronous=True)
    stream = await obj.create(**params, stream=True)
    await stream.__anext__()
    task = asyncio.create_task(stream.__anext__())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_finished(exporter, "ERROR", output=False)
    await stream.aclose()
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize("kind", MODULES)
@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.asyncio
async def test_async_creation_failure(tracing, kind, error_type):
    _, exporter, _ = tracing
    error = error_type("create-canary")
    obj, params = resource(kind, error=error, asynchronous=True)
    with pytest.raises(error_type) as caught:
        await obj.create(**params)
    assert caught.value is error
    assert_finished(exporter, "ERROR", output=False)


@pytest.mark.parametrize("kind", MODULES)
def test_creation_failure(tracing, kind):
    _, exporter, _ = tracing
    error = RuntimeError("create-canary")
    obj, params = resource(kind, error=error)
    with pytest.raises(RuntimeError) as caught:
        obj.create(**params)
    assert caught.value is error
    assert_finished(exporter, "ERROR", output=False)


def test_restore_all_methods_and_reinstrument():
    originals = {}
    for module in MODULES.values():
        for name in ("Completions", "AsyncCompletions"):
            if cls := getattr(module, name, None):
                originals[cls] = cls.create
    instrumentor = PortkeyInstrumentor()
    provider = TracerProvider()
    try:
        for _ in range(2):
            instrumentor.instrument(tracer_provider=provider)
            assert all(cls.create is not method for cls, method in originals.items())
            instrumentor.uninstrument()
            assert all(cls.create is method for cls, method in originals.items())
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_suppressed_invocation(tracing, monkeypatch):
    _, exporter, _ = tracing
    obj, params = resource("chat", RESPONSE)
    # No instrumentation should make a span or change the returned object.
    for seam in ("_post", "normal_create", "stream_create"):
        monkeypatch.setattr(obj, seam, lambda *a, **k: RESPONSE)
    token = context.attach(
        context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        assert obj.create(**params) is RESPONSE
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()


@pytest.mark.parametrize("kind", MODULES)
def test_transport_credentials_not_captured(tracing, kind):
    _, exporter, _ = tracing
    obj, params = resource(kind, RESPONSE)
    params.update(
        extra_headers={"Authorization": "credential-canary"},
        api_key="credential-canary",
    )
    if kind == "prompt":
        params["config"] = {"api_key": "credential-canary"}
    obj.create(**params)
    assert "credential-canary" not in str(exporter.get_finished_spans()[0].attributes)


@pytest.mark.parametrize("hidden", [False, True])
def test_otlp_wire_stream_privacy_and_status(hidden):
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                headers={},
                timeout=2,
            )
        )
    )
    instrumentor = PortkeyInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(
            hide_inputs=hidden,
            hide_outputs=hidden,
            hide_llm_invocation_parameters=hidden,
        ),
    )
    try:
        obj, params = resource(
            "prompt", FakeStream(RuntimeError("private-error-canary"))
        )
        stream = obj.create(
            **params, stream=True, extra_headers={"Authorization": "credential-canary"}
        )
        assert bodies == []
        with pytest.raises(RuntimeError):
            list(stream)
        assert len(bodies) == 1
        request = ExportTraceServiceRequest.FromString(bodies[0])
        spans = [
            span
            for rs in request.resource_spans
            for ss in rs.scope_spans
            for span in ss.spans
        ]
        assert len(spans) == 1
        span = spans[0]
        assert span.status.code == 2  # ERROR on the wire, not just in the SDK.
        assert span.end_time_unix_nano >= span.start_time_unix_nano
        attributes = {attribute.key: attribute.value for attribute in span.attributes}
        assert attributes[SpanAttributes.GEN_AI_PROVIDER_NAME].string_value == "portkey"
        assert attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS].int_value == 5
        wire = str(request)
        assert "credential-canary" not in wire
        assert ("input-canary" in wire) is not hidden
        assert ("hello world" in wire) is not hidden
        assert ("private-error-canary" in wire) is not hidden
        assert ("Paris" in wire) is not hidden
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("text_prompt", [False, True])
def test_real_sdk_sse_stream(tracing, text_prompt):
    import httpx
    from portkey_ai.api_resources.streaming import Stream

    chunk_type = getattr(
        generation, "PromptCompletionChunk", generation.GenericResponse
    )

    _, exporter, _ = tracing
    chunks = CHUNKS
    if text_prompt:
        chunks = [
            {"choices": [{"index": 0, "text": word}]} for word in ("hello ", "world")
        ]
    data = (
        "".join(
            "data: "
            + json.dumps(dict(success=True, data=None, warning=None, **chunk))
            + "\n\n"
            for chunk in chunks
        )
        + "data: [DONE]\n\n"
    )
    response = httpx.Response(
        200, content=data, request=httpx.Request("POST", "http://localhost/synthetic")
    )
    native = Stream(response=response, cast_to=chunk_type)
    obj, params = resource("prompt", native)
    stream = obj.create(**params, stream=True)
    assert stream.response is response
    assert not exporter.get_finished_spans()
    assert len(list(stream)) == len(chunks)
    assert_finished(exporter)
    stream.close()
    assert response.is_closed


@pytest.mark.asyncio
async def test_real_sdk_async_sse_stream(tracing):
    import httpx
    from portkey_ai.api_resources import streaming

    chunk_type = getattr(
        generation, "PromptCompletionChunk", generation.GenericResponse
    )

    if not hasattr(streaming, "AsyncStream"):
        pytest.skip("This SDK predates async streams")
    _, exporter, _ = tracing
    data = (
        "".join(
            "data: "
            + json.dumps(dict(success=True, data=None, warning=None, **chunk))
            + "\n\n"
            for chunk in CHUNKS
        )
        + "data: [DONE]\n\n"
    )
    response = httpx.Response(
        200, content=data, request=httpx.Request("POST", "http://localhost/synthetic")
    )
    native = streaming.AsyncStream(response=response, cast_to=chunk_type)
    obj, params = resource("prompt", native, asynchronous=True)
    stream = await obj.create(**params, stream=True)
    assert not exporter.get_finished_spans()
    assert len([chunk async for chunk in stream]) == len(CHUNKS)
    assert_finished(exporter)
    await stream.close()
    assert response.is_closed


def test_generator_close_with_context_error(tracing):
    _, exporter, _ = tracing
    closed = []

    def chunks():
        try:
            yield CHUNKS[0]
        finally:
            closed.append(True)

    obj, params = resource("chat", chunks())
    with pytest.raises(ValueError):
        with obj.create(**params, stream=True) as stream:
            next(stream)
            raise ValueError("consumer-error")
    assert closed == [True]
    assert_finished(exporter, "ERROR", output=False)


@pytest.mark.asyncio
async def test_async_generator_close_with_context_error(tracing):
    _, exporter, _ = tracing
    closed = []

    async def chunks():
        try:
            yield CHUNKS[0]
        finally:
            closed.append(True)

    obj, params = resource("chat", chunks(), asynchronous=True)
    with pytest.raises(ValueError):
        async with await obj.create(**params, stream=True) as stream:
            await stream.__anext__()
            raise ValueError("consumer-error")
    assert closed == [True]
    assert_finished(exporter, "ERROR", output=False)


def test_close_failure_ends_span(tracing):
    _, exporter, _ = tracing

    class BrokenClose(FakeStream):
        def close(self):
            raise RuntimeError("close-error")

    obj, params = resource("chat", BrokenClose())
    stream = obj.create(**params, stream=True)
    with pytest.raises(RuntimeError):
        stream.close()
    assert_finished(exporter, "ERROR", output=False)


@pytest.mark.asyncio
async def test_async_close_failure_ends_span(tracing):
    _, exporter, _ = tracing

    class BrokenClose(FakeAsyncStream):
        async def close(self):
            raise RuntimeError("close-error")

    obj, params = resource("chat", BrokenClose(), asynchronous=True)
    stream = await obj.create(**params, stream=True)
    with pytest.raises(RuntimeError):
        await stream.close()
    assert_finished(exporter, "ERROR", output=False)


def test_instrument_failure_rolls_back(monkeypatch):
    import traceai_portkey

    original = chat_complete.Completions.create
    wrap = traceai_portkey.wrap_function_wrapper
    calls = []

    def fail_second(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("patch-error")
        return wrap(**kwargs)

    monkeypatch.setattr(traceai_portkey, "wrap_function_wrapper", fail_second)
    provider = TracerProvider()
    try:
        with pytest.raises(RuntimeError):
            PortkeyInstrumentor().instrument(tracer_provider=provider)
        assert chat_complete.Completions.create is original
    finally:
        provider.shutdown()


def test_generator_send_and_throw(tracing):
    _, exporter, _ = tracing
    obj, params = resource("chat", (chunk for chunk in CHUNKS))
    stream = obj.create(**params, stream=True)
    assert stream.send(None) is CHUNKS[0]
    assert not exporter.get_finished_spans()
    with pytest.raises(ValueError):
        stream.throw(ValueError("injected-error"))
    assert_finished(exporter, "ERROR", output=False)


@pytest.mark.asyncio
async def test_async_generator_send_and_throw(tracing):
    _, exporter, _ = tracing

    async def chunks():
        for chunk in CHUNKS:
            yield chunk

    obj, params = resource("chat", chunks(), asynchronous=True)
    stream = await obj.create(**params, stream=True)
    assert await stream.asend(None) is CHUNKS[0]
    assert not exporter.get_finished_spans()
    with pytest.raises(ValueError):
        await stream.athrow(ValueError("injected-error"))
    assert_finished(exporter, "ERROR", output=False)


def test_empty_stream(tracing):
    _, exporter, _ = tracing
    obj, params = resource("chat", iter(()))
    stream = obj.create(**params, stream=True)
    assert not exporter.get_finished_spans()
    assert list(stream) == []
    assert_finished(exporter, output=False)


def test_malformed_chunk_is_returned_and_logged(tracing, caplog):
    _, exporter, _ = tracing
    bad_chunk = {"choices": 5}
    obj, params = resource("chat", iter([bad_chunk]))
    stream = obj.create(**params, stream=True)
    assert list(stream) == [bad_chunk]
    assert "Failed to accumulate Portkey chunk" in caplog.text
    assert_finished(exporter, output=False)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_request_iterators_are_not_consumed(tracing, asynchronous):
    _, exporter, _ = tracing
    obj, params = resource("chat", RESPONSE, asynchronous=asynchronous)
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "weather"}}]
    params.update(messages=iter(messages), tools=iter(tools))

    def consume(*args, **kwargs):
        body = kwargs.get("body", kwargs)
        assert list(body["messages"]) == messages
        assert list(body["tools"]) == tools
        return RESPONSE

    async def async_consume(*args, **kwargs):
        return consume(*args, **kwargs)

    obj._post = obj.normal_create = async_consume if asynchronous else consume
    result = obj.create(**params)
    if asynchronous:
        result = await result
    assert result is RESPONSE
    assert_finished(exporter)
