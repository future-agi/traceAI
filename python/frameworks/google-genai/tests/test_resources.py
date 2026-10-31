"""Offline public-SDK parity tests; no vendor credentials or network required."""

import asyncio
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import httpx
import pytest
from fi_instrumentation import FITracer, TraceConfig, suppress_tracing
from google import genai
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_google_genai import GoogleGenAIInstrumentor


@contextmanager
def tracing(config=None, exporter=None):
    provider = TracerProvider()
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    if exporter:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = GoogleGenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
    try:
        yield memory, provider
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def client(handler):
    transport = httpx.MockTransport(handler)
    from google.genai.types import HttpOptions

    if "client_args" not in HttpOptions.model_fields:
        c = genai.Client(api_key="synthetic-not-a-key")
        c._api_client._httpx_client.close()
        asyncio.run(c._api_client._async_httpx_client.aclose())
        c._api_client._httpx_client = httpx.Client(transport=transport)
        c._api_client._async_httpx_client = httpx.AsyncClient(transport=transport)
        return c
    return genai.Client(
        api_key="synthetic-not-a-key",
        http_options={
            "client_args": {"transport": transport},
            "async_client_args": {"transport": transport},
        },
    )


def close_client(c):
    if hasattr(c, "close"):
        c.close()
    else:
        c._api_client._httpx_client.close()


async def close_async_client(c):
    if hasattr(c.aio, "aclose"):
        await c.aio.aclose()
    else:
        await c._api_client._async_httpx_client.aclose()


def response(request):
    path = request.url.path
    if "batchEmbedContents" in path:
        return httpx.Response(
            200, json={"embeddings": [{"values": [0.1, 0.2]}, {"values": [0.3, 0.4]}]}
        )
    if "embedContent" in path:
        return httpx.Response(200, json={"embedding": {"values": [0.1, 0.2]}})
    if "cachedContents" in path:
        return httpx.Response(
            200,
            json={
                "name": "cachedContents/offline",
                "model": "models/gemini-test",
                "usageMetadata": {"totalTokenCount": 12},
            },
        )
    if "interactions" in path:
        return httpx.Response(200, json=interaction())
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "offline reply"}]}}
            ]
        },
    )


def interaction():
    return {
        "id": "offline-id",
        "status": "completed",
        "model": "gemini-test",
        "outputs": [{"type": "text", "text": "private-output"}],
        "steps": [
            {"type": "model", "content": [{"type": "text", "text": "private-output"}]}
        ],
        "usage": {
            "total_input_tokens": 5,
            "total_output_tokens": 2,
            "total_tokens": 8,
            "total_thought_tokens": 1,
            "total_cached_tokens": 0,
        },
    }


def has_interactions(c):
    if not hasattr(c, "interactions"):
        pytest.skip("Interactions is absent at the declared SDK floor")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_embeddings_and_generation_are_distinct(asynchronous):
    with tracing() as (memory, provider):
        c = client(response)
        if asynchronous:

            async def run():
                with provider.get_tracer("parent").start_as_current_span("parent"):
                    value = await c.aio.models.embed_content(
                        model="gemini-embedding-001", contents=["one", "two"]
                    )
                    await c.aio.models.generate_content(
                        model="gemini-test", contents="hello"
                    )
                await close_async_client(c)
                return value

            value = asyncio.run(run())
        else:
            with provider.get_tracer("parent").start_as_current_span("parent"):
                value = c.models.embed_content(
                    model="gemini-embedding-001", contents=["one", "two"]
                )
                c.models.generate_content(model="gemini-test", contents="hello")
        close_client(c)
        assert len(value.embeddings) == 2
        spans = memory.get_finished_spans()
        assert len(spans) == 3
        embedding, generation, parent = spans
        assert embedding.name == "models.embed_content"
        assert embedding.attributes["gen_ai.span.kind"] == "EMBEDDING"
        assert embedding.attributes["embedding.model_name"] == "gemini-embedding-001"
        assert embedding.attributes["embedding.embeddings.1.embedding.vector"] == (
            0.3,
            0.4,
        )
        assert embedding.attributes["gen_ai.embeddings.dimension.count"] == 2
        assert (
            embedding.parent.span_id
            == generation.parent.span_id
            == parent.context.span_id
        )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["create", "get", "update", "delete"])
def test_caches(asynchronous, operation):
    with tracing() as (memory, _):
        c = client(response)
        kwargs = (
            {"model": "gemini-test", "config": {"contents": "private-input"}}
            if operation == "create"
            else {"name": "cachedContents/offline"}
        )
        if operation == "update":
            kwargs["config"] = {"ttl": "3600s"}
        if asynchronous:

            async def run():
                result = await getattr(c.aio.caches, operation)(**kwargs)
                await close_async_client(c)
                return result

            asyncio.run(run())
        else:
            getattr(c.caches, operation)(**kwargs)
        close_client(c)
        spans = memory.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == f"caches.{operation}"
        assert spans[0].attributes["gen_ai.span.kind"] == "CHAIN"
        assert "gen_ai.usage.input_tokens" not in spans[0].attributes
        if operation != "delete":
            assert spans[0].attributes["google.genai.cache.token_count"] == 12


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["create", "get"])
def test_interactions(asynchronous, operation):
    c = client(response)
    has_interactions(c)
    with tracing() as (memory, _):
        kwargs = (
            {"model": "gemini-test", "input": "private-input"}
            if operation == "create"
            else {"id": "offline-id"}
        )
        if asynchronous:

            async def run():
                result = await getattr(c.aio.interactions, operation)(**kwargs)
                await close_async_client(c)
                return result

            result = asyncio.run(run())
        else:
            result = getattr(c.interactions, operation)(**kwargs)
        close_client(c)
        assert result.id == "offline-id"
        spans = memory.get_finished_spans()
        assert len(spans) == 1
        attrs = spans[0].attributes
        assert spans[0].name == f"interactions.{operation}"
        assert attrs["gen_ai.usage.input_tokens"] == 5
        assert attrs["gen_ai.usage.output_tokens"] == 2
        assert attrs["gen_ai.usage.total_tokens"] == 8
        assert attrs["gen_ai.usage.output_tokens.reasoning"] == 1
        assert attrs["gen_ai.usage.cache_read_tokens"] == 0
        assert "private-output" in attrs["output.value"]


def stream_events(c):
    new = "_gaos" in type(c.interactions).__module__
    return [
        {
            "event_type": "interaction.created",
            "interaction": {"id": "offline-id", "status": "in_progress"},
        },
        {
            "event_type": "step.delta" if new else "content.delta",
            "index": 0,
            "delta": {"type": "text", "text": "private-output"},
        },
        {"event_type": "interaction.completed", "interaction": interaction()},
    ]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("operation", ["create", "get"])
@pytest.mark.parametrize("early_close", [False, True])
def test_real_sdk_streams(asynchronous, operation, early_close):
    events = []

    def respond(request):
        data = "".join(
            f"event: {event['event_type']}\ndata: {json.dumps(event)}\n\n"
            for event in events
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=data
        )

    c = client(respond)
    has_interactions(c)
    events.extend(stream_events(c))
    kwargs = (
        {"model": "gemini-test", "input": "private-input", "stream": True}
        if operation == "create"
        else {"id": "offline-id", "stream": True}
    )
    with tracing() as (memory, provider):
        if asynchronous:

            async def run():
                with provider.get_tracer("parent").start_as_current_span(
                    "parent"
                ) as parent:
                    stream = await getattr(c.aio.interactions, operation)(**kwargs)
                    assert not memory.get_finished_spans()
                    async with stream:
                        first = await stream.__anext__()
                        assert first.event_type == "interaction.created"
                        assert trace.get_current_span() is parent
                        if not early_close:
                            remaining = [event async for event in stream]
                            assert remaining[-1].event_type == "interaction.completed"
                    await stream.close()
                await close_async_client(c)

            asyncio.run(run())
        else:
            with provider.get_tracer("parent").start_as_current_span(
                "parent"
            ) as parent:
                stream = getattr(c.interactions, operation)(**kwargs)
                assert not memory.get_finished_spans()
                with stream:
                    first = next(stream)
                    assert first.event_type == "interaction.created"
                    assert trace.get_current_span() is parent
                    if not early_close:
                        assert list(stream)[-1].event_type == "interaction.completed"
                stream.close()
        close_client(c)
        spans = memory.get_finished_spans()
        assert len(spans) == 2
        call, parent = spans
        assert call.parent.span_id == parent.context.span_id
        if early_close:
            assert call.status.status_code == trace.StatusCode.UNSET
        else:
            assert call.status.status_code == trace.StatusCode.OK
            assert call.attributes["gen_ai.usage.input_tokens"] == 5
            assert "private-output" in call.attributes["output.value"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_suppression_and_uninstrument(asynchronous):
    c = client(response)
    original = type(c.models).embed_content
    with tracing() as (memory, _):

        async def run():
            await c.aio.models.embed_content(
                model="gemini-embedding-001", contents="private-input"
            )

        with suppress_tracing():
            if asynchronous:
                asyncio.run(run())
            else:
                c.models.embed_content(
                    model="gemini-embedding-001", contents="private-input"
                )
        assert memory.get_finished_spans() == ()
    assert type(c.models).embed_content is original
    c.models.embed_content(model="gemini-embedding-001", contents="private-input")
    assert memory.get_finished_spans() == ()
    close_client(c)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_sdk_error_propagates_once(asynchronous):
    def fail(request):
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "private-input",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )

    c = client(fail)
    with tracing(TraceConfig(hide_inputs=True, hide_outputs=True)) as (memory, _):

        async def run():
            await c.aio.models.embed_content(
                model="gemini-embedding-001", contents="private-input"
            )

        with pytest.raises(Exception, match="private-input"):
            if asynchronous:
                asyncio.run(run())
            else:
                c.models.embed_content(
                    model="gemini-embedding-001", contents="private-input"
                )
        spans = memory.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == trace.StatusCode.ERROR
        assert "private-input" not in str(spans[0].attributes)
        assert "private-input" not in str(spans[0].events)
    close_client(c)


@contextmanager
def collector():
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", bodies
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("hidden", [False, True])
def test_otlp_wire_privacy(hidden):
    config = TraceConfig(
        hide_inputs=hidden, hide_outputs=hidden, hide_embedding_vectors=hidden
    )
    with collector() as (endpoint, bodies):
        with tracing(config, OTLPSpanExporter(endpoint=endpoint, headers={})) as (
            _,
            provider,
        ):
            c = client(response)
            c.models.embed_content(
                model="gemini-embedding-001", contents="private-input"
            )
            c.caches.create(
                model="gemini-test",
                config={
                    "contents": "private-input",
                    "http_options": {"headers": {"x-private": "transport-secret"}},
                },
            )
            if hasattr(c, "interactions"):
                c.interactions.create(
                    model="gemini-test",
                    input="private-input",
                    extra_headers={"x-private": "transport-secret"},
                )
            provider.force_flush()
            close_client(c)
        decoded = [ExportTraceServiceRequest.FromString(body) for body in bodies]
        wire = str(decoded)
        assert "transport-secret" not in wire
        if hidden:
            assert "private-input" not in wire
            assert "private-output" not in wire
            assert "embedding.vector" not in wire
        else:
            assert "private-input" in wire
            assert "embedding.vector" in wire
        assert "gen_ai.span.kind" in wire
        assert "gen_ai.embeddings.dimension.count" in wire


def test_typed_request_and_transport_exclusion():
    c = client(response)
    has_interactions(c)
    if "_gaos" not in type(c.interactions).__module__:
        pytest.skip("Typed requests were added with NextGen resources")
    with tracing() as (memory, _):
        c.interactions.create(
            request={"body": {"model": "gemini-test", "input": "private-input"}},
            extra_headers={"secret": "transport-secret"},
        )
        attrs = memory.get_finished_spans()[0].attributes
        assert attrs["gen_ai.request.model"] == "gemini-test"
        assert "private-input" in attrs["input.value"]
        assert "transport-secret" not in str(attrs)
    close_client(c)


def test_continuation_usage_and_bounded_capture():
    from traceai_google_genai._resource_attributes import ResponseAttributes

    capture = ResponseAttributes("interactions", TraceConfig())
    for status, tokens in [("requires_continuation", 4), ("completed", 6)]:
        capture.event(
            {
                "event_type": "interaction.completed",
                "interaction": {
                    "id": status,
                    "status": status,
                    "usage": {"total_input_tokens": tokens, "total_output_tokens": 2},
                },
            }
        )
    for _ in range(100):
        capture.event(
            {
                "event_type": "step.delta",
                "index": 0,
                "delta": {"type": "text", "text": "x" * 10_000},
            }
        )
    attrs = capture.finish()
    assert attrs["gen_ai.usage.input_tokens"] == 10
    assert attrs["gen_ai.usage.output_tokens"] == 4
    assert attrs["google.genai.content_truncated"]
    assert len(attrs["output.value"]) <= 65_536
    assert not capture.failed


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", ["iterate", "close", "context", "cancel"])
def test_stream_failure_identity_and_lifecycle(asynchronous, failure):
    from traceai_google_genai._resource_wrappers import _wrapper

    event = {
        "event_type": "step.delta",
        "index": 0,
        "delta": {"type": "text", "text": "text"},
    }
    error = (
        asyncio.CancelledError() if failure == "cancel" else ValueError("exact error")
    )

    class Stream:
        def __iter__(self):
            return self

        def __next__(self):
            if failure in ("iterate", "cancel"):
                raise error
            return event

        def close(self):
            if failure == "close":
                raise error

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    class AsyncStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if failure in ("iterate", "cancel"):
                raise error
            return event

        async def close(self):
            if failure == "close":
                raise error

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.close()

    with tracing() as (memory, provider):
        tracer = FITracer(provider.get_tracer("test"), config=TraceConfig())
        wrapper = _wrapper(
            tracer, TraceConfig(), "interactions", "create", asynchronous
        )
        if asynchronous:

            async def run():
                async def invoke():
                    return AsyncStream()

                stream = await wrapper(invoke, None, (), {})
                if failure == "context":
                    async with stream:
                        assert await stream.__anext__() is event
                        raise error
                elif failure == "close":
                    assert await stream.__anext__() is event
                    await stream.close()
                else:
                    await stream.__anext__()

            with pytest.raises(type(error)) as exc:
                asyncio.run(run())
        else:
            stream = wrapper(Stream, None, (), {})
            with pytest.raises(type(error)) as exc:
                if failure == "context":
                    with stream:
                        assert next(stream) is event
                        raise error
                elif failure == "close":
                    assert next(stream) is event
                    stream.close()
                else:
                    next(stream)
        assert exc.value is error
        spans = memory.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == trace.StatusCode.ERROR
        assert not trace.get_current_span().is_recording()


@pytest.mark.parametrize(
    "flag",
    ["hide_inputs", "hide_input_text", "hide_input_messages", "hide_input_images"],
)
def test_input_privacy_flags(flag):
    with tracing(TraceConfig(**{flag: True})) as (memory, _):
        c = client(response)
        c.models.embed_content(model="gemini-embedding-001", contents="private-input")
        c.caches.create(model="gemini-test", config={"contents": "private-input"})
        if hasattr(c, "interactions"):
            c.interactions.create(
                model="gemini-test",
                input="private-input",
                system_instruction="private-input",
            )
        assert "private-input" not in str(
            [s.attributes for s in memory.get_finished_spans()]
        )
        close_client(c)


@pytest.mark.parametrize(
    "flag", ["hide_outputs", "hide_output_text", "hide_output_messages"]
)
def test_output_privacy_applies_to_stream_buffers(flag):
    from traceai_google_genai._resource_attributes import ResponseAttributes

    capture = ResponseAttributes("interactions", TraceConfig(**{flag: True}))
    capture.response(interaction())
    assert "private-output" not in str(capture.finish())
    capture = ResponseAttributes("interactions", TraceConfig(**{flag: True}))
    capture.event(
        {"event_type": "step.delta", "index": 0, "delta": {"text": "private-output"}}
    )
    capture.event({"event_type": "interaction.completed", "interaction": interaction()})
    assert "private-output" not in str(capture.finish())
    assert capture.events == []
    assert capture.attributes["gen_ai.usage.total_tokens"] == 8


@pytest.mark.parametrize("asynchronous", [False, True])
def test_nested_resource_wrappers_do_not_duplicate_spans(asynchronous):
    from traceai_google_genai._resource_wrappers import _wrapper

    with tracing() as (memory, provider):
        tracer = FITracer(provider.get_tracer("nested"), config=TraceConfig())
        wrapper = _wrapper(
            tracer, TraceConfig(), "interactions", "create", asynchronous
        )
        if asynchronous:

            async def inner():
                return interaction()

            async def outer():
                return await wrapper(inner, None, (), {})

            asyncio.run(wrapper(outer, None, (), {}))
        else:

            def outer():
                return wrapper(interaction, None, (), {})

            wrapper(outer, None, (), {})
        assert len(memory.get_finished_spans()) == 1


def test_error_events_and_failed_status_are_errors():
    from traceai_google_genai._resource_wrappers import _wrapper

    with tracing(TraceConfig(hide_inputs=True, hide_outputs=True)) as (
        memory,
        provider,
    ):
        tracer = FITracer(
            provider.get_tracer("errors"),
            config=TraceConfig(hide_inputs=True, hide_outputs=True),
        )
        wrapper = _wrapper(
            tracer,
            TraceConfig(hide_inputs=True, hide_outputs=True),
            "interactions",
            "create",
            False,
        )
        failed = {"event_type": "error", "error": {"message": "private-input"}}
        assert list(wrapper(lambda: iter([failed]), None, (), {})) == [failed]
        result = wrapper(
            lambda: {"status": "failed", "errors": [{"message": "private-input"}]},
            None,
            (),
            {},
        )
        assert result["status"] == "failed"
        spans = memory.get_finished_spans()
        assert len(spans) == 2
        assert all(s.status.status_code == trace.StatusCode.ERROR for s in spans)
        assert "private-input" not in str([s.attributes for s in spans])


def test_attribute_failure_does_not_break_sdk(monkeypatch, caplog):
    import traceai_google_genai._resource_wrappers as wrappers

    def fail(*args, **kwargs):
        raise ValueError("serialization failure")

    monkeypatch.setattr(wrappers, "request_attributes", fail)
    monkeypatch.setattr(wrappers.ResponseAttributes, "response", fail)
    with tracing() as (memory, _):
        c = client(response)
        result = c.models.embed_content(
            model="gemini-embedding-001", contents="private-input"
        )
        assert result.embeddings[0].values == [0.1, 0.2]
        assert len(memory.get_finished_spans()) == 1
        assert "telemetry extraction failed (ValueError)" in caplog.text
        assert "serialization failure" not in caplog.text
        close_client(c)
