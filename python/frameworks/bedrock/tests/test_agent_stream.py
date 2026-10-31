"""Offline agent streaming contracts, through the public boto3 instrumentor."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import boto3
import pytest
import requests
from botocore.client import BaseClient
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from traceai_bedrock import BedrockInstrumentor
from traceai_bedrock._response_accumulator import _ResponseAccumulator


class SyntheticStream:
    def __init__(self, events, error=None, close_error=None):
        self.events = events
        self.error = error
        self.close_error = close_error
        self.reads = 0
        self.closed = False
        self.contexts = []

    def __iter__(self):
        for event in self.events:
            self.reads += 1
            self.contexts.append(trace.get_current_span().get_span_context())
            yield event
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error
        return "closed"


def chunks(*parts):
    return [{"chunk": {"bytes": part}} for part in parts]


@pytest.fixture
def instrumented(monkeypatch):
    # Never consult a developer's AWS config, credential files or metadata service.
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    clients = []

    def invoke(raw, *, config=None, request_error=None):
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(
            tracer_provider=provider, config=config or TraceConfig()
        )
        client = boto3.session.Session().client(
            "bedrock-agent-runtime",
            region_name="us-east-1",
            aws_access_key_id="synthetic",
            aws_secret_access_key="synthetic",
            endpoint_url="https://offline.invalid",
        )
        clients.append(client)

        def api_call(self, operation_name, parameters):
            assert operation_name == "InvokeAgent"
            assert parameters["enableTrace"] is True
            if request_error is not None:
                raise request_error
            return {"completion": raw, "sessionId": "synthetic-session"}

        monkeypatch.setattr(BaseClient, "_make_api_call", api_call)
        return client.invoke_agent(
            inputText="private prompt",
            agentId="synthetic-agent",
            agentAliasId="synthetic-alias",
            sessionId="synthetic-session",
            enableTrace=True,
        )["completion"]

    yield invoke, provider, exporter
    BedrockInstrumentor().uninstrument()
    for client in clients:
        client.close()
    provider.shutdown()


@pytest.mark.parametrize(
    "parts",
    [
        [b"Hello ", b"world"],
        [b"\xe2", b"\x82", b"\xac"],
        [b"a", b"\xf0\x9f", b"\x8c\x8d", b"b"],
        [b""],
        [b"invalid \xff"],
    ],
)
def test_complete_stream_preserves_bytes_and_accumulates_output(instrumented, parts):
    invoke, _, exporter = instrumented
    raw = SyntheticStream(chunks(*parts))
    stream = invoke(raw)
    assert raw.reads == 0
    assert not exporter.get_finished_spans()
    events = list(stream)
    assert all(actual is expected for actual, expected in zip(events, raw.events))
    assert b"".join(event["chunk"]["bytes"] for event in events) == b"".join(parts)
    (span,) = exporter.get_finished_spans()
    assert span.attributes[SpanAttributes.OUTPUT_VALUE] == b"".join(parts).decode(
        "utf-8", "replace"
    )
    assert span.status.status_code == StatusCode.OK
    assert span.attributes[SpanAttributes.GEN_AI_SPAN_KIND] == "AGENT"
    assert span.attributes[SpanAttributes.GEN_AI_PROVIDER_NAME] == "aws"
    assert all(ctx.span_id == span.context.span_id for ctx in raw.contexts)
    assert not trace.get_current_span().get_span_context().is_valid
    stream.close()
    assert len(exporter.get_finished_spans()) == 1


@pytest.mark.parametrize(
    "parts, consumed, expected",
    [
        ([b"Hello ", b"world"], 1, "Hello "),
        ([b"\xe2", b"\x82\xac"], 1, "�"),
        ([b"Hello"], 0, None),
    ],
)
def test_close_finishes_once_without_draining(instrumented, parts, consumed, expected):
    invoke, _, exporter = instrumented
    raw = SyntheticStream(chunks(*parts))
    stream = invoke(raw)
    iterator = iter(stream)
    for _ in range(consumed):
        next(iterator)
    assert not exporter.get_finished_spans()
    assert stream.close() == "closed"
    stream.close()
    assert raw.closed
    assert raw.reads == consumed
    (span,) = exporter.get_finished_spans()
    assert span.attributes.get(SpanAttributes.OUTPUT_VALUE) == expected
    assert span.status.status_code == StatusCode.OK


@pytest.mark.parametrize("where", ["iterate", "close", "request"])
def test_original_errors_propagate_and_finish_once(instrumented, where):
    invoke, _, exporter = instrumented
    error = RuntimeError("synthetic stream failure")
    raw = SyntheticStream(
        chunks(b"Hello ", b"world"),
        error=error if where == "iterate" else None,
        close_error=error if where == "close" else None,
    )
    with pytest.raises(RuntimeError) as caught:
        stream = invoke(raw, request_error=error if where == "request" else None)
        if where == "iterate":
            list(stream)
        else:
            next(iter(stream))
            stream.close()
    assert caught.value is error
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert len(span.events) == 1
    expected = {"iterate": "Hello world", "close": "Hello ", "request": None}[where]
    assert span.attributes.get(SpanAttributes.OUTPUT_VALUE) == expected
    if where == "iterate":
        stream.close()
        assert len(exporter.get_finished_spans()) == 1


def test_empty_stream(instrumented):
    invoke, _, exporter = instrumented
    assert list(invoke(SyntheticStream([]))) == []
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK
    assert SpanAttributes.OUTPUT_VALUE not in span.attributes


def test_trace_children_are_not_duplicated_at_close(instrumented):
    invoke, provider, exporter = instrumented
    event = {
        "trace": {
            "trace": {
                "orchestrationTrace": {
                    "modelInvocationInput": {
                        "traceId": "synthetic-trace-1",
                        "foundationModel": "synthetic-model",
                        "text": "private prompt",
                        "type": "ORCHESTRATION",
                    }
                }
            }
        }
    }
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        stream = invoke(SyntheticStream([event, *chunks(b"Hello ", b"world")]))
        assert list(stream)[0] is event
        spans = exporter.get_finished_spans()
        agent = next(
            span for span in spans if span.name == "bedrock_agent.invoke_agent"
        )
        assert len(spans) == 3  # agent, orchestration chain, LLM
        assert agent.parent.span_id == parent.get_span_context().span_id
        assert all(span.context.trace_id == agent.context.trace_id for span in spans)
        stream.close()
        stream.close()
        assert len(exporter.get_finished_spans()) == 3


def test_telemetry_failure_does_not_drop_output_or_leak_parent(
    instrumented, monkeypatch
):
    invoke, _, exporter = instrumented

    def broken_traces(self):
        raise ValueError("synthetic malformed trace")

    monkeypatch.setattr(_ResponseAccumulator, "_process_traces", broken_traces)
    stream = invoke(SyntheticStream(chunks(b"Hello ", b"world")))
    assert len(list(stream)) == 2
    stream.close()
    (span,) = exporter.get_finished_spans()
    assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "Hello world"
    # A telemetry conversion failure is not an AWS request failure.
    assert span.status.status_code == StatusCode.OK


def test_suppressed_instrumentation_returns_original_stream(instrumented):
    invoke, _, exporter = instrumented
    raw = SyntheticStream(chunks(b"Hello"))
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert invoke(raw) is raw
        assert list(raw) == raw.events
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("outcome", ["complete", "close", "error"])
def test_otlp_wire_output_and_privacy(instrumented, hidden, outcome):
    invoke, provider, _ = instrumented
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                session=session,
                timeout=3,
            )
        )
    )
    try:
        raw = SyntheticStream(
            chunks(b"private ", b"answer"),
            error=RuntimeError("synthetic failure") if outcome == "error" else None,
        )
        stream = invoke(
            raw, config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden)
        )
        if outcome == "close":
            next(iter(stream))
            stream.close()
        elif outcome == "error":
            with pytest.raises(RuntimeError, match="synthetic failure"):
                list(stream)
        else:
            list(stream)
        provider.force_flush()
        assert len(payloads) == 1
        request = ExportTraceServiceRequest.FromString(payloads[0])
        spans = [
            span
            for resource in request.resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]
        assert len(spans) == 1
        attrs = {attr.key: attr.value.string_value for attr in spans[0].attributes}
        assert "inputText" not in attrs  # Raw kwargs must not bypass TraceConfig.
        if hidden:
            assert b"private prompt" not in payloads[0]
            assert b"private answer" not in payloads[0]
            assert b"private " not in payloads[0]
        else:
            assert attrs[SpanAttributes.INPUT_VALUE] == "private prompt"
            expected = "private " if outcome == "close" else "private answer"
            assert attrs[SpanAttributes.OUTPUT_VALUE] == expected
        assert spans[0].status.code == (2 if outcome == "error" else 1)
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
