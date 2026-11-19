"""Whole-response output must not carry request contents that input settings hide."""
import json

import pytest
from fi_instrumentation import TraceConfig
from google import genai
from google.genai import types
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_google_genai import GoogleGenAIInstrumentor

try:
    import httpx2 as httpx
except ImportError:  # older google-genai releases use httpx
    import httpx

PROMPT_SENTINEL = "SENTINEL_REQUEST_CONTENTS_5518"
REPLY = "synthetic generated reply"


def lookup(topic: str) -> str:
    """Synthetic local tool."""
    return "synthetic tool result"


def _run(config):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            part = {"functionCall": {"name": "lookup", "args": {"topic": "unrelated"}}}
        else:
            part = {"text": REPLY}
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [part]},
                            "finishReason": "STOP", "index": 0}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1,
                              "totalTokenCount": 2},
        })

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = GoogleGenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        client = genai.Client(
            api_key="offline-placeholder",
            http_options=types.HttpOptions(
                httpx_client=httpx.Client(transport=httpx.MockTransport(respond))),
        )
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=PROMPT_SENTINEL,
            config=types.GenerateContentConfig(tools=[lookup]),
        )
        # SDK field: GenerateContentResponse.automatic_function_calling_history
        # holds the request contents used for the call.
        history = response.automatic_function_calling_history
        assert history and history[0].parts[0].text == PROMPT_SENTINEL
        assert response.text == REPLY
        spans = exporter.get_finished_spans()
        assert spans
        return json.dumps([dict(s.attributes) for s in spans], default=str) + json.dumps(
            [[(e.name, dict(e.attributes or {})) for e in s.events] for s in spans],
            default=str,
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_control_output_recorded_without_settings():
    assert REPLY in _run(TraceConfig())


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_rawOutputOmitsRequestContentsUnderInputSettings(trace_config):
    assert PROMPT_SENTINEL not in _run(TraceConfig(**trace_config))
