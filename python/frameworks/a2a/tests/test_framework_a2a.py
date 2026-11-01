"""Real SDK requests with synthetic transports; no vendor calls or credentials."""

import asyncio
import json
import threading
from contextlib import aclosing
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version

import httpx
import pytest
from a2a.client.errors import A2AClientError
from a2a.types import SendMessageRequest
from a2a.utils import telemetry
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from traceai_a2a import A2AInstrumentor, A2ATracingMiddleware

CURRENT = version("a2a-sdk").startswith("1.")
MODERN = not version("a2a-sdk").startswith("0.2.")
TRANSPORTS = ["jsonrpc", "rest"] if MODERN else ["jsonrpc"]
SECRET = "synthetic-sensitive-marker"
URL = f"https://user:password@offline.invalid/private-{SECRET}?token={SECRET}"


def request(streaming=False):
    if CURRENT:
        return SendMessageRequest(
            message={"message_id": "request-id", "role": 1, "parts": [{"text": SECRET}]}
        )
    if MODERN:
        from a2a.types import MessageSendParams

        return MessageSendParams(
            message={
                "messageId": "request-id",
                "role": "user",
                "parts": [{"kind": "text", "text": SECRET}],
            }
        )
    from a2a.types import SendStreamingMessageRequest

    cls = SendStreamingMessageRequest if streaming else SendMessageRequest
    return cls(
        id="request-id",
        params={
            "message": {
                "messageId": "request-id",
                "role": "user",
                "parts": [{"type": "text", "text": SECRET}],
            }
        },
    )


def task(state="completed"):
    result = {
        "id": "task-id",
        "contextId": "context-id",
        "status": {"state": f"TASK_STATE_{state.upper()}" if CURRENT else state},
    }
    if not CURRENT:
        result["kind"] = "task"
    return {"task": result} if CURRENT else result


def response(req, transport="jsonrpc", streaming=False, state="completed"):
    result = task(state)
    if transport == "rest" and not CURRENT:
        result.pop("kind")
        result["status"]["state"] = f"TASK_STATE_{state.upper()}"
        result = {"task": result}
    body = (
        {"jsonrpc": "2.0", "id": json.loads(req.content)["id"], "result": result}
        if transport == "jsonrpc"
        else result
    )
    if streaming:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(body)}\n\n".encode(),
        )
    return httpx.Response(200, json=body)


def client(http, transport="jsonrpc"):
    if MODERN:
        from a2a.client.transports.jsonrpc import JsonRpcTransport
        from a2a.client.transports.rest import RestTransport
        from a2a.types import AgentCard

        return (JsonRpcTransport if transport == "jsonrpc" else RestTransport)(
            http, AgentCard() if CURRENT else None, URL
        )
    from a2a.client import A2AClient

    return A2AClient(http, url=URL)


def call_options():
    headers = {"authorization": SECRET, "x-preserved": "yes", "Traceparent": "stale"}
    if CURRENT:
        from a2a.client import ClientCallContext

        options = ClientCallContext(service_parameters=headers, timeout=7)
        return {"context": options}, options.service_parameters
    if MODERN:
        from a2a.client import ClientCallContext

        options = ClientCallContext(
            state={"http_kwargs": {"headers": headers, "timeout": 7}}
        )
        return {"context": options}, options.state["http_kwargs"]["headers"]
    return {"http_kwargs": {"headers": headers, "timeout": 7}}, headers


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = A2AInstrumentor()
    original_trace = telemetry.trace
    global_provider = trace.get_tracer_provider()
    instrumentor.instrument(tracer_provider=provider)
    yield provider, exporter
    instrumentor.uninstrument()
    assert telemetry.trace is original_trace
    assert trace.get_tracer_provider() is global_provider
    provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_real_request_one_native_span_and_propagation(tracing, transport):
    provider, exporter = tracing
    captured = []

    async def handle(req):
        captured.append(req)
        return response(req, transport)

    options, original_headers = call_options()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            result = await client(http, transport).send_message(
                request=request(), **options
            )
            assert result is not None
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    span = spans[0]
    assert span.instrumentation_scope.name == "a2a-python-sdk"
    assert span.kind == SpanKind.CLIENT
    assert span.parent.span_id == parent.get_span_context().span_id
    assert span.attributes["gen_ai.span.kind"] == "A2A_CLIENT"
    assert span.attributes["gen_ai.a2a.message.role"] == "user"
    assert span.attributes["gen_ai.a2a.message.parts.count"] == 1
    assert span.attributes["gen_ai.a2a.task.id"] == "task-id"
    assert span.attributes["gen_ai.a2a.task.state"] == "completed"
    assert span.attributes["gen_ai.a2a.agent.url"] == "https://offline.invalid"
    parts = captured[0].headers["traceparent"].split("-")
    assert parts[1] == format(span.context.trace_id, "032x")
    assert parts[2] == format(span.context.span_id, "016x")
    assert captured[0].headers["x-preserved"] == "yes"
    assert original_headers == {
        "authorization": SECRET,
        "x-preserved": "yes",
        "Traceparent": "stale",
    }
    assert SECRET not in span.to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("exit_mode", ["exhaust", "early_close", "unstarted_close"])
async def test_stream_lifetime_and_no_context_leak(tracing, transport, exit_mode):
    provider, exporter = tracing
    captured = []

    async def handle(req):
        captured.append(req)
        return response(req, transport, streaming=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            stream = client(http, transport).send_message_streaming(request(True))
            assert exporter.get_finished_spans() == ()
            assert trace.get_current_span() is parent
            if exit_mode != "unstarted_close":
                await stream.__anext__()
                assert exporter.get_finished_spans() == ()
                assert trace.get_current_span() is parent
            if exit_mode == "exhaust":
                with pytest.raises(StopAsyncIteration):
                    await stream.__anext__()
            await stream.aclose()
            await stream.aclose()
            assert len(exporter.get_finished_spans()) == 1
            assert trace.get_current_span() is parent
    span = exporter.get_finished_spans()[0]
    assert span.attributes["gen_ai.a2a.streaming"] is True
    assert span.kind == SpanKind.CLIENT
    if captured:
        assert captured[0].headers["traceparent"].split("-")[2] == format(
            span.context.span_id, "016x"
        )
    if exit_mode == "exhaust":
        assert span.attributes["gen_ai.a2a.task.state"] == "completed"


class FailingBody(httpx.AsyncByteStream):
    def __init__(self, first, exception):
        self.first, self.exception = first, exception
        self.closed = False

    async def __aiter__(self):
        yield self.first
        raise self.exception

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception", [RuntimeError(SECRET), asyncio.CancelledError(SECRET)]
)
async def test_stream_failure_or_cancellation_after_first_event(tracing, exception):
    _, exporter = tracing
    bodies = []

    async def handle(req):
        body = FailingBody(response(req, streaming=True).content, exception)
        bodies.append(body)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        stream = client(http).send_message_streaming(request(True))
        await stream.__anext__()
        assert not exporter.get_finished_spans()
        with pytest.raises(type(exception)):
            await stream.__anext__()
        await stream.aclose()
    (span,) = exporter.get_finished_spans()
    assert bodies[0].closed
    assert SECRET not in span.to_json()
    if isinstance(exception, asyncio.CancelledError):
        assert span.attributes["gen_ai.a2a.cancelled"] is True
    else:
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes["error.type"] == "RuntimeError"
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_transport_failure_is_private(tracing, streaming):
    _, exporter = tracing

    async def handle(req):
        raise httpx.ConnectError(SECRET, request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises((A2AClientError, httpx.ConnectError)):
            if streaming:
                async with aclosing(
                    client(http).send_message_streaming(request(True))
                ) as events:
                    await events.__anext__()
            else:
                await client(http).send_message(request())
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert "error.type" in span.attributes
    assert not span.events
    assert SECRET not in span.to_json()


@pytest.mark.asyncio
async def test_failed_task_is_not_success(tracing):
    _, exporter = tracing
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: response(req, state="failed"))
    ) as http:
        await client(http).send_message(request())
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["gen_ai.a2a.task.state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_suppression_covers_native_helpers_and_lazy_iteration(tracing, streaming):
    _, exporter = tracing
    headers = []

    async def handle(req):
        headers.append(req.headers)
        return response(req, streaming=streaming)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            if streaming:
                stream = client(http).send_message_streaming(request(True))
            else:
                await client(http).send_message(request())
        finally:
            context.detach(token)
        if streaming:
            async for _ in stream:
                pass
    assert not exporter.get_finished_spans()
    assert "traceparent" not in headers[0]


@pytest.mark.asyncio
async def test_concurrent_calls_keep_contexts_separate(tracing):
    provider, exporter = tracing
    captured = []

    async def handle(req):
        await asyncio.sleep(0)
        captured.append(req.headers["traceparent"])
        return response(req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        shared = client(http)

        async def run(index):
            with provider.get_tracer("test").start_as_current_span(f"parent-{index}"):
                await shared.send_message(request())

        await asyncio.gather(*(run(i) for i in range(5)))
    calls = [s for s in exporter.get_finished_spans() if s.kind == SpanKind.CLIENT]
    assert len(calls) == 5
    assert len({s.context.trace_id for s in calls}) == 5
    assert {h.split("-")[2] for h in captured} == {
        format(s.context.span_id, "016x") for s in calls
    }


def test_instrument_idempotent_and_restore(tracing):
    provider, _ = tracing
    adapter = A2AInstrumentor()._adapter
    patches = tuple(adapter._patches)
    A2AInstrumentor().instrument(tracer_provider=provider)
    assert A2AInstrumentor()._adapter is adapter
    A2AInstrumentor().uninstrument()
    for cls, name, original, _ in patches:
        assert getattr(cls, name) is original
    A2AInstrumentor().instrument(tracer_provider=provider)


def test_disabled_native_tracing_is_not_false_success(monkeypatch):
    monkeypatch.setattr(telemetry, "otel_enabled", False, raising=False)
    with pytest.raises(RuntimeError, match="disabled"):
        A2AInstrumentor().instrument()
    assert not A2AInstrumentor().is_instrumented_by_opentelemetry


@pytest.mark.asyncio
async def test_asgi_propagation_and_otlp_wire_privacy(tracing):
    provider, exporter = tracing

    async def app(scope, receive, send):
        req = await receive()
        payload = json.loads(req["body"])
        with provider.get_tracer("agent").start_as_current_span("agent-work"):
            result = {"jsonrpc": "2.0", "id": payload["id"], "result": task()}
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(result).encode()})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=A2ATracingMiddleware(app, tracer_provider=provider, agent_url=URL)
        )
    ) as http:
        await client(http).send_message(request())
    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    remote = next(s for s in spans if s.kind == SpanKind.SERVER)
    outbound = next(s for s in spans if s.kind == SpanKind.CLIENT)
    child = next(s for s in spans if s.name == "agent-work")
    assert remote.parent.is_remote
    assert remote.parent.span_id == outbound.context.span_id
    assert child.parent.span_id == remote.context.span_id
    assert len({s.context.trace_id for s in spans}) == 1
    assert "gen_ai.a2a.task.state" not in remote.attributes

    async def fail(req):
        raise httpx.ConnectError(SECRET, request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        with pytest.raises((A2AClientError, httpx.ConnectError)):
            async with aclosing(
                client(http).send_message_streaming(request(True))
            ) as events:
                await events.__anext__()
    spans = exporter.get_finished_spans()
    assert spans[-1].status.status_code == StatusCode.ERROR

    captured = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(self.rfile.read(int(self.headers["content-length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    collector = HTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=collector.serve_forever, daemon=True)
    thread.start()
    otlp = OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{collector.server_port}/v1/traces", headers={}
    )
    try:
        assert otlp.export(spans) == SpanExportResult.SUCCESS
    finally:
        otlp.shutdown()
        collector.shutdown()
        collector.server_close()
        thread.join()
    wire = ExportTraceServiceRequest.FromString(captured[0])
    exported = [
        span
        for resource in wire.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]
    assert len(exported) == 4
    assert SECRET.encode() not in captured[0]
    assert b"password" not in captured[0]
    assert len({s.trace_id for s in exported}) == 2
    assert {s.kind for s in exported} == {1, 2, 3}
    failed = [s for s in exported if s.status.code == 2]
    assert len(failed) == 1
    assert not failed[0].status.message and not failed[0].events


@pytest.mark.asyncio
async def test_middleware_reuses_http_span_and_redacts_errors(tracing):
    provider, exporter = tracing

    async def app(*args):
        raise ValueError(SECRET)

    middleware = A2ATracingMiddleware(app, tracer_provider=provider)
    with (
        provider.get_tracer("http").start_as_current_span(
            "HTTP",
            kind=SpanKind.SERVER,
            record_exception=False,
            set_status_on_exception=False,
        ),
        pytest.raises(ValueError),
    ):
        await middleware({"type": "http", "path": f"/tasks/{SECRET}"}, None, None)
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert SECRET not in span.to_json()
