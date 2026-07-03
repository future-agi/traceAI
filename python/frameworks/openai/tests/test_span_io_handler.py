"""
Regression test for issue #151: multi-turn conversation input was being
dropped from the span. _process_input_data set INPUT_VALUE three times
on the same span, and the last two writes overwrote the first, correct
one, leaving only the first message's text on the span.
"""

import json

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from fi_instrumentation.fi_types import SpanAttributes
from traceai_openai._span_io_handler import _process_input_data
from traceai_openai._with_span import _WithSpan


@pytest.fixture
def recorder():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    return tracer, exporter


def _get_input_value(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0].attributes.get(SpanAttributes.INPUT_VALUE)


def test_multi_message_input_is_not_dropped(recorder):
    tracer, exporter = recorder
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "And Germany?"},
    ]

    with tracer.start_as_current_span("openai.chat") as span:
        _process_input_data(messages, _WithSpan(span))

    input_value = _get_input_value(exporter)
    assert input_value is not None

    parsed = json.loads(input_value)
    assert len(parsed) == len(messages), (
        "Expected all messages to survive in INPUT_VALUE, but only "
        f"{len(parsed)} of {len(messages)} were present."
    )
    assert [m["content"] for m in parsed] == [m["content"] for m in messages]


def test_multimodal_message_list_preserves_all_messages(recorder):
    tracer, exporter = recorder
    messages = [
        {"role": "user", "content": "Describe this image."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this picture?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/cat.png"},
                },
            ],
        },
    ]

    with tracer.start_as_current_span("openai.chat") as span:
        _process_input_data(messages, _WithSpan(span))

    input_value = _get_input_value(exporter)
    parsed = json.loads(input_value)
    assert len(parsed) == 2

    spans = exporter.get_finished_spans()
    images_value = spans[0].attributes.get(SpanAttributes.INPUT_IMAGES)
    assert images_value is not None
    assert json.loads(images_value) == ["https://example.com/cat.png"]
