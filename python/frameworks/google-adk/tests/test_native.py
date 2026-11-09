"""Real ADK agents and OTLP export, using only a scripted in-process model."""

import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import ModuleType

import pytest
from google.adk import telemetry
from google.adk.agents import BaseAgent, LlmAgent, SequentialAgent, base_agent
from google.adk.events import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner, Runner
from google.genai import types
from opentelemetry import context
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
from traceai_google_adk import GoogleADKInstrumentor, _bindings

SECRET = "synthetic-private-content-6842"


@pytest.fixture
def tracing(monkeypatch):
    # Test explicit native content opt-in, so adapter privacy cannot pass merely
    # because upstream happened not to emit any content.
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY"
    )
    monkeypatch.setenv("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "true")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = GoogleADKInstrumentor()
    yield instrumentor, provider, exporter
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


async def run_agent(parallel=False, error=None, workflow=False):
    requests, calls = [], []

    class Model(BaseLlm):
        turn: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            requests.append(llm_request)
            self.turn += 1
            if self.turn == 1:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    id=f"call-{n}",
                                    name="compute",
                                    args={"value": n, "text": SECRET},
                                )
                            )
                            for n in ([1, 2] if parallel else [1])
                        ],
                    )
                )
            elif self.turn == 2:
                if error == "model":
                    raise RuntimeError(SECRET)
                yield LlmResponse(
                    content=types.Content(
                        role="model", parts=[types.Part(text=SECRET)]
                    ),
                    usage_metadata=types.GenerateContentResponseUsageMetadata(
                        prompt_token_count=3,
                        candidates_token_count=2,
                        total_token_count=5,
                    ),
                )
            else:
                raise AssertionError("unexpected model retry")

    def compute(value: int, text: str) -> dict:
        """Return an offline result."""
        calls.append(value)
        if error == "tool":
            raise ValueError(SECRET)
        return {"value": value + 1, "text": text}

    child = LlmAgent(name="child", model=Model(model="offline-model"), tools=[compute])
    agent = SequentialAgent(name="workflow", sub_agents=[child]) if workflow else child
    runner = InMemoryRunner(app_name="native_test", agent=agent)
    try:
        await runner.session_service.create_session(
            app_name="native_test", user_id="test-user", session_id="test-session"
        )
        stream = runner.run_async(
            user_id="test-user",
            session_id="test-session",
            new_message=types.Content(role="user", parts=[types.Part(text=SECRET)]),
        )
        if error:
            with pytest.raises((RuntimeError, ValueError), match=SECRET):
                events = [event async for event in stream]
        else:
            events = [event async for event in stream]
            assert events[-1].content.parts[0].text == SECRET
            assert len(requests) == 2
            assert sorted(calls) == ([1, 2] if parallel else [1])
        # ADK 1.2.1 schedules nested generator finalization asynchronously.
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        if hasattr(runner, "close"):
            await runner.close()
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("error", [None, "model", "tool"])
@pytest.mark.parametrize("capture", [False, True])
async def test_real_model_tools_workflow(tracing, parallel, error, capture):
    instrumentor, provider, exporter = tracing
    methods = Runner.run_async, BaseAgent.run_async
    config = TraceConfig() if capture else None
    instrumentor.instrument(tracer_provider=provider, config=config)
    assert (Runner.run_async, BaseAgent.run_async) == methods
    calls = await run_agent(parallel, error, workflow=True)
    spans = exporter.get_finished_spans()
    assert spans
    assert all(s.attributes.get("gen_ai.span.kind") for s in spans)
    assert len({s.context.trace_id for s in spans}) == 1
    ids = {s.context.span_id for s in spans}
    assert all(not s.parent or s.parent.span_id in ids for s in spans)
    assert sum(s.attributes.get("gen_ai.span.kind") == "AGENT" for s in spans) == 2
    tools = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "TOOL"]
    # ADK 1.2.1 stops before a second tool on failure; current ADK schedules both.
    assert len(tools) == len(calls)
    assert all(s.attributes.get("gen_ai.tool.name") == "compute" for s in tools)
    llms = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert len(llms) == (1 if error == "tool" else 2)
    if not error:
        assert sum(s.attributes.get("gen_ai.usage.input_tokens", 0) for s in spans) == 3
        assert (
            sum(s.attributes.get("gen_ai.usage.output_tokens", 0) for s in spans) == 2
        )
        assert any(
            s.attributes.get("gen_ai.request.model") == "offline-model" for s in llms
        )
    else:
        assert any(s.status.status_code == StatusCode.ERROR for s in spans)
    serialized = str(
        [(s.name, dict(s.attributes), s.events, s.status.description) for s in spans]
    )
    assert (SECRET in serialized) is capture
    assert "gcp.vertex.agent.llm_request" not in serialized
    assert "gcp.vertex.agent.tool_call_args" not in serialized


@pytest.mark.asyncio
async def test_native_span_tree_is_not_duplicated(tracing, monkeypatch):
    instrumentor, provider, exporter = tracing
    original = telemetry.tracer
    bindings = _bindings(original)
    raw = provider.get_tracer("native-control")
    for module, name in bindings:
        monkeypatch.setattr(module, name, raw)
    agent_original = vars(base_agent).get("tracer")
    if agent_original is not None:
        monkeypatch.setattr(base_agent, "tracer", raw)
    await run_agent(parallel=True, workflow=True)
    baseline = exporter.get_finished_spans()
    baseline_tree = sorted(
        (
            s.name,
            next(
                (
                    p.name
                    for p in baseline
                    if s.parent and p.context.span_id == s.parent.span_id
                ),
                None,
            ),
        )
        for s in baseline
    )
    for module, name in _bindings(raw):
        monkeypatch.setattr(module, name, original)
    if agent_original is not None:
        monkeypatch.setattr(base_agent, "tracer", agent_original)
    exporter.clear()
    instrumentor.instrument(tracer_provider=provider)
    await run_agent(parallel=True, workflow=True)
    adapted = exporter.get_finished_spans()
    adapted_tree = sorted(
        (
            s.name,
            next(
                (
                    p.name
                    for p in adapted
                    if s.parent and p.context.span_id == s.parent.span_id
                ),
                None,
            ),
        )
        for s in adapted
    )
    assert adapted_tree == baseline_tree


def test_setup_restoration_idempotence_and_late_import(tracing, monkeypatch):
    instrumentor, provider, _ = tracing
    original = telemetry.tracer
    bindings = _bindings(original)
    methods = Runner.run_async, BaseAgent.run_async
    instrumentor.instrument(tracer_provider=provider)
    adapted = telemetry.tracer
    instrumentor.instrument(tracer_provider=provider)
    assert telemetry.tracer is adapted
    late_module = ModuleType("google.adk._test_late_import")
    late_module.tracer = adapted
    monkeypatch.setitem(sys.modules, late_module.__name__, late_module)
    instrumentor.uninstrument()
    assert (Runner.run_async, BaseAgent.run_async) == methods
    assert all(vars(module)[name] is original for module, name in bindings)
    assert late_module.tracer is original
    with provider.get_tracer("still-owned-by-caller").start_as_current_span(
        "alive"
    ) as span:
        assert span.is_recording()


def test_setup_rolls_back_every_binding(tracing, monkeypatch):
    instrumentor, provider, _ = tracing
    original = telemetry.tracer
    bindings = _bindings(original)

    class FailingModule(ModuleType):
        def __setattr__(self, name, value):
            if name == "tracer" and value is not original:
                raise RuntimeError("synthetic assignment failure")
            super().__setattr__(name, value)

    target = FailingModule("google.adk._test_failure")
    target.tracer = original
    monkeypatch.setitem(sys.modules, target.__name__, target)
    with pytest.raises(RuntimeError, match="synthetic assignment failure"):
        instrumentor.instrument(tracer_provider=provider)
    assert not instrumentor.is_instrumented_by_opentelemetry
    assert all(vars(module)[name] is original for module, name in bindings)
    assert target.tracer is original


def test_rejects_competing_instrumentor_before_mutation(tracing, monkeypatch):
    instrumentor, provider, _ = tracing
    original = telemetry.tracer
    monkeypatch.setattr(
        Runner.run_async, "__wrapped__", Runner.run_async, raising=False
    )
    with pytest.raises(RuntimeError, match="other Google ADK instrumentors"):
        instrumentor.instrument(tracer_provider=provider)
    assert telemetry.tracer is original
    assert not instrumentor.is_instrumented_by_opentelemetry


@pytest.mark.asyncio
async def test_suppressed_invocation(tracing):
    instrumentor, provider, exporter = tracing
    instrumentor.instrument(tracer_provider=provider)
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        await run_agent()
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()


@pytest.mark.asyncio
async def test_direct_iteration_and_close_keep_native_methods(tracing):
    instrumentor, provider, exporter = tracing
    methods = Runner.run_async, BaseAgent.run_async
    instrumentor.instrument(tracer_provider=provider)

    class StreamingAgent(BaseAgent):
        async def _run_async_impl(self, ctx):
            for index in range(3):
                yield Event(
                    author=self.name,
                    content=types.Content(
                        role="model", parts=[types.Part(text=str(index))]
                    ),
                )

    runner = InMemoryRunner(app_name="stream_test", agent=StreamingAgent(name="stream"))
    await runner.session_service.create_session(
        app_name="stream_test", user_id="u", session_id="s"
    )
    stream = runner.run_async(
        user_id="u",
        session_id="s",
        new_message=types.Content(role="user", parts=[types.Part(text="offline")]),
    )
    try:
        first = await stream.__anext__()
        assert first.content.parts[0].text == "0"
        await stream.aclose()
        for _ in range(5):
            await asyncio.sleep(0)
        assert exporter.get_finished_spans()
        assert (Runner.run_async, BaseAgent.run_async) == methods
    finally:
        if hasattr(runner, "close"):
            await runner.close()


@pytest.mark.parametrize(
    "config",
    [
        None,
        TraceConfig(hide_inputs=True, hide_outputs=True),
        TraceConfig(hide_input_text=True, hide_output_text=True),
        TraceConfig(hide_input_messages=True, hide_output_messages=True),
    ],
)
def test_otlp_wire_privacy(tracing, config):
    instrumentor, provider, _ = tracing
    payloads = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    exporter = OTLPSpanExporter(
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces"
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        instrumentor.instrument(tracer_provider=provider, config=config)
        with telemetry.tracer.start_as_current_span(
            "call_llm",
            attributes={
                "gcp.vertex.agent.llm_request": json.dumps(
                    {
                        "contents": [SECRET],
                        "config": {
                            "http_options": {"headers": {"Authorization": SECRET}}
                        },
                    }
                ),
                "gen_ai.tool.call.arguments": SECRET,
            },
        ) as span:
            span.set_attribute(
                "gcp.vertex.agent.llm_response",
                json.dumps(
                    {
                        "content": {"text": SECRET},
                        "usage_metadata": {"prompt_token_count": 0},
                    }
                ),
            )
            span.set_attributes(
                {
                    "gen_ai.tool.call.result": SECRET,
                    "gen_ai.input.messages": SECRET,
                    "gen_ai.output.messages": SECRET,
                    "adk.experimental.future_payload": SECRET,
                }
            )
            span.add_event(SECRET, {"payload": SECRET})
            span.add_event(
                "exception", {"exception.message": SECRET, "payload": SECRET}
            )
            span.record_exception(ValueError(SECRET), attributes={"payload": SECRET})
            span.set_status(StatusCode.ERROR, SECRET)
        asyncio.run(run_agent(parallel=True, error="model", workflow=True))
        provider.force_flush()
        assert payloads
        assert SECRET.encode() not in b"".join(payloads)
        request = ExportTraceServiceRequest.FromString(payloads[0])
        span = request.resource_spans[0].scope_spans[0].spans[0]
        attrs = {a.key: a.value for a in span.attributes}
        assert attrs["gen_ai.span.kind"].string_value == "LLM"
        assert attrs["gen_ai.usage.input_tokens"].int_value == 0
        assert span.status.code == 2
        assert not span.status.message
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
