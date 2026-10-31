"""Exercise real SDK clients against a synthetic loopback HTTP service."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version

import pytest
import together
from fi_instrumentation import TraceConfig
from opentelemetry import context as context_api
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_together import TogetherInstrumentor

LEGACY = version("together").startswith("1.")
INPUT = "synthetic private input"
OUTPUT = "synthetic private output"
TOOLS = [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}]
# Together 1.x raises its own exception tree; Stainless SDKs raise APIError.
API_ERROR = together.error.TogetherException if LEGACY else together.APIError
# Malformed SSE data surfaces as a JSON decode error (ValueError) during iteration.
STREAM_ERROR = (ValueError, API_ERROR)
TOOL_CALL = {"id": "call-1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"Paris"}'}}


def response_body(path, request):
    if path.endswith("embeddings"):
        return {"object": "list", "model": request["model"], "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}], "usage": {"prompt_tokens": 2, "total_tokens": 2}}
    choice = {"index": 0, "finish_reason": "stop"}
    chat = "chat/" in path
    if request.get("stream"):
        if chat or LEGACY:
            choice["delta"] = {"role": "assistant", "content": OUTPUT}
            if request.get("tools"):
                choice["delta"]["tool_calls"] = [dict(TOOL_CALL, index=0)]
        else:
            choice["text"] = OUTPUT
    elif chat:
        choice["message"] = {"role": "assistant", "content": OUTPUT}
        if request.get("tools"):
            choice["message"]["tool_calls"] = [TOOL_CALL]
    else:
        choice["text"] = OUTPUT
    object_type = "chat.completion" if chat else ("text.completion" if LEGACY else "text_completion")
    if request.get("stream"):
        object_type = "chat.completion.chunk" if chat else ("completion.chunk" if LEGACY else "text_completion")
    return {"id": "synthetic", "object": object_type, "created": 1, "model": request["model"], "choices": [choice], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}


@pytest.fixture
def server():
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/traces":
                payloads.append(body)
                self.send_response(200)
                self.end_headers()
                return
            request = json.loads(body)
            status = 400 if request["model"] == "error" else 200
            if status == 400:
                data = json.dumps({"error": {"message": OUTPUT, "type": "invalid_request_error"}}).encode()
            else:
                data = json.dumps(response_body(self.path, request)).encode()
            stream = request.get("stream") and status == 200
            if stream:
                # Invalid JSON after a valid chunk exercises errors during iteration.
                tail = b'data: not-json\n\n' if request["model"] == "stream-error" else b'data: [DONE]\n\n'
                data = b'data: ' + data + b'\n\n' + tail
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1", payloads
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.fixture
def tracing():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = TogetherInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    yield provider, exporter, instrumentor
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


def make_client(server, asynchronous=False):
    cls = together.AsyncTogether if asynchronous else together.Together
    return cls(api_key="synthetic-not-a-real-key", base_url=server[0], max_retries=0, timeout=5)


def call(client, surface, **options):
    options.setdefault("model", "synthetic-model")
    if surface == "chat":
        return client.chat.completions.create(messages=[{"role": "user", "content": INPUT}], **options)
    if surface == "completions":
        return client.completions.create(prompt=INPUT, **options)
    return client.embeddings.create(input=INPUT, **options)


def assert_span(exporter, surface, status=StatusCode.OK):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "together." + ("chat.completions" if surface == "chat" else surface)
    assert span.status.status_code == status
    assert span.attributes["gen_ai.provider.name"] == "together"
    assert span.attributes["gen_ai.span.kind"] == ("EMBEDDING" if surface == "embeddings" else "LLM")
    return span


@pytest.mark.parametrize("surface", ["chat", "completions", "embeddings"])
def test_sync_surfaces(server, tracing, surface):
    provider, exporter, _ = tracing
    client = make_client(server)
    parent = provider.get_tracer("test").start_span("parent")
    from opentelemetry.trace import use_span
    with use_span(parent, end_on_exit=False):
        response = call(client, surface)
    assert response.model == "synthetic-model"
    span = assert_span(exporter, surface)
    assert span.parent.span_id == parent.get_span_context().span_id
    model_key = "embedding.model_name" if surface == "embeddings" else "gen_ai.request.model"
    assert span.attributes[model_key] == "synthetic-model"
    if surface != "embeddings":
        assert span.attributes["gen_ai.usage.output_tokens"] == 3
        assert OUTPUT in span.attributes["output.value"]
    parent.end()
    if hasattr(client, "close"):
        client.close()


@pytest.mark.parametrize("surface", ["chat", "completions", "embeddings"])
def test_async_surfaces(server, tracing, surface):
    async def run():
        client = make_client(server, asynchronous=True)
        response = await call(client, surface)
        assert response.model == "synthetic-model"
        assert_span(tracing[1], surface)
        if hasattr(client, "close"):
            await client.close()
    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("surface", ["chat", "completions"])
def test_streaming(server, tracing, surface, asynchronous):
    exporter = tracing[1]
    options = {"stream": True}
    if surface == "chat":
        options["tools"] = TOOLS

    async def run():
        client = make_client(server, asynchronous=True)
        stream = await call(client, surface, **options)
        assert hasattr(stream, "__aiter__"), "a single await must return the async stream"
        assert not exporter.get_finished_spans()
        chunks = [chunk async for chunk in stream]
        assert len(chunks) == 1
        if hasattr(client, "close"):
            await client.close()

    if asynchronous:
        asyncio.run(run())
    else:
        client = make_client(server)
        stream = call(client, surface, **options)
        assert not exporter.get_finished_spans()
        assert len(list(stream)) == 1
        if hasattr(client, "close"):
            client.close()
    span = assert_span(exporter, surface)
    assert span.attributes["gen_ai.usage.output_tokens"] == 3
    assert OUTPUT in span.attributes["output.value"]
    if surface == "chat":
        assert "weather" in span.attributes["output.value"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("surface", ["chat", "completions", "embeddings"])
def test_api_errors(server, tracing, surface, asynchronous):
    async def run():
        client = make_client(server, asynchronous=True)
        try:
            with pytest.raises(API_ERROR):
                await call(client, surface, model="error")
        finally:
            if hasattr(client, "close"):
                await client.close()
    if asynchronous:
        asyncio.run(run())
    else:
        client = make_client(server)
        try:
            with pytest.raises(API_ERROR):
                call(client, surface, model="error")
        finally:
            if hasattr(client, "close"):
                client.close()
    span = assert_span(tracing[1], surface, StatusCode.ERROR)
    assert span.events


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("surface", ["chat", "completions"])
def test_stream_errors(server, tracing, surface, asynchronous):
    async def run():
        client = make_client(server, asynchronous=True)
        stream = await call(client, surface, model="stream-error", stream=True)
        try:
            with pytest.raises(STREAM_ERROR):
                async for _ in stream:
                    pass
        finally:
            if hasattr(client, "close"):
                await client.close()
    if asynchronous:
        asyncio.run(run())
    else:
        client = make_client(server)
        try:
            stream = call(client, surface, model="stream-error", stream=True)
            with pytest.raises(STREAM_ERROR):
                list(stream)
        finally:
            if hasattr(client, "close"):
                client.close()
    assert_span(tracing[1], surface, StatusCode.ERROR)


@pytest.mark.skipif(LEGACY, reason="Together 1.x has no raw-response API")
@pytest.mark.parametrize("stream", [False, True])
def test_raw_responses_end_spans_without_consuming_body(server, tracing, stream):
    exporter = tracing[1]
    client = make_client(server)
    try:
        raw = client.chat.completions.with_raw_response.create(
            model="synthetic-model", messages=[{"role": "user", "content": INPUT}], stream=stream
        )
        span = assert_span(exporter, "chat")
        if stream:
            assert "output.value" not in span.attributes
        else:
            assert span.attributes["output.value"] == "{}"
        parsed = raw.parse()
        if stream:
            assert len(list(parsed)) == 1
        else:
            assert parsed.choices[0].message.content == OUTPUT
        exporter.clear()
        with client.chat.completions.with_streaming_response.create(
            model="synthetic-model", messages=[{"role": "user", "content": INPUT}], stream=stream
        ) as response:
            assert len(exporter.get_finished_spans()) == 1
            body = response.parse()
            if stream:
                assert len(list(body)) == 1
            else:
                assert body.choices[0].message.content == OUTPUT
    finally:
        client.close()


def test_tools_and_suppression(server, tracing):
    client = make_client(server)
    try:
        response = call(client, "chat", tools=TOOLS)
        assert response.choices[0].message.tool_calls[0].function.name == "weather"
        span = assert_span(tracing[1], "chat")
        assert "weather" in span.attributes["gen_ai.tool.definitions.0.tool.json_schema"]
        assert span.attributes["gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"] == "weather"
        tracing[1].clear()
        token = context_api.attach(context_api.set_value(context_api._SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            call(client, "chat")
        finally:
            context_api.detach(token)
        assert not tracing[1].get_finished_spans()
    finally:
        if hasattr(client, "close"):
            client.close()


def test_repeated_instrument_and_uninstrument(server, tracing):
    provider, exporter, instrumentor = tracing
    client = make_client(server)
    try:
        instrumentor.instrument(tracer_provider=provider)
        call(client, "chat")
        assert_span(exporter, "chat")
        instrumentor.uninstrument()
        exporter.clear()
        for surface in ("chat", "completions", "embeddings"):
            call(client, surface)
        assert not exporter.get_finished_spans()
        instrumentor.instrument(tracer_provider=provider)
        call(client, "chat")
        assert_span(exporter, "chat")
    finally:
        if hasattr(client, "close"):
            client.close()


@pytest.mark.parametrize("hidden", [False, True])
def test_otlp_wire_privacy(server, hidden):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=server[0] + "/traces")))
    instrumentor = TogetherInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden))
    client = make_client(server)
    try:
        call(client, "chat")
        list(call(client, "completions", stream=True))
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        if hasattr(client, "close"):
            client.close()
    assert len(server[1]) == 2
    for payload in server[1]:
        request = ExportTraceServiceRequest.FromString(payload)
        spans = [s for r in request.resource_spans for scope in r.scope_spans for s in scope.spans]
        assert len(spans) == 1
        attributes = {kv.key: kv.value for kv in spans[0].attributes}
        assert attributes["gen_ai.provider.name"].string_value == "together"
        assert attributes["gen_ai.usage.output_tokens"].int_value == 3
        assert (INPUT.encode() in payload) is not hidden
        assert (OUTPUT.encode() in payload) is not hidden
