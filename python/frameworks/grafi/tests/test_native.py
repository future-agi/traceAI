"""Real grafi runtime with synthetic tools; only loopback OTLP is used."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from grafi.assistants.assistant import Assistant
from grafi.common.decorators.record_decorators import record_tool_invoke
from grafi.common.events.topic_events.publish_to_topic_event import PublishToTopicEvent
from grafi.common.exceptions.workflow_exceptions import NodeExecutionError
from grafi.common.models.invoke_context import InvokeContext
from grafi.common.models.message import Message
from grafi.nodes.node import Node
from grafi.runtime import ExecutionServices, GrafiRuntime, current_services
from grafi.tools.tool import Tool
from grafi.topics.expressions.subscription_builder import SubscriptionBuilder
from grafi.topics.topic_impl.input_topic import InputTopic
from grafi.topics.topic_impl.output_topic import OutputTopic
from grafi.workflows.impl.event_driven_workflow import EventDrivenWorkflow
from openinference.semconv.trace import OpenInferenceSpanKindValues
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_grafi import configure, create_tracer_provider

MARKER = "SYNTHETIC_PRIVATE_CONTENT"


class Echo(Tool):
    name: str = "Echo"
    type: str = "Echo"
    oi_span_type: OpenInferenceSpanKindValues = OpenInferenceSpanKindValues.TOOL
    fail: bool = False

    @record_tool_invoke
    async def invoke(self, context, messages):
        if self.fail:
            raise ValueError(MARKER)
        await asyncio.sleep(0)
        yield [Message(role="assistant", content=MARKER + "-response")]


class EchoAssistant(Assistant):
    name: str = "EchoAssistant"
    type: str = "EchoAssistant"
    fail: bool = False

    def _construct_workflow(self):
        input_topic, output_topic = InputTopic(name="input"), OutputTopic(name="output")
        node = (
            Node.builder()
            .name("EchoNode")
            .type("EchoNode")
            .subscribe(SubscriptionBuilder().subscribed_to(input_topic).build())
            .tool(Echo(fail=self.fail))
            .publish_to(output_topic)
            .build()
        )
        self.workflow = (
            EventDrivenWorkflow.builder().name("EchoWorkflow").node(node).build()
        )
        return self


def request(session="session"):
    return PublishToTopicEvent(
        invoke_context=InvokeContext(
            conversation_id=session,
            invoke_id="invoke",
            assistant_request_id="request",
            user_id="user",
        ),
        data=[Message(role="user", content=MARKER)],
    )


async def run(runtime, session="session", fail=False):
    assistant = EchoAssistant(fail=fail)._construct_workflow()
    return [event async for event in runtime.invoke(assistant, request(session))]


@pytest.fixture
def collector(monkeypatch):
    for key in (
        "FI_CAPTURE_CONTENT",
        "FI_HIDE_ERROR_DETAILS",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_SERVICE_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payload = ExportTraceServiceRequest.FromString(body)
            received.append((self.path, dict(self.headers), body, payload))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    options = {
        "base_url": f"http://127.0.0.1:{server.server_port}",
        "api_key": "synthetic-api",
        "secret_key": "synthetic-secret",
    }
    yield received, options
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def wire_spans(received):
    return [
        s
        for _, _, _, payload in received
        for rs in payload.resource_spans
        for ss in rs.scope_spans
        for s in ss.spans
    ]


def wire_attrs(span):
    return {a.key: a.value.string_value for a in span.attributes}


def test_unadapted_native_export_requires_mapping_and_privacy():
    """Red baseline: routing native spans alone loses semantics and exports content."""
    memory = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}))
    provider.add_span_processor(SimpleSpanProcessor(memory))
    asyncio.run(
        run(GrafiRuntime(ExecutionServices(tracer=provider.get_tracer("grafi"))))
    )
    spans = memory.get_finished_spans()
    assert len(spans) == 4
    assert {s.attributes["oi_span_type"] for s in spans} == {"AGENT", "CHAIN", "TOOL"}
    assert all("fi.span.kind" not in s.attributes for s in spans)
    assert any(MARKER in s.attributes["input"] for s in spans)
    provider.shutdown()


@pytest.mark.parametrize("capture", [False, True])
def test_real_runtime_otlp_mapping_privacy_and_shutdown(
    collector, capture, monkeypatch
):
    received, options = collector
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES", "host.name=PRIVATE_HOST,arbitrary=PRIVATE_RESOURCE"
    )
    global_provider = trace.get_tracer_provider()
    services = ExecutionServices()
    with configure(
        "graphite-test", services=services, capture_content=capture, **options
    ) as handle:
        assert handle.runtime.services.event_store is services.event_store
        assert handle.runtime.services.error_reporter is services.error_reporter
        events = asyncio.run(run(handle.runtime))
        assert events[0].data[0].content == MARKER + "-response"
        assert handle.force_flush(5000)
    assert trace.get_tracer_provider() is global_provider
    with pytest.raises(RuntimeError, match="No ExecutionServices"):
        current_services()
    spans = wire_spans(received)
    assert len(spans) == 4  # assistant, workflow, node, tool: no adapter-created spans
    assert len({s.span_id for s in spans}) == 4
    assert len({s.trace_id for s in spans}) == 1
    roots = [s for s in spans if not s.parent_span_id]
    assert len(roots) == 1
    ids = {s.span_id for s in spans}
    assert all(not s.parent_span_id or s.parent_span_id in ids for s in spans)
    assert {wire_attrs(s)["fi.span.kind"] for s in spans} == {"AGENT", "CHAIN", "TOOL"}
    for span in spans:
        attrs = wire_attrs(span)
        assert attrs["session.id"] == "session" and attrs["user.id"] == "user"
        assert ("input.value" in attrs) is capture
        assert (
            not {"input", "output", "oi_span_type", "conversation_id", "kwargs"}
            & attrs.keys()
        )
    for path, headers, body, payload in received:
        assert path == "/tracer/v1/traces"
        assert {k.lower(): v for k, v in headers.items()}[
            "x-api-key"
        ] == "synthetic-api"
        assert {k.lower(): v for k, v in headers.items()}[
            "x-secret-key"
        ] == "synthetic-secret"
        assert "content-encoding" not in {k.lower() for k in headers}
        assert (MARKER.encode() in body) is capture
        assert b"PRIVATE_HOST" not in body and b"PRIVATE_RESOURCE" not in body
        assert b"synthetic-secret" not in body
        resource = {
            a.key: a.value.string_value
            for a in payload.resource_spans[0].resource.attributes
        }
        assert resource == {
            "project_name": "graphite-test",
            "project_type": "observe",
            "service.name": "graphite",
        }


@pytest.mark.parametrize("capture_errors", [False, True])
def test_native_failure_status_and_stack_fields(collector, capture_errors):
    received, options = collector
    # Preserve Graphite's real workflow-level failure contract.
    with (
        configure(capture_errors=capture_errors, **options) as handle,
        pytest.raises(NodeExecutionError),
    ):
        asyncio.run(run(handle.runtime, fail=True))
    spans = wire_spans(received)
    assert spans and any(span.status.code == 2 for span in spans)
    wire = b"".join(row[2] for row in received)
    assert (MARKER.encode() in wire) is capture_errors
    assert b"error.stack" not in wire and b"error.details" not in wire
    assert b"exception.stacktrace" not in wire and b"Traceback" not in wire
    if not capture_errors:
        assert all(not s.status.message for s in spans)


def test_two_runtimes_do_not_duplicate_or_cross_sessions(collector):
    received, options = collector
    with configure(**options) as a, configure(**options) as b:

        async def go():
            return await asyncio.gather(
                run(a.runtime, "session-a"), run(b.runtime, "session-b")
            )

        asyncio.run(go())
    spans = wire_spans(received)
    assert len(spans) == 8
    traces = {}
    for span in spans:
        traces.setdefault(span.trace_id, set()).add(wire_attrs(span)["session.id"])
    assert sorted(traces.values(), key=str) == [{"session-a"}, {"session-b"}]


def test_existing_tracer_is_not_overwritten(collector):
    _, options = collector
    provider = TracerProvider()
    with pytest.raises(ValueError, match="already has a tracer"):
        configure(
            services=ExecutionServices(tracer=provider.get_tracer("existing")),
            **options,
        )
    provider.shutdown()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com",
        "https://example.com/path",
        "https://user:pass@example.com",
        "https://example.com?q=1",
        "ftp://example.com",
    ],
)
def test_bad_endpoint_rejected(base_url):
    with pytest.raises(ValueError):
        create_tracer_provider(
            base_url=base_url, api_key="synthetic", secret_key="synthetic"
        )


def test_missing_auth_fails_before_provider_creation(monkeypatch):
    monkeypatch.delenv("FI_API_KEY", raising=False)
    monkeypatch.delenv("FI_SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="both FI_API_KEY"):
        create_tracer_provider()


def test_env_privacy_precedence_and_provider_entry_point(collector, monkeypatch):
    received, options = collector
    monkeypatch.setenv("FI_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    provider = create_tracer_provider(
        capture_content=False, capture_errors=True, **options
    )
    with provider.get_tracer("grafi").start_as_current_span("synthetic") as span:
        span.set_attribute("input", MARKER)
        span.record_exception(ValueError(MARKER))
    provider.shutdown()
    assert len(wire_spans(received)) == 1
    assert MARKER.encode() not in received[0][2]


def test_native_payload_disable_overrides_content_opt_in(collector, monkeypatch):
    received, options = collector
    monkeypatch.setenv("FI_CAPTURE_CONTENT", "1")
    monkeypatch.setenv("GRAFI_SPAN_DISABLE_PAYLOADS", "1")
    with configure(**options) as handle:
        asyncio.run(run(handle.runtime))
    assert len(wire_spans(received)) == 4
    assert all(MARKER.encode() not in row[2] for row in received)


def test_export_failure_does_not_break_native_results(collector, monkeypatch):
    import traceai_grafi
    from opentelemetry.sdk.trace.export import SpanExporter

    class FailingExporter(SpanExporter):
        def export(self, spans):
            raise RuntimeError("synthetic exporter unavailable")

    monkeypatch.setattr(
        traceai_grafi, "OTLPSpanExporter", lambda **_kwargs: FailingExporter()
    )
    with configure(**collector[1]) as handle:
        events = asyncio.run(run(handle.runtime))
        handle.force_flush(5000)
    assert events[0].data[0].content == MARKER + "-response"


def test_native_llm_kind_and_early_stream_close(collector):
    from grafi.runtime import bind_services

    received, options = collector
    with configure(**options) as handle:

        async def go():
            with bind_services(handle.runtime.services):
                tool = Echo(oi_span_type=OpenInferenceSpanKindValues.LLM)
                stream = tool.invoke(request().invoke_context, request().data)
                await anext(stream)
                await stream.aclose()

        asyncio.run(go())
    spans = wire_spans(received)
    assert len(spans) == 1
    assert wire_attrs(spans[0])["fi.span.kind"] == "LLM"
    assert spans[0].end_time_unix_nano > spans[0].start_time_unix_nano
