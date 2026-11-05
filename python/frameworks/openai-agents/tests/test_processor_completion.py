"""Offline end-of-lifecycle contracts against the real Agents SDK."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import Mock

import agents.tracing as tracing
import pytest
from agents import set_trace_processors
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as otel
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from traceai_openai_agents import OpenAIAgentsInstrumentor
from traceai_openai_agents._processor import FiTracingProcessor


def attributes(exporter, name):
    return next(s.attributes for s in exporter.get_finished_spans() if s.name == name)


def test_trace_group_metadata_and_generation_completion(
    instrument, in_memory_span_exporter
):
    with tracing.trace(
        "workflow", group_id="conversation-1", metadata={"tenant": "synthetic"}
    ):
        with tracing.agent_span("assistant"):
            with tracing.generation_span(
                input=[{"role": "user", "content": "first"}]
            ) as first:
                first.span_data.output = [
                    {"role": "assistant", "content": "intermediate"}
                ]
            with tracing.generation_span(
                input=[{"role": "user", "content": "second"}]
            ) as last:
                last.span_data.output = [{"role": "assistant", "content": "final"}]
                last.span_data.usage = {"input_tokens": 0, "output_tokens": 2}
    root = attributes(in_memory_span_exporter, "workflow")
    assert root["session.id"] == "conversation-1"
    assert json.loads(root["metadata"]) == {"tenant": "synthetic"}
    for name in ("workflow", "assistant"):
        data = attributes(in_memory_span_exporter, name)
        assert json.loads(data["input.value"])[0]["content"] == "first"
        assert json.loads(data["output.value"])[0]["content"] == "final"
    generations = [
        s
        for s in in_memory_span_exporter.get_finished_spans()
        if s.name == "generation"
    ]
    assert generations[-1].attributes["gen_ai.usage.input_tokens"] == 0


@pytest.mark.parametrize("triggered", [True, False])
def test_guardrail_final_outcome(instrument, in_memory_span_exporter, triggered):
    with tracing.trace("workflow"):
        with tracing.guardrail_span("policy") as span:
            span.span_data.triggered = triggered
    assert (
        attributes(in_memory_span_exporter, "policy")["guardrail.triggered"]
        is triggered
    )


# These span types do not exist at the declared SDK floor; exercise all that do.
@pytest.mark.parametrize(
    "kind", [k for k in ("task", "turn") if hasattr(tracing, k + "_span")] or [None]
)
def test_task_turn_final_usage_and_metadata(instrument, in_memory_span_exporter, kind):
    if kind is None:
        # Historical SDKs have neither type; all other lifecycle contracts still run.
        assert not hasattr(tracing, "task_span") and not hasattr(tracing, "turn_span")
        return
    factory = getattr(tracing, kind + "_span")
    args = {"name": "run"} if kind == "task" else {"turn": 2, "agent_name": "assistant"}
    with tracing.trace("workflow"):
        with factory(**args) as span:
            span.span_data.usage = {
                "input_tokens": 0,
                "output_tokens": 3,
                "total_tokens": 3,
            }
            span.span_data.metadata = {"phase": "complete"}
    data = attributes(in_memory_span_exporter, "run" if kind == "task" else "turn")
    assert data["gen_ai.usage.input_tokens"] == 0
    assert data["gen_ai.usage.output_tokens"] == 3
    assert data["gen_ai.usage.total_tokens"] == 3
    assert json.loads(data["metadata"]) == {"phase": "complete"}
    if kind == "turn":
        assert data["openai.agents.turn"] == 2
        assert data["openai.agents.agent_name"] == "assistant"


@pytest.mark.parametrize("kind", ["transcription", "speech", "speech_group"])
def test_audio_completion_without_binary_payload(
    instrument, in_memory_span_exporter, kind
):
    with tracing.trace("workflow"):
        with getattr(tracing, kind + "_span")() as span:
            if kind == "transcription":
                span.span_data.input = "private-audio-bytes"
                span.span_data.output = "recognized text"
            elif kind == "speech":
                span.span_data.input = "spoken text"
                span.span_data.output = "private-audio-bytes"
            else:
                span.span_data.input = "group text"
    data = attributes(in_memory_span_exporter, kind)
    assert "private-audio-bytes" not in json.dumps(dict(data))
    if kind == "transcription":
        assert data["output.value"] == "recognized text"
    else:
        assert data["input.value"] == (
            "spoken text" if kind == "speech" else "group text"
        )


def test_processor_flushes_but_does_not_shut_down_shared_provider(tracer_provider):
    provider = Mock(wraps=tracer_provider)
    processor = FiTracingProcessor(
        tracer_provider.get_tracer("test"), tracer_provider=provider
    )
    processor.force_flush()
    processor.shutdown()
    assert provider.force_flush.call_count == 2
    provider.shutdown.assert_not_called()
    # The original one-argument entry point remains usable without provider ownership.
    legacy = FiTracingProcessor(tracer_provider.get_tracer("legacy"))
    legacy.force_flush()
    legacy.shutdown()


def test_instrumentor_wires_provider_flush(tracer_provider, monkeypatch):
    provider = Mock(wraps=tracer_provider)
    install = Mock(wraps=set_trace_processors)
    monkeypatch.setattr("agents.set_trace_processors", install)
    instrumentor = OpenAIAgentsInstrumentor()
    try:
        instrumentor.instrument(tracer_provider=provider)
        processor = install.call_args.args[0][0]
        processor.force_flush()
        provider.force_flush.assert_called_once()
    finally:
        set_trace_processors([])
        instrumentor.uninstrument()


def test_empty_generation_io_is_preserved(instrument, in_memory_span_exporter):
    with tracing.trace("empty"):
        with tracing.generation_span(input=[], output=[]):
            pass
    root = attributes(in_memory_span_exporter, "empty")
    assert json.loads(root["input.value"]) == []
    assert json.loads(root["output.value"]) == []


def test_concurrent_traces_keep_io_and_context_separate(
    instrument, in_memory_span_exporter
):
    async def run(name):
        with tracing.trace(name):
            with tracing.agent_span(name + " agent"):
                with tracing.generation_span(
                    input=[{"role": "user", "content": name}]
                ) as span:
                    await asyncio.sleep(0)
                    span.span_data.output = [
                        {"role": "assistant", "content": name + " result"}
                    ]
        assert not otel.get_current_span().get_span_context().is_valid

    async def both():
        await asyncio.gather(run("one"), run("two"))

    asyncio.run(both())
    for name in ("one", "two"):
        data = attributes(in_memory_span_exporter, name)
        assert json.loads(data["input.value"])[0]["content"] == name
        assert json.loads(data["output.value"])[0]["content"] == name + " result"


def test_handoff_and_custom_completion(instrument, in_memory_span_exporter):
    with tracing.trace("workflow"):
        with tracing.agent_span("first"):
            with tracing.handoff_span(from_agent="first", to_agent="second"):
                pass
        with tracing.agent_span("second"):
            with tracing.custom_span("custom", data={"phase": "start"}) as span:
                span.span_data.data["phase"] = "end"
    data = attributes(in_memory_span_exporter, "custom")
    assert json.loads(data["output.value"]) == {"phase": "end"}
    handoff = attributes(in_memory_span_exporter, "handoff to second")
    assert handoff["openai.agents.handoff.to_agent"] == "second"


def test_final_empty_output_does_not_retain_previous_messages(
    instrument, in_memory_span_exporter
):
    with tracing.trace("workflow"):
        with tracing.agent_span("assistant"):
            with tracing.generation_span(
                input=[], output=[{"role": "assistant", "content": "old"}]
            ):
                pass
            with tracing.generation_span(input=[], output=[]):
                pass
    for name in ("workflow", "assistant"):
        data = attributes(in_memory_span_exporter, name)
        assert json.loads(data["output.value"]) == []
        assert not any(key.startswith("gen_ai.output.messages") for key in data)


def test_malformed_diagnostics_do_not_break_run_or_leave_span_open(
    instrument, in_memory_span_exporter, caplog
):
    with tracing.trace("workflow"):
        with tracing.generation_span() as span:
            span.span_data.output = [None]  # A misbehaving third-party Model.
    assert len(in_memory_span_exporter.get_finished_spans()) == 2
    assert all(
        s.end_time is not None for s in in_memory_span_exporter.get_finished_spans()
    )
    assert "Could not map OpenAI Agents span attributes" in caplog.text
    assert not otel.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("exclusive", [True, False])
def test_processor_replacement_is_explicit(tracer_provider, exclusive):
    other = Mock()
    set_trace_processors([other])
    instrumentor = OpenAIAgentsInstrumentor()
    try:
        instrumentor.instrument(
            tracer_provider=tracer_provider, exclusive_processor=exclusive
        )
        with tracing.trace("workflow"):
            pass
        assert other.on_trace_start.call_count == (0 if exclusive else 1)
    finally:
        set_trace_processors([])
        instrumentor.uninstrument()


def test_response_custom_tool_completion(instrument, in_memory_span_exporter):
    from openai.types.responses import Response

    response = Response(
        id="resp_offline",
        created_at=0,
        model="offline-model",
        object="response",
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        output=[
            {
                "id": "tool_1",
                "type": "custom_tool_call",
                "call_id": "call_1",
                "name": "lookup",
                "input": "query",
            }
        ],
    )
    with tracing.trace("workflow"):
        with tracing.response_span(response=response):
            pass
    data = attributes(in_memory_span_exporter, "response")
    assert (
        data["gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"]
        == "lookup"
    )
    assert (
        data["gen_ai.output.messages.0.message.tool_calls.0.tool_call.id"] == "call_1"
    )


@pytest.mark.parametrize(
    "hide_inputs,hide_outputs",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_otlp_wire_completion_and_privacy(hide_inputs, hide_outputs):
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = TracerProvider()
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                headers={},
            )
        )
    )
    tracer = FITracer(
        provider.get_tracer("wire"),
        config=TraceConfig(hide_inputs=hide_inputs, hide_outputs=hide_outputs),
    )
    processor = FiTracingProcessor(tracer, tracer_provider=provider)
    set_trace_processors([processor])
    try:
        with tracing.trace("wire", group_id="session"):
            with tracing.generation_span(
                input=[{"role": "user", "content": "private-input"}]
            ) as gen:
                gen.span_data.output = [
                    {"role": "assistant", "content": "private-output"}
                ]
            with tracing.guardrail_span("guard") as guard:
                guard.span_data.triggered = True
            with tracing.transcription_span(input="private-audio") as audio:
                audio.span_data.output = "private-transcript"
        processor.force_flush()
        assert payloads
        request = ExportTraceServiceRequest.FromString(payloads[0])
        spans = [
            s
            for r in request.resource_spans
            for scope in r.scope_spans
            for s in scope.spans
        ]
        assert len(spans) == 4
        root = next(s for s in spans if s.name == "wire")
        assert all(s.parent_span_id == root.span_id for s in spans if s is not root)
        wire = b"".join(payloads)
        assert b"guardrail.triggered" in wire
        assert b"session.id" in wire
        assert b"private-audio" not in wire
        assert (b"private-input" in wire) is not hide_inputs
        for value in (b"private-output", b"private-transcript"):
            assert (value in wire) is not hide_outputs
    finally:
        set_trace_processors([])
        provider.shutdown()
        server.shutdown()
        thread.join()
        server.server_close()
