"""A structured stream payload must not carry request content that input settings hide."""
import json
from unittest.mock import Mock

import pytest
from botocore.eventstream import EventStream
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_bedrock._wrappers import _InvokeModelWithResponseStream

PROMPT_SENTINEL = "SENTINEL_STREAM_PROMPT_5518"
ANSWER = "synthetic generated text"


class OfflineStream(EventStream):
    def __init__(self, events):
        self.events = events

    def __iter__(self):
        yield from self.events

    def close(self):
        return "closed"


def _chunk(payload):
    return {"chunk": {"bytes": json.dumps(payload).encode()}}


def _run(config):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=config)
    # The final event of a Cohere stream repeats the request prompt next to the generations.
    final = {
        "is_finished": True,
        "finish_reason": "COMPLETE",
        "response": {"prompt": PROMPT_SENTINEL, "generations": [{"text": ANSWER}]},
    }
    kwargs = {"modelId": "cohere.command-text-v14", "contentType": "application/json", "body": '{"max_tokens":16}'}
    response = {"body": OfflineStream([_chunk(final)]), "contentType": "application/json"}
    wrapped = Mock(return_value=response)
    result = _InvokeModelWithResponseStream(tracer)(wrapped)(**kwargs)
    list(result["body"])
    provider.shutdown()
    (span,) = exporter.get_finished_spans()
    return json.dumps(dict(span.attributes), default=str) + json.dumps(
        [(e.name, dict(e.attributes or {})) for e in span.events], default=str
    )


def test_control_output_recorded_without_settings():
    assert ANSWER in _run(TraceConfig())


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_structuredStreamOutputOmitsRequestPromptUnderInputSettings(trace_config):
    assert PROMPT_SENTINEL not in _run(TraceConfig(**trace_config))
