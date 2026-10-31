"""Real native multimodal spans must honor every image representation.

Only OpenAI's HTTP transport is scripted. The real Pipecat processor, frames,
SDK response parsing and the mapped exporter are exercised unchanged.
"""
import copy
import json
import os

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pipecat.frames.frames import LLMTextFrame
from pipecat.utils.tracing.service_attributes import (
    add_gemini_live_span_attributes,
    add_llm_span_attributes,
    add_openai_realtime_span_attributes,
)

import test_real_processor as processor

IMAGE = "data:image/png;base64,UElQRUNBVC1QUklWQVRFLUlNQUdFLTcyZg=="
REMOTE_IMAGE = "https://images.invalid/private-pipecat-image-72f.png"
TEXT = "public image question"
CONTENT = [
    {"type": "text", "text": TEXT},
    {"type": "image_url", "image_url": {"url": IMAGE, "detail": "low"}},
    {"type": "image_url", "image_url": {"url": REMOTE_IMAGE}},
]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("FI_HIDE_") or key in ("FI_BASE64_IMAGE_MAX_LENGTH", "FI_PII_REDACTION"):
            monkeypatch.delenv(key)


def image_config(monkeypatch, policy, via_environment):
    if policy == "default":
        return TraceConfig()
    values = {"hide_input_images": True} if policy == "hide" else {"base64_image_max_length": len(IMAGE) - 1}
    if via_environment:
        for key, value in values.items():
            monkeypatch.setenv("FI_" + key.upper(), str(value).lower())
        return TraceConfig()
    return TraceConfig(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ("default", "hide", "limit"))
@pytest.mark.parametrize("via_environment", (False, True), ids=("explicit", "environment"))
async def test_real_multimodal_processor_keeps_text_wire_usage_and_parentage(
    monkeypatch, policy, via_environment
):
    original = copy.deepcopy(CONTENT)
    monkeypatch.setattr(processor, "INPUT", original)
    spans, frames, requests, error = await processor.operation(
        monkeypatch, config=image_config(monkeypatch, policy, via_environment)
    )
    assert error is None
    assert original == CONTENT
    assert requests[0]["messages"] == [{"role": "user", "content": CONTENT}]
    assert [f.text for f in frames if isinstance(f, LLMTextFrame)] == [processor.OUTPUT]
    assert sorted(s.name for s in spans) == ["llm", "turn"]
    llm, = [s for s in spans if s.name == "llm"]
    parent, = [s for s in spans if s.name == "turn"]
    assert llm.end_time is not None and parent.end_time is not None
    assert llm.parent.span_id == parent.context.span_id
    assert llm.context.trace_id == parent.context.trace_id
    assert llm.attributes["gen_ai.usage.input_tokens"] == 3
    assert llm.attributes["gen_ai.usage.output_tokens"] == 2
    assert llm.attributes["gen_ai.usage.total_tokens"] == 5
    assert llm.attributes["output.value"] == processor.OUTPUT
    for key in ("input", "input.value", "gen_ai.input.messages.0.message.content"):
        value = llm.attributes[key]
        assert TEXT in value
        assert (IMAGE in value) == (policy == "default"), key
        assert (REMOTE_IMAGE in value) == (policy != "hide"), key


@pytest.mark.parametrize("via_environment", (False, True), ids=("explicit", "environment"))
@pytest.mark.parametrize("representation", ("native", "canonical", "raw", "metadata", "event"))
def test_image_hiding_covers_retained_aliases_without_mutating_other_sinks(
    monkeypatch, via_environment, representation
):
    config = image_config(monkeypatch, "hide", via_environment)
    mapped = processor.MappedCapture(config=config)
    raw = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(mapped))
    provider.add_span_processor(SimpleSpanProcessor(raw))
    messages = json.dumps([{"role": "user", "content": CONTENT}])
    key, value = {
        "native": ("input", messages),
        "canonical": ("gen_ai.input.messages.0.message.content", json.dumps(CONTENT)),
        "raw": ("raw.input", json.dumps({"messages": json.loads(messages)})),
        "metadata": ("metadata", json.dumps({"safe": "retained", "input": json.loads(messages),
                                                "output": CONTENT})),
        "event": ("input", messages),
    }[representation]
    try:
        with provider.get_tracer("native").start_as_current_span("parent"):
            with provider.get_tracer("native").start_as_current_span("operation") as span:
                add_llm_span_attributes(span, "OpenAILLMService", "fixture", output=processor.OUTPUT)
                span.set_attribute("gen_ai.usage.input_tokens", 3)
                if representation == "event":
                    span.add_event("native.input", {key: value})
                else:
                    span.set_attribute(key, value)
        operation, parent = mapped.get_finished_spans()
        assert operation.parent.span_id == parent.context.span_id
        assert operation.end_time is not None
        assert operation.attributes["gen_ai.usage.input_tokens"] == 3
        assert operation.attributes["output.value"] == processor.OUTPUT
        if representation == "event":
            exported = operation.events[0].attributes[key]
        elif representation == "metadata":
            metadata = json.loads(operation.attributes[key])
            assert metadata["safe"] == "retained"
            # Input image hiding is independent of output images.
            assert metadata["output"] == CONTENT
            exported = json.dumps(metadata["input"])
        else:
            exported = operation.attributes[key]
        assert TEXT in exported
        assert IMAGE not in exported and REMOTE_IMAGE not in exported
        untouched = raw.get_finished_spans()[0]
        assert (untouched.events[0].attributes if representation == "event" else untouched.attributes)[key] == value
    finally:
        provider.shutdown()


@pytest.mark.parametrize("limit", (len(IMAGE), len(IMAGE) + 1))
def test_base64_limit_is_strict_and_keeps_images_at_the_boundary(limit):
    exporter = processor.MappedCapture(config=TraceConfig(base64_image_max_length=limit))
    messages = json.dumps([{"role": "user", "content": CONTENT}])
    assert exporter._convert_attributes({"input": messages})["input"] == messages


@pytest.mark.parametrize("content", (
    {"type": "input_image", "image_url": IMAGE},
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": IMAGE.split(",", 1)[1]}},
    {"inline_data": {"mime_type": "image/png", "data": IMAGE.split(",", 1)[1]}},
    {"fileData": {"mimeType": "image/png", "fileUri": REMOTE_IMAGE}},
))
@pytest.mark.parametrize("hide", (False, True))
def test_structured_image_blocks_do_not_redact_nonimage_neighbors(content, hide):
    original = [{"type": "text", "text": TEXT}, content,
                {"inline_data": {"mime_type": "audio/pcm", "data": "public-audio"}}]
    encoded = json.dumps(original)
    exporter = processor.MappedCapture(config=TraceConfig(hide_input_images=hide))
    converted = exporter._convert_attributes({"input": encoded})
    result = json.loads(converted["input"])
    assert result[0] == original[0] and result[2] == original[2]
    assert (IMAGE.split(",", 1)[1] in converted["input"] or REMOTE_IMAGE in converted["input"]) == (not hide)
    assert original[1] == content


def test_other_public_policies_still_reach_canonical_and_native_attributes():
    exporter = processor.MappedCapture(config=TraceConfig(pii_redaction=True, hide_embedding_vectors=True))
    converted = exporter._convert_attributes({
        "input": "Contact synthetic.person@example.com",
        "embedding.embeddings.0.embedding.vector": (0.25, 0.75),
        "gen_ai.request.temperature": 0.437,
    })
    assert "synthetic.person@example.com" not in repr(converted)
    assert "embedding.embeddings.0.embedding.vector" not in converted
    # Parameter privacy is independent; it is retained by default.
    assert converted["gen_ai.request.temperature"] == 0.437


@pytest.mark.parametrize("via_environment", (False, True), ids=("explicit", "environment"))
def test_invocation_policy_masks_native_and_metadata_parameter_aliases(monkeypatch, via_environment):
    if via_environment:
        monkeypatch.setenv("FI_HIDE_LLM_INVOCATION_PARAMETERS", "true")
        config = TraceConfig()
    else:
        config = TraceConfig(hide_llm_invocation_parameters=True)
    exporter = processor.MappedCapture(config=config)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer("native").start_as_current_span("operation") as span:
            add_llm_span_attributes(span, "OpenAILLMService", "fixture", messages=TEXT,
                                    output=processor.OUTPUT,
                                    parameters={"temperature": 0.437, "custom": "PRIVATE-PARAM"},
                                    extra_parameters={"custom": "PRIVATE-EXTRA"})
            add_gemini_live_span_attributes(span, "fixture", "fixture", "setup",
                                           settings={"temperature": 0.437})
            add_openai_realtime_span_attributes(span, "fixture", "fixture", "setup",
                                               session_properties={"temperature": 0.437, "instructions": TEXT})
            span.set_attribute("metadata", json.dumps({"settings": {"temperature": 0.437}, "safe": "retained"}))
        operation, = exporter.get_finished_spans()
        assert operation.end_time is not None
        assert operation.attributes["gen_ai.request.model"] == "fixture"
        assert operation.attributes["input.value"] == TEXT
        assert operation.attributes["output.value"] == processor.OUTPUT
        assert operation.attributes["session.instructions"] == TEXT
        exported = repr(dict(operation.attributes))
        assert "0.437" not in exported
        assert "PRIVATE-PARAM" not in exported and "PRIVATE-EXTRA" not in exported
        assert json.loads(operation.attributes["metadata"])["safe"] == "retained"
    finally:
        provider.shutdown()
