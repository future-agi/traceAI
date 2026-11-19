"""Whole-response output must not carry request context that input settings hide."""
import json

import httpx
import ollama
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_ollama import OllamaInstrumentor

# GenerateResponse.context is the SDK's encoding (token ids) of the conversation
# used for this response, i.e. the request prompt.
CONTEXT_TOKENS = [918273, 918274, 918275]
REPLY = "synthetic generated reply"


def _run_generate(config):
    def respond(request):
        return httpx.Response(200, json={
            "model": "offline-model",
            "created_at": "2026-01-01T00:00:00Z",
            "response": REPLY,
            "done": True,
            "done_reason": "stop",
            "context": CONTEXT_TOKENS,
            "prompt_eval_count": 3,
            "eval_count": 4,
        })

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OllamaInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        client = ollama.Client(host="http://localhost:11434", transport=httpx.MockTransport(respond))
        try:
            response = client.generate(model="offline-model", prompt="synthetic prompt")
        finally:
            client._client.close()
        assert list(response.context) == CONTEXT_TOKENS
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        return json.dumps(dict(spans[0].attributes), default=str) + json.dumps(
            [(e.name, dict(e.attributes or {})) for e in spans[0].events], default=str
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_control_output_recorded_without_settings():
    dumped = _run_generate(TraceConfig())
    assert REPLY in dumped


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}, {"hide_input_images": True}],
)
def test_rawOutputOmitsRequestContextUnderInputSettings(trace_config):
    dumped = _run_generate(TraceConfig(**trace_config))
    assert "918273" not in dumped
