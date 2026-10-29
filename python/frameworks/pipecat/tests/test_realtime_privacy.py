"""Privacy at installed Pipecat 0.0.77 realtime and multimodal boundaries.

The native service hooks/attribute producers run unchanged. Only websocket
transport is scripted; hiding telemetry must not hide application content.
These contracts run on both the package's locked legacy core and source core.
"""
import json
import os

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from pipecat.services.gemini_multimodal_live import events as gemini_events
from pipecat.services.gemini_multimodal_live.gemini import (
    GeminiMultimodalLiveContext,
    GeminiMultimodalLiveLLMService,
)
from pipecat.services.openai_realtime_beta import events as realtime_events
from pipecat.services.openai_realtime_beta.openai import OpenAIRealtimeBetaLLMService
from pipecat.utils.tracing.service_attributes import (
    add_gemini_live_span_attributes,
    add_openai_realtime_span_attributes,
)

from traceai_pipecat.exporters.base_exporter import BaseMappedSpanExporter

INPUT = "PIPECAT-REALTIME-PRIVATE-INPUT-83b"
OUTPUT = "PIPECAT-GEMINI-PRIVATE-OUTPUT-76d"
INPUT_FLAGS = ("hide_inputs", "hide_input_text", "hide_input_messages")
OUTPUT_FLAGS = ("hide_outputs", "hide_output_text", "hide_output_messages")


class Capture(BaseMappedSpanExporter, InMemorySpanExporter):
    pass


class Wire:
    def __init__(self):
        self.rows = []

    async def send(self, message):
        self.rows.append(json.loads(message))


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("FI_HIDE_"):
            monkeypatch.delenv(key)


@pytest.fixture
def capture(monkeypatch):
    providers = []

    def make(config=None):
        sink = Capture(config=config)
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(sink))
        monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
        providers.append(provider)
        return provider.get_tracer("pipecat-contract"), sink

    yield make
    for provider in providers:
        provider.shutdown()
    assert not trace.get_current_span().is_recording()


def privacy_config(monkeypatch, flag, via_environment):
    if flag is None:
        return TraceConfig()
    if via_environment:
        monkeypatch.setenv("FI_" + flag.upper(), "true")
        return TraceConfig()
    return TraceConfig(**{flag: True})


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", (None,) + INPUT_FLAGS)
@pytest.mark.parametrize("via_environment", (False, True), ids=("explicit", "environment"))
async def test_real_realtime_setup_hides_all_instruction_aliases_but_preserves_wire(
    monkeypatch, capture, flag, via_environment
):
    tracer, sink = capture(privacy_config(monkeypatch, flag, via_environment))
    service = OpenAIRealtimeBetaLLMService(
        api_key="offline-fixture-not-real",
        session_properties=realtime_events.SessionProperties(instructions=INPUT),
    )
    service._tracing_enabled = True
    wire = Wire()
    service._websocket = wire
    evt = realtime_events.SessionCreatedEvent(
        type="session.created", event_id="fixture", session=realtime_events.SessionProperties()
    )
    with tracer.start_as_current_span("parent"):
        result = await service._handle_evt_session_created(evt)
    assert result is None
    assert wire.rows[0]["session"]["instructions"] == INPUT
    assert service._session_properties.instructions == INPUT
    setup, parent = sink.get_finished_spans()
    assert setup.name == "llm_setup" and setup.end_time is not None
    assert setup.parent.span_id == parent.context.span_id
    if flag:
        assert INPUT not in repr(dict(setup.attributes))
    else:
        assert setup.attributes["instructions"] == INPUT
        assert setup.attributes["session.instructions"] == INPUT
    assert setup.attributes["gen_ai.provider.name"] == "openai"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", (None,) + INPUT_FLAGS)
@pytest.mark.parametrize("via_environment", (False, True), ids=("explicit", "environment"))
async def test_real_gemini_setup_hides_service_and_context_instructions(
    monkeypatch, capture, flag, via_environment
):
    tracer, sink = capture(privacy_config(monkeypatch, flag, via_environment))
    service = GeminiMultimodalLiveLLMService(api_key="offline-fixture-not-real", system_instruction=INPUT)
    service._context = GeminiMultimodalLiveContext(messages=[{"role": "system", "content": INPUT + "-context"}])
    service._tracing_enabled = True
    with tracer.start_as_current_span("parent"):
        result = await service._handle_evt_setup_complete(
            gemini_events.ServerEvent(setupComplete=gemini_events.SetupComplete())
        )
    assert result is None and service._api_session_ready is True
    assert service._system_instruction == INPUT
    assert service._context.extract_system_instructions() == INPUT + "-context"
    setup, parent = sink.get_finished_spans()
    assert setup.name == "llm_setup" and setup.end_time is not None
    assert setup.parent.span_id == parent.context.span_id
    if flag:
        assert INPUT not in repr(dict(setup.attributes))
    else:
        assert setup.attributes["system_instruction"] == INPUT
        assert setup.attributes["context_system_instruction"] == INPUT + "-context"
    assert setup.attributes["gen_ai.provider.name"] == "gcp.gemini"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", (None,) + OUTPUT_FLAGS)
@pytest.mark.parametrize("via_environment", (False, True), ids=("explicit", "environment"))
async def test_real_gemini_tool_result_privacy_does_not_change_wire(
    monkeypatch, capture, flag, via_environment
):
    tracer, sink = capture(privacy_config(monkeypatch, flag, via_environment))
    service = GeminiMultimodalLiveLLMService(api_key="offline-fixture-not-real")
    service._tracing_enabled = True
    wire = Wire()
    service._websocket = wire
    payload = {"tool_call_id": "fixture", "tool_call_name": "tool", "content": json.dumps({"result": OUTPUT})}
    with tracer.start_as_current_span("parent"):
        result = await service._tool_result(payload)
    assert result is None
    assert wire.rows[0]["toolResponse"]["functionResponses"][0]["response"]["result"] == {"result": OUTPUT}
    assert payload["content"] == json.dumps({"result": OUTPUT})
    operation, parent = sink.get_finished_spans()
    assert operation.name == "llm_tool_result" and operation.end_time is not None
    assert operation.parent.span_id == parent.context.span_id
    if flag:
        assert OUTPUT not in repr(dict(operation.attributes))
    else:
        assert OUTPUT in operation.attributes["tool.result"]
        assert OUTPUT in operation.attributes["raw.output"]
    assert operation.attributes["tool_call.function.name"] == "tool"


@pytest.mark.parametrize("sdk", (add_gemini_live_span_attributes, add_openai_realtime_span_attributes))
@pytest.mark.parametrize("flag", (None,) + INPUT_FLAGS + OUTPUT_FLAGS)
@pytest.mark.parametrize("is_input", (False, True), ids=("output-transcript", "input-transcript"))
def test_native_transcript_direction_and_independent_privacy(capture, sdk, flag, is_input):
    tracer, sink = capture(TraceConfig(**({flag: True} if flag else {})))
    marker = INPUT if is_input else OUTPUT
    with tracer.start_as_current_span("transcription") as span:
        sdk(span, "fixture", "fixture-model", "transcription", transcript=marker, is_input=is_input)
        span.set_attribute("gen_ai.usage.input_tokens", 3)
    operation, = sink.get_finished_spans()
    attributes = dict(operation.attributes)
    assert attributes["gen_ai.usage.input_tokens"] == 3
    direction = "input" if is_input else "output"
    opposite = "output" if is_input else "input"
    hidden = flag in (INPUT_FLAGS if is_input else OUTPUT_FLAGS)
    assert f"{opposite}.value" not in attributes
    assert not any(k.startswith(f"gen_ai.{opposite}.messages.") for k in attributes)
    if hidden:
        assert marker not in repr(attributes)
    else:
        assert attributes[f"{direction}.value"] == marker
        assert attributes[f"gen_ai.{direction}.messages.0.message.content"] == marker
        assert attributes[f"gen_ai.{direction}.messages.0.message.role"] == ("user" if is_input else "assistant")


@pytest.mark.parametrize("flag", (None,) + OUTPUT_FLAGS + INPUT_FLAGS)
def test_native_gemini_text_output_is_mapped_and_hidden_only_as_output(capture, flag):
    tracer, sink = capture(TraceConfig(**({flag: True} if flag else {})))
    with tracer.start_as_current_span("model_turn") as span:
        add_gemini_live_span_attributes(span, "fixture", "fixture-model", "model_turn", text_output=OUTPUT)
    operation, = sink.get_finished_spans()
    if flag in OUTPUT_FLAGS:
        assert OUTPUT not in repr(dict(operation.attributes))
    else:
        assert operation.attributes["output.value"] == OUTPUT
        assert operation.attributes["gen_ai.output.messages.0.message.content"] == OUTPUT


@pytest.mark.parametrize("metadata", (
    "opaque application metadata", "[1,2]", "null", "42", "true", '"a JSON string"',
    json.dumps({"safe": "retained", "system_instruction": INPUT}),
))
@pytest.mark.parametrize("flag", INPUT_FLAGS)
def test_nonobject_metadata_never_drops_usage_status_or_parentage(capture, metadata, flag):
    tracer, sink = capture(TraceConfig(**{flag: True}))
    with tracer.start_as_current_span("parent"):
        with tracer.start_as_current_span("native") as span:
            span.set_attributes({"metadata": metadata, "input": INPUT, "gen_ai.usage.input_tokens": 3})
            span.set_status(Status(StatusCode.ERROR, INPUT))
    operation, parent = sink.get_finished_spans()
    assert operation.parent.span_id == parent.context.span_id
    assert operation.end_time is not None and operation.status.status_code == StatusCode.ERROR
    assert operation.attributes["gen_ai.usage.input_tokens"] == 3
    assert INPUT not in repr(dict(operation.attributes))
    if metadata.startswith("{"):
        assert json.loads(operation.attributes["metadata"])["safe"] == "retained"


@pytest.mark.parametrize("flag", INPUT_FLAGS + OUTPUT_FLAGS)
@pytest.mark.parametrize("via_environment", (False, True), ids=("explicit", "environment"))
def test_legacy_and_source_core_error_details_are_masked_everywhere(
    monkeypatch, capture, flag, via_environment
):
    tracer, sink = capture(privacy_config(monkeypatch, flag, via_environment))
    with tracer.start_as_current_span("error") as span:
        span.set_attributes({"error.message": INPUT, "provider.error_message": INPUT,
                             "error.type": "FixtureError", "exception.type": "ValueError"})
        span.set_status(Status(StatusCode.ERROR, INPUT))
        span.record_exception(ValueError(INPUT))
        span.add_event("provider.error", {"error.message": INPUT, "provider.exception.message": INPUT,
                                          "error.type": "FixtureError"})
    operation, = sink.get_finished_spans()
    assert operation.status.status_code == StatusCode.ERROR
    assert operation.status.description is None
    assert operation.attributes["error.type"] == "FixtureError"
    assert operation.attributes["exception.type"] == "ValueError"
    assert operation.events[0].attributes["exception.type"] == "ValueError"
    assert operation.events[1].attributes["error.type"] == "FixtureError"
    assert INPUT not in repr((dict(operation.attributes), [dict(e.attributes) for e in operation.events]))


@pytest.mark.parametrize("policy,hidden", (("true", True), ("false", False), ("invalid", True)))
def test_explicit_error_environment_policy_overrides_legacy_derivation(monkeypatch, capture, policy, hidden):
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", policy)
    tracer, sink = capture(TraceConfig(hide_output_text=True))
    with tracer.start_as_current_span("error") as span:
        span.set_attribute("error.message", INPUT)
        span.set_status(Status(StatusCode.ERROR, INPUT))
        span.record_exception(ValueError(INPUT))
    operation, = sink.get_finished_spans()
    assert operation.status.status_code == StatusCode.ERROR
    exported = repr((dict(operation.attributes), operation.status.description,
                     [dict(e.attributes) for e in operation.events]))
    assert (INPUT not in exported) == hidden


def test_error_only_environment_policy_hides_details_without_hiding_content(monkeypatch, capture):
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    tracer, sink = capture()
    with tracer.start_as_current_span("error") as span:
        span.set_attributes({"input": INPUT, "output": OUTPUT, "error.message": INPUT})
        span.set_status(Status(StatusCode.ERROR, INPUT))
        span.record_exception(ValueError(INPUT))
    operation, = sink.get_finished_spans()
    assert operation.attributes["input.value"] == INPUT
    assert operation.attributes["output.value"] == OUTPUT
    assert "error.message" not in operation.attributes
    assert operation.status.description is None
    assert INPUT not in repr([dict(e.attributes) for e in operation.events])
