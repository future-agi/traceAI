"""Whole-response output must not carry request content that input settings hide."""
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SENTINEL = "SENTINEL_REQUEST_PROMPT_4471"
REPLY = "generated reply text"


def _exported(trace_config):
    from fi_instrumentation import TraceConfig
    from together import Together
    from together.resources.completions import Completions
    from together.types.completions import CompletionResponse

    from traceai_together import TogetherInstrumentor

    def create(self, *args, **kwargs):
        # SDK field CompletionResponse.prompt carries the request prompt back.
        return CompletionResponse(
            id="cmpl-1",
            object="text.completion",
            created=1,
            model=kwargs["model"],
            choices=[{"index": 0, "text": REPLY, "finish_reason": "stop"}],
            prompt=[{"text": kwargs["prompt"]}],
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        )

    original = Completions.create
    Completions.create = create
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = TogetherInstrumentor()
    try:
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**trace_config))
        client = Together(api_key="test-key")
        client.completions.create(model="m", prompt=SENTINEL, echo=True)
    finally:
        instrumentor.uninstrument()
        Completions.create = original
    spans = exporter.get_finished_spans()
    assert spans
    return json.dumps([dict(s.attributes) for s in spans], default=str) + json.dumps(
        [[(e.name, dict(e.attributes or {})) for e in s.events] for s in spans], default=str
    )


def test_control_output_recorded_without_settings():
    dumped = _exported({})
    assert REPLY in dumped


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_rawOutputOmitsRequestPromptUnderInputSettings(trace_config):
    dumped = _exported(trace_config)
    assert SENTINEL not in dumped
