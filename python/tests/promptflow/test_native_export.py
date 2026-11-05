"""Native Prompt Flow contracts. All model responses are local HTTPX fixtures."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("promptflow.tracing")
import httpx
import openai
from fi_instrumentation import TraceConfig
from fi_instrumentation.promptflow import PromptFlowSpanExporter, configure
from opentelemetry import trace as otel
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from opentelemetry.util._once import Once
from promptflow.tracing import trace
from promptflow.tracing._integrations._openai_injector import recover_openai_api

PRIVATE = "synthetic-private-marker"


@pytest.fixture(autouse=True)
def native_state(monkeypatch):
    monkeypatch.setattr(otel, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(otel, "_TRACER_PROVIDER_SET_ONCE", Once())
    for name in (
        "PF_DISABLE_TRACING",
        "PF_TRACING_SKIP_LOCAL_SETUP",
        "PF_TRACING_SKIP_EXPORTER_SETUP",
    ):
        monkeypatch.delenv(name, raising=False)
    recover_openai_api()
    yield
    recover_openai_api()
    provider = otel.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()


@pytest.fixture
def sink():
    return InMemorySpanExporter()


def wire(sink):
    return encode_spans(sink.get_finished_spans()).SerializeToString()


@trace
def node(value):
    return "reply:" + value


@trace
def flow(value):
    return node(value)


def test_baseline_native_disabled_and_raw_privacy_bypass(monkeypatch, sink):
    """The old recipe cannot safely switch to raw native export."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(sink))
    otel.set_tracer_provider(provider)
    flow(PRIVATE)
    # 1.18.0 predates the disabled-by-default switch; 1.18.5 requires opt-in.
    from importlib.metadata import version

    if version("promptflow-tracing") == "1.18.5":
        assert sink.get_finished_spans() == ()
    sink.clear()
    monkeypatch.setenv("PF_DISABLE_TRACING", "false")
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    flow(PRIVATE)
    assert len(sink.get_finished_spans()) == 2
    assert all("fi.span.kind" not in s.attributes for s in sink.get_finished_spans())
    assert PRIVATE.encode() in wire(sink)


def test_topology_private_by_default_and_global(sink):
    provider = configure(
        project_name="promptflow-test", span_exporter=sink, batch=False
    )
    assert otel.get_tracer_provider() is provider
    assert flow(PRIVATE) == "reply:" + PRIVATE
    child, root = sink.get_finished_spans()
    assert child.parent.span_id == root.context.span_id
    assert child.context.trace_id == root.context.trace_id
    assert root.parent is None
    assert {s.name for s in (child, root)} == {"flow", "node"}
    assert all(s.attributes["fi.span.kind"] == "CHAIN" for s in (child, root))
    assert root.resource.attributes["project_name"] == "promptflow-test"
    assert root.resource.attributes["project_type"] == "observe"
    assert PRIVATE.encode() not in wire(sink)
    assert all(not s.events for s in (child, root))
    assert provider.force_flush()


def test_no_reconfigure_or_second_exporter(sink):
    provider = configure(
        project_name="promptflow-test", span_exporter=sink, batch=False
    )
    with pytest.raises(RuntimeError, match="global tracer provider"):
        configure(project_name="promptflow-test", span_exporter=sink)
    from promptflow.tracing import start_trace

    start_trace()  # devkit is installed in the matrix, but no local service is started.
    assert len(provider._active_span_processor._span_processors) == 1
    flow(PRIVATE)
    assert len(sink.get_finished_spans()) == 2


def test_reject_competing_openai_wrapper(monkeypatch, sink):
    monkeypatch.setitem(
        sys.modules,
        "traceai_openai",
        SimpleNamespace(
            OpenAIInstrumentor=lambda: SimpleNamespace(
                is_instrumented_by_opentelemetry=True
            )
        ),
    )
    with pytest.raises(RuntimeError, match="Remove OpenAIInstrumentor"):
        configure(project_name="promptflow-test", span_exporter=sink)
    assert isinstance(otel.get_tracer_provider(), otel.ProxyTracerProvider)
    assert "PF_DISABLE_TRACING" not in os.environ


def test_reject_unsupported_version(monkeypatch, sink):
    monkeypatch.setattr("fi_instrumentation.promptflow.version", lambda _: "2.0.0")
    with pytest.raises(ValueError, match="1.18.x"):
        configure(project_name="promptflow-test", span_exporter=sink)
    assert isinstance(otel.get_tracer_provider(), otel.ProxyTracerProvider)


@pytest.mark.parametrize("capture", [False, True])
def test_content_opt_in(capture, sink):
    configure(
        project_name="promptflow-test",
        span_exporter=sink,
        batch=False,
        capture_content=capture,
        config=TraceConfig(hide_inputs=False, hide_outputs=False),
    )
    flow(PRIVATE)
    assert (PRIVATE.encode() in wire(sink)) is capture
    for span in sink.get_finished_spans():
        assert "inputs" not in span.attributes and "output" not in span.attributes
        if capture:
            assert json.loads(span.attributes["input.value"]) == {"value": PRIVATE}
            assert span.attributes["output.mime_type"] == "application/json"


@pytest.mark.parametrize(
    "flag",
    [
        "hide_inputs",
        "hide_input_messages",
        "hide_input_text",
        "hide_input_images",
        "hide_llm_invocation_parameters",
    ],
)
def test_input_hide_flags_drop_serialized_payload(flag, sink):
    configure(
        project_name="promptflow-test",
        span_exporter=sink,
        batch=False,
        capture_content=True,
        config=TraceConfig(**{flag: True}, hide_outputs=True),
    )
    flow(PRIVATE)
    assert PRIVATE.encode() not in wire(sink)


@pytest.mark.parametrize(
    "flag", ["hide_outputs", "hide_output_messages", "hide_output_text"]
)
def test_output_hide_flags_drop_serialized_payload(flag, sink):
    configure(
        project_name="promptflow-test",
        span_exporter=sink,
        batch=False,
        capture_content=True,
        config=TraceConfig(**{flag: True}, hide_inputs=True),
    )
    flow(PRIVATE)
    assert PRIVATE.encode() not in wire(sink)


def test_environment_privacy_and_errors(monkeypatch, sink):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    configure(
        project_name="promptflow-test",
        span_exporter=sink,
        batch=False,
        capture_content=True,
    )

    @trace
    def fails(value):
        raise ValueError(value)

    with pytest.raises(ValueError, match=PRIVATE):
        fails(PRIVATE)
    (span,) = sink.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None
    assert span.attributes["error.type"] == "ValueError"
    assert PRIVATE.encode() not in wire(sink)


def test_private_default_error_keeps_status_not_payload(sink):
    configure(project_name="promptflow-test", span_exporter=sink, batch=False)

    @trace
    def fails():
        raise RuntimeError(PRIVATE)

    with pytest.raises(RuntimeError):
        fails()
    (span,) = sink.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "RuntimeError"
    assert PRIVATE.encode() not in wire(sink)


def test_sync_and_async_streams_end_on_exhaustion(sink):
    configure(project_name="promptflow-test", span_exporter=sink, batch=False)

    @trace
    def tokens(value):
        yield value
        yield "!"

    @trace
    def streamed_flow(value):
        return tokens(value)

    stream = streamed_flow(PRIVATE)
    assert sink.get_finished_spans() == ()
    assert list(stream) == [PRIVATE, "!"]
    child, parent = sink.get_finished_spans()
    assert child.parent.span_id == parent.context.span_id
    assert child.end_time <= parent.end_time
    sink.clear()

    @trace
    async def async_node(value):
        return value

    @trace
    async def async_flow(value):
        return await async_node(value)

    @trace
    async def async_tokens(value):
        yield value

    async def consume():
        assert await async_flow(PRIVATE) == PRIVATE
        return [value async for value in async_tokens(PRIVATE)]

    assert asyncio.run(consume()) == [PRIVATE]
    assert len(sink.get_finished_spans()) == 3
    assert PRIVATE.encode() not in wire(sink)


def completion_response(request):
    return httpx.Response(
        200,
        json={
            "id": "offline-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "synthetic-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": PRIVATE},
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        },
    )


def test_native_openai_exactly_once_with_reported_usage(sink):
    configure(project_name="promptflow-test", span_exporter=sink, batch=False)
    with openai.OpenAI(
        api_key="offline-not-a-key",
        http_client=httpx.Client(transport=httpx.MockTransport(completion_response)),
    ) as client:

        @trace
        def answer():
            return (
                client.chat.completions.create(
                    model="synthetic-model",
                    messages=[{"role": "user", "content": PRIVATE}],
                )
                .choices[0]
                .message.content
            )

        assert answer() == PRIVATE
    spans = sink.get_finished_spans()
    assert len(spans) == 2
    llm, parent = spans
    assert llm.attributes["fi.span.kind"] == "LLM"
    assert llm.parent.span_id == parent.context.span_id
    assert llm.attributes["llm.model_name"] == "synthetic-model"
    assert llm.attributes["llm.token_count.prompt"] == 2
    assert llm.attributes["llm.token_count.completion"] == 3
    assert llm.attributes["llm.token_count.total"] == 5
    assert not any("token_count" in key for key in parent.attributes)
    assert PRIVATE.encode() not in wire(sink)


def test_native_openai_stream_preserves_delivery(monkeypatch, sink):
    # Prompt Flow estimates streaming usage itself. Stub only its tokenizer so
    # this test does not download encoding data; do not assert estimated counts.
    from promptflow.tracing._openai_utils import OpenAIMetricsCalculator

    monkeypatch.setattr(
        OpenAIMetricsCalculator, "get_openai_metrics_for_chat_api", lambda *args: {}
    )
    configure(project_name="promptflow-test", span_exporter=sink, batch=False)
    chunk = {
        "id": "offline-stream",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "synthetic-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": None,
                "delta": {"role": "assistant", "content": PRIVATE},
            }
        ],
    }
    body = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"

    def response(request):
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    with openai.OpenAI(
        api_key="offline-not-a-key",
        http_client=httpx.Client(transport=httpx.MockTransport(response)),
    ) as client:
        stream = client.chat.completions.create(
            model="synthetic-model",
            stream=True,
            messages=[{"role": "user", "content": PRIVATE}],
        )
        assert sink.get_finished_spans() == ()
        assert [part.choices[0].delta.content for part in stream] == [PRIVATE]
    (span,) = sink.get_finished_spans()
    assert span.attributes["fi.span.kind"] == "LLM"
    assert span.attributes["llm.model_name"] == "synthetic-model"
    assert PRIVATE.encode() not in wire(sink)


def test_unrelated_spans_are_unchanged_and_native_not_mutated(sink):
    raw = InMemorySpanExporter()
    adapter = PromptFlowSpanExporter(sink)
    provider = TracerProvider(resource=Resource({"project_name": "promptflow-test"}))
    provider.add_span_processor(SimpleSpanProcessor(raw))
    provider.add_span_processor(SimpleSpanProcessor(adapter))
    with provider.get_tracer("promptflow").start_as_current_span("flow") as span:
        span.set_attributes({"span_type": "Flow", "inputs": PRIVATE, "custom": PRIVATE})
        span.add_event(PRIVATE, {"payload": PRIVATE})
        span.set_status(Status(StatusCode.ERROR, PRIVATE))
    with provider.get_tracer("other").start_as_current_span("other"):
        pass
    original, unrelated = raw.get_finished_spans()
    converted, untouched = sink.get_finished_spans()
    assert original.attributes["inputs"] == PRIVATE
    assert converted.context == original.context
    assert converted.start_time == original.start_time
    assert untouched is unrelated
    assert PRIVATE.encode() not in encode_spans([converted]).SerializeToString()
    provider.shutdown()


def test_real_flow_and_node_topology(tmp_path, sink):
    from promptflow.core import Flow

    configure(project_name="promptflow-test", span_exporter=sink, batch=False)
    (tmp_path / "node.py").write_text(
        "from promptflow.core import tool\n"
        "@tool\ndef echo(value: str) -> str:\n    return 'reply:' + value\n"
    )
    (tmp_path / "flow.dag.yaml").write_text(
        "inputs:\n  value:\n    type: string\n"
        "outputs:\n  answer:\n    type: string\n    reference: ${echo.output}\n"
        "nodes:\n- name: echo\n  type: python\n"
        "  source:\n    type: code\n    path: node.py\n"
        "  inputs:\n    value: ${inputs.value}\n"
    )
    loaded = Flow.load(tmp_path / "flow.dag.yaml")
    assert loaded(value=PRIVATE) == {"answer": "reply:" + PRIVATE}
    spans = sink.get_finished_spans()
    assert len(spans) == 2
    child, parent = spans
    assert child.name == "echo"
    assert child.parent.span_id == parent.context.span_id
    assert (
        child.attributes["fi.span.kind"] == parent.attributes["fi.span.kind"] == "CHAIN"
    )
    assert PRIVATE.encode() not in wire(sink)


def test_http_wire_headers_resource_and_private_payload(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["content-length"]))
            received.append((self.path, self.headers, payload))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FI_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("FI_API_KEY", "offline-api-placeholder")
    monkeypatch.setenv("FI_SECRET_KEY", "offline-secret-placeholder")
    try:
        provider = configure(project_name="promptflow-wire", batch=True)
        flow(PRIVATE)
        assert provider.force_flush()
        assert len(received) == 1  # One exporter and one batch, not a second raw route.
        path, headers, payload = received[0]
        assert path == "/tracer/v1/traces"
        assert headers["X-Api-Key"] == "offline-api-placeholder"
        assert headers["X-Secret-Key"] == "offline-secret-placeholder"
        assert headers["Content-Type"] == "application/x-protobuf"
        assert PRIVATE.encode() not in payload
        request = ExportTraceServiceRequest.FromString(payload)
        (resource,) = request.resource_spans
        attributes = {a.key: a.value.string_value for a in resource.resource.attributes}
        assert attributes["project_name"] == "promptflow-wire"
        assert attributes["project_type"] == "observe"
        spans = [s for scope in resource.scope_spans for s in scope.spans]
        assert len(spans) == 2
        child, parent = spans
        assert child.parent_span_id == parent.span_id
        assert child.trace_id == parent.trace_id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_exporter_lifecycle_result():
    target = Mock()
    target.export.return_value = SpanExportResult.FAILURE
    target.force_flush.return_value = True
    adapter = PromptFlowSpanExporter(target)
    assert adapter.export([]) == SpanExportResult.FAILURE
    assert adapter.force_flush(123)
    target.force_flush.assert_called_once_with(timeout_millis=123)
    adapter.shutdown()
    target.shutdown.assert_called_once_with()
