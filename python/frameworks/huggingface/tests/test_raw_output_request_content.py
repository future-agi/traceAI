"""Whole-response output must not carry request tokens that input settings hide."""
import json

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_huggingface import HuggingFaceInstrumentor

PROMPT_SENTINEL = "SENTINEL_PREFILL_TOKEN_6612"
REPLY = "synthetic generated reply"


def _run_text_generation(config, monkeypatch):
    from huggingface_hub import InferenceClient
    from huggingface_hub.inference._generated.types import TextGenerationOutput

    # TextGenerationOutput.details.prefill carries the prompt tokens back.
    result = TextGenerationOutput.parse_obj_as_instance({
        "generated_text": REPLY,
        "details": {
            "finish_reason": "length",
            "generated_tokens": 4,
            "prefill": [{"id": 1, "logprob": -0.1, "text": PROMPT_SENTINEL}],
            "tokens": [{"id": 2, "logprob": -0.2, "special": False, "text": REPLY}],
        },
    })
    monkeypatch.setattr(InferenceClient, "text_generation", lambda self, *a, **k: result)

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = HuggingFaceInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        client = InferenceClient(model="offline-model", token="offline-placeholder")
        response = client.text_generation(PROMPT_SENTINEL, details=True, decoder_input_details=True)
        assert response.details.prefill[0].text == PROMPT_SENTINEL
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        return json.dumps(dict(spans[0].attributes), default=str) + json.dumps(
            [(e.name, dict(e.attributes or {})) for e in spans[0].events], default=str
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_control_output_recorded_without_settings(monkeypatch):
    dumped = _run_text_generation(TraceConfig(), monkeypatch)
    assert REPLY in dumped


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}, {"hide_input_images": True}],
)
def test_rawOutputOmitsRequestTokensUnderInputSettings(trace_config, monkeypatch):
    dumped = _run_text_generation(TraceConfig(**trace_config), monkeypatch)
    assert PROMPT_SENTINEL not in dumped
