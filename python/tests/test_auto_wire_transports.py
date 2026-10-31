"""The wire layer on real HTTP clients against a local server: spans, streaming passthrough, dedup."""

import asyncio
import importlib
import importlib.util
import json

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from auto_mock_server import AUDIO_BYTES, CHAT_RESPONSE, MockServer, sse_body
from fi_instrumentation import FITracer, TraceConfig, suppress_tracing
from fi_instrumentation.auto import wire

httpx = pytest.importorskip("httpx")
requests = pytest.importorskip("requests")
# The current openai and anthropic SDKs ship on httpx2, a separate package with the httpx API.
HTTPX_MODULES = [httpx] + ([importlib.import_module("httpx2")] if importlib.util.find_spec("httpx2") else [])

CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]}
# conftest mocks the module-level requests.post/get; a Session goes through the real transport.
http = requests.Session()


@pytest.fixture
def server():
    with MockServer() as mock:
        yield mock


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("test"), config=TraceConfig())
    wire.enable(tracer)
    yield exporter, tracer
    wire.disable()


def _only(exporter):
    finished = exporter.get_finished_spans()
    assert len(finished) == 1, [s.name for s in finished]
    return finished[0]


@pytest.mark.parametrize("httpx", HTTPX_MODULES, ids=lambda m: m.__name__)
def test_httpx_sync_json(server, spans, httpx):
    exporter, _ = spans
    with httpx.Client() as client:
        response = client.post(f"{server.url}/v1/chat/completions", json=CHAT)
    assert response.json() == CHAT_RESPONSE
    span = _only(exporter)
    assert span.name == "chat gpt-4o-mini"
    assert span.kind == trace_api.SpanKind.CLIENT
    attrs = span.attributes
    assert attrs["gen_ai.provider.name"] == "openai_compatible"
    assert attrs["output.value"] == "pong"
    assert attrs["gen_ai.usage.total_tokens"] == 6
    assert attrs["traceai.capture.layer"] == "wire"
    assert "gen_ai.request.stream" not in attrs


@pytest.mark.parametrize("httpx", HTTPX_MODULES, ids=lambda m: m.__name__)
def test_httpx_sync_stream_passthrough(server, spans, httpx):
    exporter, _ = spans
    with httpx.Client() as client:
        with client.stream("POST", f"{server.url}/v1/chat/completions", json={**CHAT, "stream": True}) as response:
            assert exporter.get_finished_spans() == ()
            body = b"".join(response.iter_bytes())
    assert body == sse_body()
    attrs = _only(exporter).attributes
    assert attrs["output.value"] == "pong"
    assert attrs["gen_ai.request.stream"] is True
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.response.time_to_first_chunk"] >= 0


@pytest.mark.parametrize("httpx", HTTPX_MODULES, ids=lambda m: m.__name__)
def test_httpx_async_json_and_stream(server, spans, httpx):
    exporter, _ = spans

    async def run():
        async with httpx.AsyncClient() as client:
            plain = await client.post(f"{server.url}/v1/chat/completions", json=CHAT)
            chunks = []
            async with client.stream("POST", f"{server.url}/v1/chat/completions", json={**CHAT, "stream": True}) as streamed:
                async for chunk in streamed.aiter_bytes():
                    chunks.append(chunk)
            return plain.json(), b"".join(chunks)

    plain, streamed = asyncio.run(run())
    assert plain == CHAT_RESPONSE
    assert streamed == sse_body()
    outputs = sorted(s.attributes["output.value"] for s in exporter.get_finished_spans())
    assert outputs == ["pong", "pong"]


@pytest.mark.parametrize("httpx", HTTPX_MODULES, ids=lambda m: m.__name__)
def test_httpx_audio_is_counted_not_buffered(server, spans, httpx):
    exporter, _ = spans
    with httpx.Client() as client:
        response = client.post(f"{server.url}/v1/audio/speech", json={"model": "tts-1", "voice": "alloy", "input": "Hello"})
    assert response.content == AUDIO_BYTES
    attrs = _only(exporter).attributes
    assert attrs["traceai.audio.output_bytes"] == len(AUDIO_BYTES)
    assert attrs["gen_ai.audio.mime_type"] == "audio/mpeg"
    assert attrs["input.value"] == "Hello"
    assert "output.value" not in attrs


def test_requests_json_and_stream(server, spans):
    exporter, _ = spans
    plain = http.post(f"{server.url}/v1/chat/completions", json=CHAT)
    assert plain.json() == CHAT_RESPONSE
    streamed = http.post(f"{server.url}/v1/chat/completions", json={**CHAT, "stream": True}, stream=True)
    lines = list(streamed.iter_lines())
    assert b"data: [DONE]" in lines
    finished = exporter.get_finished_spans()
    assert [s.attributes["output.value"] for s in finished] == ["pong", "pong"]
    assert finished[1].attributes["gen_ai.request.stream"] is True


def test_aiohttp_json_and_stream(server, spans):
    aiohttp = pytest.importorskip("aiohttp")
    exporter, _ = spans

    async def run():
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{server.url}/v1/chat/completions", json=CHAT) as response:
                plain = await response.json()
            lines = []
            async with session.post(f"{server.url}/v1/chat/completions", json={**CHAT, "stream": True}) as response:
                async for line in response.content:
                    lines.append(line)
            return plain, b"".join(lines)

    plain, streamed = asyncio.run(run())
    assert plain == CHAT_RESPONSE
    assert streamed == sse_body()
    assert sorted(s.attributes["output.value"] for s in exporter.get_finished_spans()) == ["pong", "pong"]


def test_library_layer_span_suppresses_wire_capture(server, spans):
    exporter, tracer = spans
    with tracer.start_as_current_span("openai.chat", attributes={"gen_ai.span.kind": "LLM"}):
        http.post(f"{server.url}/v1/chat/completions", json=CHAT)
    assert [s.name for s in exporter.get_finished_spans()] == ["openai.chat"]


def test_framework_span_becomes_parent(server, spans):
    exporter, tracer = spans
    with tracer.start_as_current_span("agent step", attributes={"gen_ai.span.kind": "CHAIN"}) as parent:
        http.post(f"{server.url}/v1/chat/completions", json=CHAT)
    wire_span = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat"))
    assert wire_span.parent.span_id == parent.get_span_context().span_id


def test_suppressed_and_non_ai_requests_are_ignored(server, spans):
    exporter, _ = spans
    with suppress_tracing():
        http.post(f"{server.url}/v1/chat/completions", json=CHAT)
    http.get(f"{server.url}/v1/models")
    http.post(f"{server.url}/v1/chat/completions", data=b"not json", headers={"Content-Type": "application/json"})
    assert exporter.get_finished_spans() == ()


def test_http_error_and_transport_failure(server, spans):
    exporter, _ = spans
    response = http.post(f"{server.url}/v1/chat/completions", json={**CHAT, "model": "broken"})
    assert response.status_code == 401
    with pytest.raises(requests.ConnectionError):
        http.post("http://127.0.0.1:9/v1/chat/completions", json=CHAT)
    rejected, failed = exporter.get_finished_spans()
    assert rejected.status.status_code == trace_api.StatusCode.ERROR
    assert rejected.status.description == "bad key"
    assert rejected.attributes["error.type"] == "401"
    assert failed.status.status_code == trace_api.StatusCode.ERROR
    assert failed.attributes["error.type"] == "ConnectionError"


def test_masking_applies_to_wire_spans(server):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    wire.enable(FITracer(provider.get_tracer("test"), config=TraceConfig(hide_inputs=True, hide_outputs=True)))
    try:
        http.post(f"{server.url}/v1/chat/completions", json=CHAT)
    finally:
        wire.disable()
    attrs = _only(exporter).attributes
    assert attrs.get("input.value") != json.dumps(CHAT)
    assert attrs.get("output.value") != "pong"
    assert "gen_ai.input.messages.0.message.content" not in attrs


def test_oversized_bodies_are_truncated_not_buffered(server):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    wire.enable(FITracer(provider.get_tracer("test"), config=TraceConfig()), max_body_bytes=160)
    try:
        response = http.post(f"{server.url}/v1/chat/completions", json={**CHAT, "stream": True}, stream=True)
        assert b"".join(response.iter_content(64)) == sse_body()
    finally:
        wire.disable()
    attrs = _only(exporter).attributes
    assert attrs["traceai.wire.truncated"] is True
    assert "output.value" not in attrs


def test_disable_stops_capture(server, spans):
    exporter, _ = spans
    wire.disable()
    http.post(f"{server.url}/v1/chat/completions", json=CHAT)
    assert exporter.get_finished_spans() == ()
    assert wire.transport_statuses(["requests"])["requests"] in ("pending", "instrumented")
