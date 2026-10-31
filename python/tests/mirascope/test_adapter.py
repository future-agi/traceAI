import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from fi_instrumentation.mirascope import MirascopeSpanExporter, configure
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import (
    Link,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
)


class Exporter(InMemorySpanExporter):
    def __init__(self):
        super().__init__()
        self.close_count = 0

    def shutdown(self):
        self.close_count += 1


def source_span(scope="mirascope.llm", operation="chat"):
    context = SpanContext(
        1, 2, False, TraceFlags(1), TraceState([("tenant", "PRIVATE_STATE")])
    )
    return ReadableSpan(
        name="chat openai/gpt-4o-mini:completions",
        context=context,
        parent=SpanContext(1, 3, False),
        start_time=1,
        end_time=2,
        resource=Resource(
            {"service.name": "PRIVATE_SERVICE", "private": "PRIVATE_RESOURCE"}
        ),
        instrumentation_scope=InstrumentationScope(
            scope, "2", attributes={"private": "PRIVATE_SCOPE"}
        ),
        attributes={
            "gen_ai.operation.name": operation,
            "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "openai/gpt-4o-mini:completions",
            "gen_ai.usage.input_tokens": 5,
            "gen_ai.usage.output_tokens": 3,
            "gen_ai.input.messages": '[{"role":"user","content":"PRIVATE_INPUT"}]',
            "gen_ai.output.messages": '[{"role":"assistant","content":"PRIVATE_OUTPUT"}]',
            "gen_ai.tool.definitions": "PRIVATE_SCHEMA",
            "mirascope.trace.arg_values": "PRIVATE_ARGS",
            "mirascope.trace.output": "PRIVATE_RESULT",
            "mirascope.ops.session.id": "session-1",
            "new.upstream.field": "PRIVATE_FUTURE",
            "error.message": "PRIVATE_ERROR",
            "exception.stacktrace": "PRIVATE_STACK",
            "nested.exception_stacktrace": "PRIVATE_STACK",
        },
        events=[
            Event(
                "exception",
                {
                    "exception.type": "ValueError",
                    "exception.message": "PRIVATE_ERROR",
                    "exception.stacktrace": "PRIVATE_STACK",
                },
                1,
            ),
            Event("PRIVATE_EVENT_NAME", {"message": "PRIVATE_EVENT"}, 1),
        ],
        links=[Link(context, {"private": "PRIVATE_LINK"})],
        status=Status(StatusCode.ERROR, "PRIVATE_STATUS"),
    )


def wire_bytes(spans):
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

    return encode_spans(spans).SerializeToString()


@pytest.mark.parametrize("capture", [False, True])
def test_wire_privacy_and_backend_mapping(capture):
    target = Exporter()
    wrapper = MirascopeSpanExporter(
        target, project_name="project", capture_content=capture
    )
    original = source_span()
    before = wire_bytes([original])
    assert wrapper.export([original]) == SpanExportResult.SUCCESS
    spans = target.get_finished_spans()
    encoded = wire_bytes(spans)
    for never in (
        b"PRIVATE_STACK",
        b"PRIVATE_EVENT",
        b"PRIVATE_LINK",
        b"PRIVATE_STATE",
        b"PRIVATE_SCOPE",
        b"PRIVATE_RESOURCE",
        b"PRIVATE_SERVICE",
    ):
        assert never not in encoded
    for content in (
        b"PRIVATE_INPUT",
        b"PRIVATE_OUTPUT",
        b"PRIVATE_ARGS",
        b"PRIVATE_RESULT",
        b"PRIVATE_SCHEMA",
        b"PRIVATE_FUTURE",
        b"PRIVATE_ERROR",
        b"PRIVATE_STATUS",
    ):
        assert (content in encoded) is capture
    assert (
        wire_bytes([original]) == before
    )  # copies only; other exporters are untouched
    span = spans[0]
    assert span.context.trace_id == original.context.trace_id
    assert span.context.span_id == original.context.span_id
    assert span.parent.span_id == original.parent.span_id
    assert span.status.status_code == StatusCode.ERROR
    assert span.events[0].attributes["exception.type"] == "ValueError"
    assert span.attributes["gen_ai.span.kind"] == "LLM"
    assert span.attributes["session.id"] == "session-1"
    assert span.attributes["gen_ai.usage.input_tokens"] == 5
    assert span.resource.attributes == {
        "project_name": "project",
        "project_type": "observe",
        "service.name": "mirascope-app",
    }
    assert ("input.value" in span.attributes) is capture
    if capture:
        assert (
            span.attributes["input.value"]
            == original.attributes["gen_ai.input.messages"]
        )
        assert (
            span.attributes["output.value"]
            == original.attributes["gen_ai.output.messages"]
        )


@pytest.mark.parametrize(
    "operation,kind", [("execute_tool", "TOOL"), ("resume", "CHAIN"), ("chat", "LLM")]
)
def test_kind_mapping(operation, kind):
    target = Exporter()
    MirascopeSpanExporter(target, project_name="project").export(
        [source_span(operation=operation)]
    )
    assert target.get_finished_spans()[0].attributes["gen_ai.span.kind"] == kind


@pytest.mark.parametrize(
    "flag",
    [
        "FI_HIDE_INPUTS",
        "FI_HIDE_OUTPUTS",
        "FI_HIDE_INPUT_MESSAGES",
        "FI_HIDE_OUTPUT_MESSAGES",
        "FI_HIDE_INPUT_TEXT",
        "FI_HIDE_OUTPUT_TEXT",
        "FI_HIDE_INPUT_IMAGES",
        "FI_HIDE_LLM_INVOCATION_PARAMETERS",
    ],
)
def test_hide_flags_override_capture(monkeypatch, flag):
    monkeypatch.setenv(flag, "true")
    target = Exporter()
    MirascopeSpanExporter(target, project_name="project", capture_content=True).export(
        [source_span()]
    )
    assert b"PRIVATE_" not in wire_bytes(target.get_finished_spans())


def test_error_hide_override(monkeypatch):
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    target = Exporter()
    MirascopeSpanExporter(target, project_name="project", capture_content=True).export(
        [source_span()]
    )
    encoded = wire_bytes(target.get_finished_spans())
    assert b"PRIVATE_INPUT" in encoded
    for value in (b"PRIVATE_ERROR", b"PRIVATE_STATUS", b"PRIVATE_STACK"):
        assert value not in encoded


def test_unrelated_scope_and_fail_closed(monkeypatch, caplog):
    target = Exporter()
    wrapper = MirascopeSpanExporter(target, project_name="project")
    assert (
        wrapper.export([source_span(scope="unrelated.library")])
        == SpanExportResult.SUCCESS
    )
    assert not target.get_finished_spans()

    def fail(span):
        raise ValueError("PRIVATE_FAILURE")

    monkeypatch.setattr(wrapper, "_copy", fail)
    assert wrapper.export([source_span()]) == SpanExportResult.FAILURE
    assert not target.get_finished_spans()
    assert "PRIVATE_FAILURE" not in caplog.text


@pytest.fixture
def native():
    ops = pytest.importorskip("mirascope.ops")
    yield ops
    ops.uninstrument_llm()


def test_provider_reuse_idempotency_and_borrowed_shutdown(native):
    provider = TracerProvider()
    other = Exporter()
    provider.add_span_processor(SimpleSpanProcessor(other))
    trace.set_tracer_provider(provider)
    target = Exporter()
    handle = configure(project_name="project", exporter=target)
    assert trace.get_tracer_provider() is provider
    assert configure(project_name="project", exporter=target) is handle

    @native.trace
    def task(value: str):
        return value

    assert task("PRIVATE_INPUT") == "PRIVATE_INPUT"
    assert handle.force_flush()
    assert len(target.get_finished_spans()) == 1
    assert len(other.get_finished_spans()) == 1
    assert "PRIVATE_INPUT" in str(other.get_finished_spans()[0].attributes)
    assert "PRIVATE_INPUT" not in str(target.get_finished_spans()[0].attributes)
    handle.shutdown()
    handle.shutdown()
    assert target.close_count == 1
    assert other.close_count == 0
    task("PRIVATE_AFTER")
    assert len(other.get_finished_spans()) == 2
    assert len(target.get_finished_spans()) == 1
    with pytest.raises(RuntimeError, match="shut down"):
        configure(project_name="project", exporter=target)


def test_provider_conflict_and_option_conflict(native):
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    other = TracerProvider()
    try:
        with pytest.raises(ValueError, match="global provider"):
            configure(
                project_name="project", tracer_provider=other, exporter=Exporter()
            )
    finally:
        other.shutdown()
    target = Exporter()
    configure(project_name="project", exporter=target)
    with pytest.raises(ValueError, match="different options"):
        configure(project_name="other-project", exporter=target)
    with pytest.raises(ValueError, match="different options"):
        configure(project_name="project", exporter=target, capture_content=True)


@pytest.mark.parametrize(
    "kwargs,exception",
    [
        ({"project_name": ""}, ValueError),
        ({"project_name": 12}, ValueError),
        ({"capture_content": "false"}, TypeError),
        ({"service_name": ""}, ValueError),
        ({"endpoint": ""}, ValueError),
        ({"headers": {"key": 3}}, TypeError),
        ({"tracer_provider": object()}, TypeError),
    ],
)
def test_validation(native, kwargs, exception):
    with pytest.raises(exception):
        configure(**dict({"project_name": "project"}, **kwargs))


@pytest.mark.parametrize("capture", [False, True])
def test_real_native_functions_sessions_events_and_errors(native, capture):
    target = Exporter()
    handle = configure(project_name="project", capture_content=capture, exporter=target)

    @native.trace
    def task(value: str):
        with native.span("child") as span:
            span.info("PRIVATE_EVENT_NAME")
        return value + "-result"

    @native.trace
    async def async_task(value: str):
        return value + "-async-result"

    @native.trace
    def failing(value: str):
        raise ValueError(value)

    with native.session(id="session-1"):
        assert task("PRIVATE_INPUT") == "PRIVATE_INPUT-result"
        assert asyncio.run(async_task("PRIVATE_ASYNC")) == "PRIVATE_ASYNC-async-result"
        with pytest.raises(ValueError, match="PRIVATE_ERROR"):
            failing("PRIVATE_ERROR")
    assert handle.force_flush()
    spans = target.get_finished_spans()
    assert len(spans) == 4
    assert all(span.attributes["session.id"] == "session-1" for span in spans)
    encoded = wire_bytes(spans)
    assert (b"PRIVATE_INPUT" in encoded) is capture
    assert (b"PRIVATE_ASYNC" in encoded) is capture
    assert (b"PRIVATE_ERROR" in encoded) is capture
    assert b"PRIVATE_EVENT_NAME" not in encoded
    assert b"exception.stacktrace" not in encoded
    task_span = next(s for s in spans if s.name == "task")
    child = next(s for s in spans if s.name == "child")
    assert child.parent.span_id == task_span.context.span_id
    assert task_span.attributes["gen_ai.span.kind"] == "CHAIN"
    if capture:
        assert task_span.attributes["output.value"] == "PRIVATE_INPUT-result"


def test_actual_otlp_http_headers_path_and_flush(native):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, dict(self.headers), payload))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        handle = configure(
            project_name="wire-project",
            endpoint=f"http://127.0.0.1:{server.server_port}/tracer/v1/traces",
        )
        with native.span("request", private="PRIVATE_PAYLOAD"):
            pass
        assert handle.force_flush()
        handle.shutdown()
        assert len(received) == 1
        path, headers, payload = received[0]
        headers = {k.lower(): v for k, v in headers.items()}
        assert path == "/tracer/v1/traces"
        assert headers["x-api-key"] == "synthetic-api-key"
        assert headers["x-secret-key"] == "synthetic-secret-key"
        assert headers.get("content-encoding", "identity") == "identity"
        assert headers["content-type"] == "application/x-protobuf"
        assert b"PRIVATE_PAYLOAD" not in payload
        decoded = ExportTraceServiceRequest.FromString(payload)
        resource = {
            item.key: item.value.string_value
            for item in decoded.resource_spans[0].resource.attributes
        }
        assert resource["project_name"] == "wire-project"
        assert resource["project_type"] == "observe"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize(
    "mode", ["sync", "async", "stream", "async_stream", "resume", "error"]
)
def test_native_model_paths(native, monkeypatch, capture, mode):
    import httpx
    from mirascope import llm
    from mirascope.llm.providers.provider_registry import reset_provider_registry

    reset_provider_registry()
    llm.register_provider(
        "openai", api_key="synthetic", base_url="http://127.0.0.1:9/v1"
    )
    requests = []

    def respond(request, **kwargs):
        body = json.loads(request.content)
        requests.append(body)
        if mode == "error":
            return httpx.Response(
                400,
                request=request,
                json={
                    "error": {
                        "message": "PRIVATE_ERROR",
                        "type": "invalid_request_error",
                    }
                },
            )
        base = {"id": "synthetic-response", "created": 0, "model": "gpt-4o-mini"}
        usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
        if mode == "resume" and len(requests) == 1:
            return httpx.Response(
                200,
                request=request,
                json=dict(
                    base,
                    object="chat.completion",
                    usage=usage,
                    choices=[
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "weather",
                                            "arguments": '{"city":"PRIVATE_CITY"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                ),
            )
        if body.get("stream"):
            chunks = [
                dict(
                    base,
                    object="chat.completion.chunk",
                    choices=[
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "PRIVATE_ANSWER"},
                            "finish_reason": None,
                        }
                    ],
                ),
                dict(
                    base,
                    object="chat.completion.chunk",
                    choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
                ),
                dict(base, object="chat.completion.chunk", choices=[], usage=usage),
            ]
            data = (
                "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                content=data,
            )
        return httpx.Response(
            200,
            request=request,
            json=dict(
                base,
                object="chat.completion",
                usage=usage,
                choices=[
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "PRIVATE_ANSWER"},
                        "finish_reason": "stop",
                    }
                ],
            ),
        )

    def send(client, request, **kwargs):
        return respond(request, **kwargs)

    async def async_send(client, request, **kwargs):
        return respond(request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    target = Exporter()
    handle = configure(project_name="project", exporter=target, capture_content=capture)
    native.instrument_llm()
    model = llm.Model("openai/gpt-4o-mini:completions")
    with native.span("parent"):
        if mode == "resume":

            @llm.tool
            def weather(city: str) -> str:
                """Return the weather for a city."""
                with native.span(
                    "weather",
                    **{
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": "weather",
                    },
                ):
                    return "PRIVATE_TOOL_RESULT"

            response = model.call("PRIVATE_PROMPT", tools=[weather])
            assert response.resume(response.execute_tools()).text() == "PRIVATE_ANSWER"
        elif mode == "sync":
            assert model.call("PRIVATE_PROMPT").text() == "PRIVATE_ANSWER"
        elif mode == "async":
            response = asyncio.run(model.call_async("PRIVATE_PROMPT"))
            assert response.text() == "PRIVATE_ANSWER"
        elif mode == "stream":
            response = model.stream("PRIVATE_PROMPT")
            assert "".join(response.text_stream(sep="")) == "PRIVATE_ANSWER"
        elif mode == "async_stream":

            async def consume():
                response = await model.stream_async("PRIVATE_PROMPT")
                return "".join([chunk async for chunk in response.text_stream(sep="")])

            assert asyncio.run(consume()) == "PRIVATE_ANSWER"
        else:
            with pytest.raises(Exception, match="PRIVATE_ERROR"):
                model.call("PRIVATE_PROMPT")
    assert handle.force_flush()
    assert len(requests) == (2 if mode == "resume" else 1)
    spans = target.get_finished_spans()
    assert len(spans) == (5 if mode == "resume" else 2)  # no SDK duplicates
    if mode == "resume":
        models = [s for s in spans if s.attributes["gen_ai.span.kind"] == "LLM"]
        assert len(models) == 2
        assert (
            sum(s.attributes.get("gen_ai.usage.input_tokens", 0) for s in spans) == 10
        )
        assert (
            len([s for s in spans if s.attributes["gen_ai.span.kind"] == "TOOL"]) == 1
        )
        assert (b"PRIVATE_TOOL_RESULT" in wire_bytes(spans)) is capture
        assert (b"PRIVATE_CITY" in wire_bytes(spans)) is capture
    model_span = next(s for s in spans if s.attributes["gen_ai.span.kind"] == "LLM")
    parent = next(s for s in spans if s.name == "parent")
    assert model_span.parent.span_id == parent.context.span_id
    assert (
        model_span.attributes["gen_ai.request.model"]
        == "openai/gpt-4o-mini:completions"
    )
    encoded = wire_bytes(spans)
    assert b"exception.stacktrace" not in encoded
    if mode == "error":
        assert model_span.status.status_code == StatusCode.ERROR
        assert (b"PRIVATE_ERROR" in encoded) is capture
    else:
        assert model_span.attributes["gen_ai.usage.input_tokens"] == 5
        assert model_span.attributes["gen_ai.usage.output_tokens"] == 3
        assert (b"PRIVATE_PROMPT" in encoded) is capture
        assert (b"PRIVATE_ANSWER" in encoded) is capture
        if capture:
            assert "PRIVATE_PROMPT" in model_span.attributes["input.value"]
            assert (
                "PRIVATE_CITY" if mode == "resume" else "PRIVATE_ANSWER"
            ) in model_span.attributes["output.value"]
    reset_provider_registry()
