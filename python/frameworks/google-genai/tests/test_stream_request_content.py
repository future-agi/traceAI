"""Streamed output must not carry request contents that input settings hide, as for non-streamed calls."""
import json

import pytest
from fi_instrumentation import TraceConfig
from google import genai
from google.genai import types
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_google_genai import GoogleGenAIInstrumentor
from traceai_google_genai._stream import _ResponseAccumulator, _ResponseExtractor

try:
    import httpx2 as httpx
except ImportError:  # older google-genai releases use httpx
    import httpx

PROMPT_SENTINEL = "SENTINEL_STREAM_CONTENTS_9931"
REPLY = "synthetic streamed reply"
SETTINGS = [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}, {"hide_input_images": True}]


def lookup(topic: str) -> str:
    """Synthetic local tool."""
    return "synthetic tool result"


def _run_stream(config):
    calls = []

    def respond(request):
        calls.append(request)
        part = {"functionCall": {"name": "lookup", "args": {"topic": "unrelated"}}} if len(calls) == 1 else {"text": REPLY}
        body = {"candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("data: " + json.dumps(body) + "\n\n").encode())

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = GoogleGenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        client = genai.Client(
            api_key="offline-placeholder",
            http_options=types.HttpOptions(httpx_client=httpx.Client(transport=httpx.MockTransport(respond))),
        )
        for _ in client.models.generate_content_stream(
            model="gemini-2.0-flash", contents=PROMPT_SENTINEL, config=types.GenerateContentConfig(tools=[lookup]),
        ):
            pass
        spans = exporter.get_finished_spans()
        assert spans
        # The output side only: hide_input_images hides input.value, not the text prompt in the input messages.
        return json.dumps([[dict(s.attributes).get("output.value"), dict(s.attributes).get("raw.output")] for s in spans],
                          default=str)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_the_streamed_request_contents_are_recorded_when_no_setting_is_on():
    assert PROMPT_SENTINEL in _run_stream(TraceConfig())


@pytest.mark.parametrize("setting", SETTINGS)
def test_streamed_output_omits_request_contents_under_input_settings(setting):
    dumped = _run_stream(TraceConfig(**setting))
    assert PROMPT_SENTINEL not in dumped
    assert REPLY in dumped


class _Chunk:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, *, exclude_unset, warnings):
        return self.payload


@pytest.mark.parametrize("hide_input", [True, False])
def test_the_streamed_response_view_keeps_only_response_side_fields_while_inputs_are_hidden(hide_input):
    accumulator = _ResponseAccumulator()
    accumulator.process_chunk(_Chunk({
        "candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": REPLY}]}, "finish_reason": "STOP"}],
        "usage_metadata": {"prompt_token_count": 1},
        "model_version": "m-1",
        "automatic_function_calling_history": [{"role": "user", "parts": [{"text": PROMPT_SENTINEL}]}],
    }))
    attributes = dict(_ResponseExtractor(accumulator, hide_input=hide_input).get_attributes())
    output = json.dumps(attributes)
    assert REPLY in output
    assert (PROMPT_SENTINEL in output) is (not hide_input)
