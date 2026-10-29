"""Pinned real Pipecat OpenAILLMService, real frames and real SDK SSE parsing.

Only the HTTP transport is scripted. Neither the processor nor its traced
methods are substituted. The integration maps Pipecat's native spans; it does
not install another set of processor hooks.
"""
from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
from openai import AsyncOpenAI, APIConnectionError
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import LLMTextFrame, StartFrame
from pipecat.observers.base_observer import BaseObserver
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext, OpenAILLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams
from pipecat.utils.tracing.turn_context_provider import TurnContextProvider

from traceai_pipecat.exporters.base_exporter import BaseMappedSpanExporter

INPUT = "PIPECAT-PRIVATE-INPUT-72f9"
OUTPUT = "PIPECAT-PRIVATE-OUTPUT-4b32"


class MappedCapture(BaseMappedSpanExporter, InMemorySpanExporter):
    """Actual mapping/export implementation, with an in-process terminal sink."""


class Frames(BaseObserver):
    def __init__(self):
        super().__init__()
        self.frames = []

    async def on_push_frame(self, data):
        self.frames.append(data.frame)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            assert address[0] in ("localhost", "127.0.0.1", "::1"), address
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    for key in ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT",
                "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_ERROR_DETAILS"):
        monkeypatch.delenv(key, raising=False)


async def operation(monkeypatch, *, error=None, config=None):
    exporter = MappedCapture(config=config)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    frames = Frames()
    manager = TaskManager()
    manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    clock = SystemClock()
    clock.start()
    setup = FrameProcessorSetup(clock=clock, task_manager=manager, observer=frames)
    service = OpenAILLMService(api_key="sk-pipecat-test-not-real", model="gpt-fixture")
    sink = FrameProcessor()
    service.link(sink)
    await service._client.close()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if error is not None:
            raise error
        chunk = {"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 0,
                 "model": "gpt-fixture", "choices": [{"index": 0, "delta": {"content": OUTPUT}, "finish_reason": None}]}
        usage = {**chunk, "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
        body = "".join("data: " + json.dumps(row) + "\n\n" for row in (chunk, usage)) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    service._client = AsyncOpenAI(api_key="sk-pipecat-test-not-real", max_retries=0,
                                 http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await service.setup(setup)
    await sink.setup(setup)
    context = OpenAILLMContext(messages=[{"role": "user", "content": INPUT}])
    caught = None
    try:
        await service.process_frame(StartFrame(enable_tracing=True), FrameDirection.DOWNSTREAM)
        with provider.get_tracer("test").start_as_current_span("turn") as parent:
            TurnContextProvider.get_instance().set_current_turn_context(parent.get_span_context())
            try:
                await service.process_frame(OpenAILLMContextFrame(context), FrameDirection.DOWNSTREAM)
            except Exception as exc:
                caught = exc
            finally:
                TurnContextProvider.get_instance().set_current_turn_context(None)
        return exporter.get_finished_spans(), frames.frames, requests, caught
    finally:
        await service._client.close()
        await service.cleanup()
        await sink.cleanup()
        provider.shutdown()
        assert not manager.current_tasks()
        assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
async def test_actual_frames_span_lifecycle_parentage_and_usage(monkeypatch):
    spans, frames, requests, error = await operation(monkeypatch)
    assert error is None
    assert len(requests) == 1
    assert requests[0]["messages"] == [{"role": "user", "content": INPUT}]
    assert requests[0]["model"] == "gpt-fixture"
    assert requests[0]["stream"] is True
    assert [f.text for f in frames if isinstance(f, LLMTextFrame)] == [OUTPUT]
    assert sorted(s.name for s in spans) == ["llm", "turn"]
    llm = next(s for s in spans if s.name == "llm")
    parent = next(s for s in spans if s.name == "turn")
    assert llm.parent.span_id == parent.context.span_id
    assert llm.context.trace_id == parent.context.trace_id
    assert llm.end_time is not None and parent.end_time is not None
    assert INPUT in llm.attributes["input.value"]
    assert llm.attributes["output.value"] == OUTPUT
    assert llm.attributes["gen_ai.usage.input_tokens"] == 3
    assert llm.attributes["gen_ai.usage.output_tokens"] == 2
    assert llm.attributes["gen_ai.usage.total_tokens"] == 5


@pytest.mark.asyncio
async def test_real_processor_error_is_preserved_and_ends_span(monkeypatch):
    original = ValueError("pipecat transport failure")
    spans, frames, requests, caught = await operation(monkeypatch, error=original)
    assert isinstance(caught, APIConnectionError)
    assert caught.__cause__ is original
    llm, = [s for s in spans if s.name == "llm"]
    assert llm.status.status_code.name == "ERROR"
    assert llm.end_time is not None
    assert any(e.name == "exception" for e in llm.events)
    assert not [f for f in frames if isinstance(f, LLMTextFrame)]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [
    "hide_inputs", "hide_outputs", "hide_input_text", "hide_output_text",
    "hide_input_messages", "hide_output_messages",
])
@pytest.mark.parametrize("via_environment", [False, True], ids=["explicit", "environment"])
async def test_real_processor_error_hides_private_cause_on_legacy_and_source_core(
    monkeypatch, flag, via_environment
):
    from fi_instrumentation import TraceConfig

    if via_environment:
        monkeypatch.setenv("FI_" + flag.upper(), "true")
        config = TraceConfig()
    else:
        config = TraceConfig(**{flag: True})
    private_error = "PIPECAT-PRIVATE-ERROR-86f"
    original = ValueError(private_error)
    spans, frames, requests, caught = await operation(monkeypatch, error=original, config=config)
    assert isinstance(caught, APIConnectionError)
    assert caught.__cause__ is original and str(original) == private_error
    assert requests[0]["messages"][0]["content"] == INPUT
    llm, = [s for s in spans if s.name == "llm"]
    assert llm.status.status_code.name == "ERROR" and llm.end_time is not None
    assert any("APIConnectionError" in e.attributes.get("exception.type", "") for e in llm.events)
    exported = repr((dict(llm.attributes), llm.status.description, [dict(e.attributes) for e in llm.events]))
    assert private_error not in exported
    assert not [f for f in frames if isinstance(f, LLMTextFrame)]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag,hidden,visible", [
    ("FI_HIDE_INPUTS", INPUT, OUTPUT), ("FI_HIDE_OUTPUTS", OUTPUT, INPUT),
    ("FI_HIDE_INPUT_TEXT", INPUT, OUTPUT), ("FI_HIDE_OUTPUT_TEXT", OUTPUT, INPUT),
    ("FI_HIDE_INPUT_MESSAGES", INPUT, OUTPUT), ("FI_HIDE_OUTPUT_MESSAGES", OUTPUT, INPUT),
])
async def test_native_aliases_and_canonical_messages_obey_independent_privacy(monkeypatch, flag, hidden, visible):
    monkeypatch.setenv(flag, "true")
    spans, frames, requests, error = await operation(monkeypatch)
    assert error is None
    assert requests[0]["messages"][0]["content"] == INPUT
    assert [f.text for f in frames if isinstance(f, LLMTextFrame)] == [OUTPUT]
    llm, = [s for s in spans if s.name == "llm"]
    assert hidden not in repr(dict(llm.attributes))
    assert visible in repr(dict(llm.attributes))


@pytest.mark.asyncio
@pytest.mark.parametrize("field,hidden,visible", [
    ("hide_inputs", INPUT, OUTPUT), ("hide_outputs", OUTPUT, INPUT),
    ("hide_input_text", INPUT, OUTPUT), ("hide_output_text", OUTPUT, INPUT),
    ("hide_input_messages", INPUT, OUTPUT), ("hide_output_messages", OUTPUT, INPUT),
])
async def test_explicit_privacy_preserves_real_processor_results(monkeypatch, field, hidden, visible):
    from fi_instrumentation import TraceConfig

    spans, frames, requests, error = await operation(monkeypatch, config=TraceConfig(**{field: True}))
    assert error is None
    assert requests[0]["messages"][0]["content"] == INPUT
    assert [f.text for f in frames if isinstance(f, LLMTextFrame)] == [OUTPUT]
    llm, = [s for s in spans if s.name == "llm"]
    assert hidden not in repr(dict(llm.attributes))
    assert visible in repr(dict(llm.attributes))


def test_actual_http_exporter_swap_masks_wire_without_mutating_other_sinks(monkeypatch):
    import sys
    from pathlib import Path
    from fi_instrumentation.otel import HTTPSpanExporter
    from traceai_pipecat import enable_http_attribute_mapping, MappedHTTPSpanExporter

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
    from harness import Receiver, _flatten_attributes

    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_API_KEY", "fixture-api-key-not-real")
    monkeypatch.setenv("FI_SECRET_KEY", "fixture-secret-key-not-real")
    with Receiver() as receiver:
        provider = TracerProvider()
        processor = SimpleSpanProcessor(HTTPSpanExporter(endpoint=receiver.collector_endpoint))
        provider.add_span_processor(processor)
        raw = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(raw))
        monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
        assert enable_http_attribute_mapping() is True
        assert isinstance(processor.span_exporter, MappedHTTPSpanExporter)
        with provider.get_tracer("pipecat").start_as_current_span("native") as span:
            span.set_attributes({"input": INPUT, "output": OUTPUT})
        provider.shutdown()
        wire, = receiver.spans()
    attributes = _flatten_attributes(wire["attributes"])
    assert INPUT not in repr(attributes)
    assert attributes["output.value"] == OUTPUT
    original, = raw.get_finished_spans()
    assert original.attributes["input"] == INPUT
    assert "input.value" not in original.attributes


def test_mapping_failure_cannot_export_original_plaintext(monkeypatch):
    exporter = MappedCapture()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def fail(_):
        raise ValueError(INPUT)

    monkeypatch.setattr(exporter, "_convert_attributes", fail)
    with provider.get_tracer("pipecat").start_as_current_span("native") as span:
        span.set_attribute("input", INPUT)
    assert exporter.get_finished_spans() == ()
    provider.shutdown()
