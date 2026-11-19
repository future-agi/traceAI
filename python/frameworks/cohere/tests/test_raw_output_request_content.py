"""Whole-response output must not carry request history that input settings hide."""
import json

import cohere
import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cohere import CohereInstrumentor

HISTORY_SENTINEL = "SENTINEL_CHAT_HISTORY_8830"
REPLY = "synthetic generated reply"


def _run_chat(config):
    def respond(request):
        # NonStreamedChatResponse.chat_history echoes the conversation back.
        return httpx.Response(200, json={
            "text": REPLY,
            "generation_id": "offline-gen",
            "finish_reason": "COMPLETE",
            "chat_history": [
                {"role": "USER", "message": HISTORY_SENTINEL},
                {"role": "CHATBOT", "message": REPLY},
            ],
            "meta": {"billed_units": {"input_tokens": 2, "output_tokens": 3}},
        })

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CohereInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = cohere.Client(api_key="offline-placeholder", httpx_client=http)
            response = client.chat(
                model="command-r",
                message="next question",
                chat_history=[{"role": "USER", "message": HISTORY_SENTINEL}],
            )
        assert response.chat_history[0].message == HISTORY_SENTINEL
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        return json.dumps(dict(spans[0].attributes), default=str) + json.dumps(
            [(e.name, dict(e.attributes or {})) for e in spans[0].events], default=str
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_control_output_recorded_without_settings():
    dumped = _run_chat(TraceConfig())
    assert REPLY in dumped


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_rawOutputOmitsRequestHistoryUnderInputSettings(trace_config):
    dumped = _run_chat(TraceConfig(**trace_config))
    assert HISTORY_SENTINEL not in dumped

def _output_values(dumped):
    """The output.value of every span in a dump (the first JSON document of the dump holds the attributes)."""
    parsed, _ = json.JSONDecoder().raw_decode(dumped)
    return [str(attrs.get("output.value")) for attrs in (parsed if isinstance(parsed, list) else [parsed])]


def test_inputImagesSettingKeepsTheRequestOutOfOutputValue():
    # hide_input_images hides input.value, not a text prompt in the input messages: check the output side.
    for value in _output_values(_run_chat(TraceConfig(hide_input_images=True))):
        assert HISTORY_SENTINEL not in value
