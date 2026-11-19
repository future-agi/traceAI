"""base64_image_max_length / FI_BASE64_IMAGE_MAX_LENGTH, by value and whatever the key.

A base64 image longer than the limit is replaced, whole, by the redaction marker in every
exported attribute, event and status; a value of exactly the limit is exported unchanged.
One case per key shape the instrumentations export (source lines cited). Uses a real SDK
TracerProvider with an in-memory exporter, so the assertions are on the exported spans.
"""

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import (
    DEFAULT_BASE64_IMAGE_MAX_LENGTH,
    FI_BASE64_IMAGE_MAX_LENGTH,
    REDACTED_VALUE,
    TraceConfig,
)

MAX = 1000
# A synthetic payload marker: base64 letters, so it is part of the base64 text
CANARY = "CANARYxPAYLOADxBYTES"
# base64 of the PNG signature and the start of an IHDR chunk
PNG_HEAD = "iVBORw0KGgoAAAANSUhEUg"
MSG = "gen_ai.input.messages"


def base64_of(length, head=""):
    start = head + CANARY
    return start + "A" * (length - len(start))


def data_url(length):
    prefix = "data:image/png;base64,"
    return prefix + base64_of(length - len(prefix), PNG_HEAD)


def exported(span):
    return json.dumps(
        {
            "attributes": dict(span.attributes),
            "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in span.events],
            "status": [span.status.status_code.name, span.status.description],
        }
    )


def assert_no_image(span):
    text = exported(span)
    assert CANARY not in text
    assert "base64," not in text
    assert PNG_HEAD not in text


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def export_with(exporter, clean_env):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def _export(attributes, **config):
        config.setdefault("base64_image_max_length", MAX)
        tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(**config))
        exporter.clear()
        with tracer.start_as_current_span("llm", attributes=dict(attributes)) as span:
            span.set_attributes(dict(attributes))
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        return spans[0]

    return _export


def body_with(url):
    return json.dumps(
        {"model": "gpt-x", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": url}},
        ]}]}
    )


# One case per key shape: label, attributes for an image of `length`
SHAPES = [
    # traceai_openai/_request_attributes_extractor.py:113,137,200-205 (also groq :162,
    # litellm :98, llama_index :850, smolagents :334, langchain :1218): no image.url suffix
    ("openai chat message_content.image", lambda n: {
        f"{MSG}.0.message.contents.1.message_content.image": data_url(n),
    }),
    # traceai_openai/_attributes/_responses_api.py:321,336 (prefix + message_content.image)
    ("openai responses input image", lambda n: {
        f"{MSG}.0.message.contents.0.message_content.image": data_url(n),
    }),
    # fi_instrumentation/instrumentation/_attributes.py:447-452 (core helper), mistralai :134,
    # langchain :1230, llama_index :859, litellm :108
    ("message_content.image.image.url", lambda n: {
        f"{MSG}.0.message.contents.1.message_content.image.image.url": data_url(n),
    }),
    # traceai_anthropic/_wrappers.py:746-749 (raw base64 source.data, no data URL); also bedrock
    # utils/anthropic/_messages.py:286, google-genai :492, vertexai :609
    ("anthropic raw base64 under message_content.image", lambda n: {
        f"{MSG}.0.message.content.0.message_content.image": base64_of(n),
    }),
    # traceai_langchain/_tracer.py:1213 (message_content.image.<field>)
    ("langchain message_content.image.<field>", lambda n: {
        f"{MSG}.0.message.contents.0.message_content.image.data": base64_of(n),
    }),
    # Output images: traceai_openai/_response_attributes_extractor.py:339, litellm :228, vertexai :629
    ("output message image", lambda n: {
        "gen_ai.output.messages.0.message.content.0.message_content.image": data_url(n),
    }),
    # The request body copies: input.value / raw.input JSON with a chat image_url
    ("input.value and raw.input request body", lambda n: {
        "input.value": body_with(data_url(n)),
        "input.mime_type": "application/json",
        "raw.input": body_with(data_url(n)),
    }),
    # A declared-image JSON object (Anthropic/Bedrock source), and Google inline_data, in a body
    ("declared image objects in JSON", lambda n: {
        "input.value": json.dumps({"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64_of(n)}},
        ]}], "contents": [{"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": base64_of(n)}}]}]}),
    }),
    # A bare base64 PNG with no declaring field, and a data URL inside plain text
    ("bare base64 PNG and a data URL in text", lambda n: {
        "tool.output": json.dumps({"image": {"source": {"bytes": base64_of(n, PNG_HEAD)}}}),
        "input.value": f"describe [image: {data_url(n)}] please",
    }),
    # A double-encoded body
    ("double-encoded body", lambda n: {"raw.input": json.dumps(body_with(data_url(n)))}),
]


@pytest.mark.parametrize("label,shape", SHAPES, ids=[s[0] for s in SHAPES])
def test_longer_than_the_limit_only_the_marker_is_exported(export_with, label, shape):
    span = export_with(shape(MAX + 1))
    assert_no_image(span)
    assert REDACTED_VALUE in exported(span)


@pytest.mark.parametrize("label,shape", SHAPES, ids=[s[0] for s in SHAPES])
def test_exactly_the_limit_is_exported_unchanged(export_with, label, shape):
    attributes = shape(MAX)
    assert dict(export_with(attributes).attributes) == attributes


def test_whole_value_becomes_exactly_the_marker(export_with):
    key = f"{MSG}.0.message.contents.1.message_content.image"
    assert export_with({key: data_url(MAX + 1)}).attributes[key] == REDACTED_VALUE


def test_only_the_image_is_replaced_in_a_body(export_with):
    value = export_with({"input.value": body_with(data_url(MAX + 1))}).attributes["input.value"]
    assert json.loads(value)["messages"][0]["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": REDACTED_VALUE}},
    ]


def test_events_the_exception_and_the_status_are_capped(exporter, clean_env):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(base64_image_max_length=MAX))
    span = tracer.start_span("llm")
    span.add_event("request", {"request.body": json.dumps({"url": data_url(MAX + 1)})})
    try:
        raise ValueError(f"400 invalid image {data_url(MAX + 1)}")
    except ValueError as error:
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
    span.end()
    (exported_span,) = exporter.get_finished_spans()
    assert_no_image(exported_span)
    assert exported_span.status.description == f"400 invalid image {REDACTED_VALUE}"
    events = [e for e in exported_span.events if e.name == "exception"]
    assert len(events) == 1, f"{len(events)} exception events"
    (event,) = events
    assert event.attributes["exception.type"] == "ValueError"
    assert event.attributes["exception.escaped"] == "False"


def test_unparseable_json_is_scanned_and_fails_closed(export_with):
    def truncated(n):
        return '{"messages":[{"content":[{"image_url":{"url":"' + data_url(n)

    assert_no_image(export_with({"input.value": truncated(MAX + 1)}))
    assert export_with({"input.value": truncated(MAX)}).attributes["input.value"] == truncated(MAX)


@pytest.mark.parametrize(
    "build",
    [
        lambda: "data:image/png;base64," * 47_662,
        lambda: "data:image/" + ";a" * 500_000,
        lambda: "QUJD" * 250_000 + "!",
        lambda: "[" * 1_000_000,
        lambda: "[" + '"a",' * 250_000 + '"a"]',
        lambda: "data:image\\/png;base64," + "\\/" * 500_000,
    ],
    ids=["headers", "parameter run", "bad last char", "deep unparseable", "long array", "escaped slashes"],
)
def test_linear_on_1_mb_values(build):
    import time

    from fi_instrumentation.instrumentation._image_cap import cap_base64_images

    value = build()
    best = min(_cpu(lambda: cap_base64_images(value, MAX, REDACTED_VALUE)) for _ in range(3))
    # Generous: linear code needs tens of ms per MB here, quadratic code minutes
    assert best < 2.0, best


def _cpu(fn):
    import time

    start = time.process_time()
    fn()
    return time.process_time() - start


def test_runs_before_pii_redaction(export_with):
    key = f"{MSG}.0.message.contents.0.message_content.image"
    value = f"data:image/png;base64,{PNG_HEAD}4111111111111111{'A' * MAX}"
    assert export_with({key: value}, pii_redaction=True).attributes[key] == REDACTED_VALUE


@pytest.mark.parametrize(
    "attributes",
    [
        {f"{MSG}.0.message.contents.1.message_content.image": "https://example.com/" + "a" * (2 * MAX)},
        {"input.value": json.dumps({"input_audio": {"format": "wav", "data": base64_of(2 * MAX)}})},
        {"input.value": "lorem ipsum " * MAX},
        {"input.value": "data:image/svg+xml," + "%3Csvg%3E" * MAX},
    ],
    ids=["https url", "audio base64", "text", "non-base64 data url"],
)
def test_not_capped(export_with, attributes):
    assert dict(export_with(attributes).attributes) == attributes


def test_hide_input_images_still_removes_images(export_with):
    key = f"{MSG}.0.message.contents.1.message_content.image"
    span = export_with({key: data_url(MAX - 100)}, hide_input_images=True)
    assert key not in span.attributes


def test_default_limit_is_32000(export_with):
    key = f"{MSG}.0.message.contents.1.message_content.image"
    assert DEFAULT_BASE64_IMAGE_MAX_LENGTH == 0
    # Retain the old boundary inputs; both now redact with the omitted cap.
    at = data_url(32_000)
    assert export_with({key: at}, base64_image_max_length=None).attributes[key] == "[image-redacted]"
    over = data_url(32_001)
    assert export_with({key: over}, base64_image_max_length=None).attributes[key] == "[image-redacted]"


def test_limit_from_the_environment(export_with, monkeypatch):
    monkeypatch.setenv(FI_BASE64_IMAGE_MAX_LENGTH, str(MAX))
    key = "custom.image"
    assert export_with({key: data_url(MAX + 1)}, base64_image_max_length=None).attributes[key] == REDACTED_VALUE
    assert export_with({key: data_url(MAX)}, base64_image_max_length=None).attributes[key] == data_url(MAX)
