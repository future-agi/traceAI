"""New assertion regressions against standalone 795e04ad, using real SDK exports.
No new-module import is needed for RED; the existing FITracer/config paths suffice.
"""
import json
import time
from decimal import Decimal

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags

from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig

MAX = 256
MARK = "__REDACTED__"
PREFIX = "data:image/png;base64,"
PAYLOAD = "QUJD" * 500 + "TAILPAYLOAD"
BIG = PREFIX + PAYLOAD
EMAIL = "jane.doe@example.com"


def at(length):
    return PREFIX + "A" * (length - len(PREFIX))


@pytest.fixture
def sdk(clean_env):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def tracer(**options):
        defaults = dict(base64_image_max_length=MAX, pii_redaction=False, hide_error_details=False)
        defaults.update(options)
        return FITracer(provider.get_tracer(__name__), config=TraceConfig(**defaults))

    return exporter, provider, tracer


def one(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def value_export(sdk, value, **options):
    exporter, _, tracer = sdk
    exporter.clear()
    caller = {"input.value": value}
    span = tracer(**options).start_span("case", attributes=caller)
    span.set_attribute("later", value)
    span.end()
    result = one(exporter)
    assert result.attributes["later"] == result.attributes["input.value"]
    assert caller == {"input.value": value}
    return result.attributes["input.value"]


def surfaces(sdk, value, expected, **options):
    exporter, _, tracer = sdk
    exporter.clear()
    caller = {"input.value": value}
    event = {"value": value}
    error = ValueError(value)
    args = error.args
    span = tracer(**options).start_span("case", attributes=caller)
    span.set_attribute("later", value)
    span.add_event("request", event, timestamp=1000000001)
    span.record_exception(error, timestamp=1000000002, escaped=True)
    span.set_status(Status(StatusCode.ERROR, value))
    span.end()
    result = one(exporter)
    assert result.attributes["input.value"] == expected
    assert result.attributes["later"] == expected
    (request,) = [e for e in result.events if e.name == "request"]
    assert request.attributes["value"] == expected
    assert request.timestamp == 1000000001
    exceptions = [e for e in result.events if e.name == "exception"]
    assert len(exceptions) == 1
    exception = exceptions[0]
    assert exception.attributes["exception.message"] == expected
    assert exception.attributes["exception.escaped"] == "True"
    assert exception.timestamp == 1000000002
    assert result.status.description == expected
    if "TAILPAYLOAD" not in expected:
        assert "TAILPAYLOAD" not in exception.attributes["exception.stacktrace"]
    assert error.args == args
    assert caller == {"input.value": value}
    assert event == {"value": value}


def wrapped(first, separator):
    rest = [PAYLOAD[i:i + 76] for i in range(first, len(PAYLOAD), 76)]
    return PREFIX + separator.join([PAYLOAD[:first], *rest])


URL_CASES = [
    "data:image/png;name=" + "a" * 600 + ";base64," + PAYLOAD,
    wrapped(8, "\n"), wrapped(8, "\r\n"), wrapped(600, "\n"),
    'data:image/png;name="' + "a" * 600 + '";base64,' + PAYLOAD,
    "data:image\\/png;base64," + PAYLOAD,
]
URL_IDS = ["long-header", "LF-short-first", "CRLF-short-first", "LF-long-first", "quoted-header", "escaped-prose"]


@pytest.mark.parametrize("url", URL_CASES, ids=URL_IDS)
def test_boundaries(sdk, url):
    for prefix in ("400 invalid image ", ""):
        expected = "[image-redacted]" if url in (*URL_CASES[1:4], URL_CASES[-1]) else prefix + MARK + " caption retained"
        surfaces(sdk, prefix + url + " caption retained", expected)


@pytest.mark.parametrize("url", URL_CASES, ids=URL_IDS)
def test_boundary_exact_control(sdk, url):
    surfaces(sdk, url, url, base64_image_max_length=len(url))


@pytest.mark.parametrize("length", [MAX - 1, MAX, MAX + 1])
def test_caption_occurrence(sdk, length):
    url = at(length)
    caption = " caption " + "diagnostic prose " * 300
    for prefix in ("", "image: "):
        surfaces(sdk, prefix + url + caption, "[image-redacted]")


@pytest.mark.parametrize("depth", [63, 64, 65])
def test_object_depth(sdk, depth):
    deep = "[" * depth + json.dumps(BIG) + "]" * depth
    raw = '{"deep":' + deep + ',"ordinary":"keep","shallow":' + json.dumps(BIG) + '}'
    assert value_export(sdk, raw) == raw.replace(BIG, MARK)


@pytest.mark.parametrize("depth", [7, 8, 9])
def test_encoded_depth(sdk, depth):
    raw = '{"data":"' + PAYLOAD + '","media_type":"image/png"}'
    expected = '{"data":"' + MARK + '","media_type":"image/png"}'
    for _ in range(depth):
        raw, expected = json.dumps(raw), json.dumps(expected)
    result = value_export(sdk, raw)
    assert "TAILPAYLOAD" not in result
    assert result == expected


@pytest.mark.parametrize("kind", ["complete", "missing-close", "unterminated-string"])
def test_escaped_declared(sdk, kind):
    stem = '{"\\u006dedia_type":"image\\/png","d\\u0061ta":"'
    suffix = '"}' if kind == "complete" else '"' if kind == "missing-close" else ''
    assert value_export(sdk, stem + PAYLOAD + suffix) == stem + MARK + suffix


@pytest.mark.parametrize("kind", ["invalid-escape", "partial-escape"])
def test_damaged_payload_token(sdk, kind):
    stem = '{"media_type":"image/png","note":"ordinary sibling","data":"'
    damage = "\\q" if kind == "invalid-escape" else "\\u0"
    suffix = '"}' if kind == "invalid-escape" else ''
    assert value_export(sdk, stem + PAYLOAD + damage + suffix) == stem + MARK + suffix
    ordinary = '{"type":"image","name":"' + "ordinary prose " * 100
    assert value_export(sdk, ordinary) == ordinary


def test_lossless_numeric_and_escape_tokens(sdk):
    raw = '{ "id":9007199254740993, "n":1e400,"same":1,"same":2,"s":"\\uD800","esc":"\\u0061\\/b","url":' + json.dumps(BIG) + '}'
    result = value_export(sdk, raw)
    assert result == raw.replace(BIG, MARK)
    def invalid_constant(value):
        raise AssertionError("non-JSON number " + value)
    parsed = json.loads(result, parse_float=Decimal, parse_constant=invalid_constant)
    assert parsed["id"] == 9007199254740993
    assert parsed["n"] == Decimal("1e400")
    result.encode("utf-8")  # the raw lone-surrogate escape remains ASCII


def test_deep_ordinary_sibling(sdk):
    deep = "[" * 6000 + '"keep"' + "]" * 6000
    raw = '{"deep":' + deep + ',"url":' + json.dumps(BIG) + '}'
    assert value_export(sdk, raw) == raw.replace(BIG, MARK)


@pytest.mark.parametrize("repeats", [64, 65])
def test_escaped_payload_boundary(sdk, repeats):
    payload = "\\u0051\\u0055\\u004a\\u0044" * repeats
    raw = '{"media_type":"image/png","data":"' + payload + '"}'
    assert value_export(sdk, raw) == (raw.replace(payload, MARK) if repeats * 4 > MAX else raw)


def test_lossless_exact_control(sdk):
    raw = '{"id":9007199254740993,"n":1e400,"url":' + json.dumps(at(MAX)) + '}'
    assert value_export(sdk, raw) == raw


@pytest.mark.parametrize("maximum", [0, 1, 4])
def test_schema_metadata(sdk, maximum):
    raw = '{"type":"image","modality":"image","id":"QUJD","name":"image","schema":"base64","other":"QUJD","source":{"type":"base64","media_type":"image/png","data":"QUJD"}}'
    expected = raw.replace('"data":"QUJD"', '"data":"' + ("[image-redacted]" if maximum == 0 else MARK) + '"') if maximum < 4 else raw
    result = value_export(sdk, raw, base64_image_max_length=maximum)
    parsed = json.loads(result)
    assert parsed["type"] == "image"
    assert parsed["source"]["type"] == "base64"
    assert parsed["id"] == "QUJD"
    assert result == expected


def test_source_inherits_declaration_not_ordinary_siblings(sdk):
    raw = '{"source":{"type":"base64","data":"' + PAYLOAD + '"},"type":"image","name":"' + PAYLOAD + '"}'
    assert value_export(sdk, raw) == raw.replace('"data":"' + PAYLOAD + '"', '"data":"' + MARK + '"')


@pytest.mark.parametrize("maximum", [0, 1, 4])
def test_flattened_schema_metadata(sdk, maximum):
    exporter, _, tracer = sdk
    prefix = "gen_ai.input.messages.0.message.contents.0.message_content.image."
    metadata = {"type": "image", "modality": "image", "mime_type": "image/png", "id": "QUJD", "name": "image", "schema": "base64", "other": "QUJD"}
    caller = {prefix + key: value for key, value in metadata.items()}
    caller[prefix + "data"] = "QUJD"
    span = tracer(base64_image_max_length=maximum).start_span("case", attributes=caller)
    span.end()
    result = one(exporter)
    for key, value in metadata.items():
        assert result.attributes[prefix + key] == value
    assert result.attributes[prefix + "data"] == ("[image-redacted]" if maximum == 0 else MARK if maximum < 4 else "QUJD")
    assert caller[prefix + "data"] == "QUJD"


def test_audio_and_unknown_alphabet_are_not_images(sdk):
    raw = '{"type":"audio","source":{"type":"base64","data":"' + PAYLOAD + '"}}'
    assert value_export(sdk, raw, base64_image_max_length=0) == raw


@pytest.mark.parametrize("boolean", [False, True])
def test_boolean_cap_rejected_over_env(clean_env, monkeypatch, boolean):
    monkeypatch.setenv("FI_BASE64_IMAGE_MAX_LENGTH", "90000")
    config = TraceConfig(base64_image_max_length=boolean)
    assert config.base64_image_max_length == 0
    assert config.redact_value(BIG) == "[image-redacted]"


@pytest.mark.parametrize("maximum", [0, 1])
def test_integer_cap_controls_and_precedence(sdk, monkeypatch, maximum):
    monkeypatch.setenv("FI_BASE64_IMAGE_MAX_LENGTH", "90000")
    assert value_export(sdk, BIG, base64_image_max_length=maximum) == ("[image-redacted]" if maximum == 0 else MARK)
    config = TraceConfig(base64_image_max_length=maximum)
    assert config.base64_image_max_length == maximum


def test_negative_cap_is_not_unlimited(clean_env, monkeypatch):
    config = TraceConfig(base64_image_max_length=-1)
    assert config.base64_image_max_length == 0
    assert config.redact_value(BIG) == "[image-redacted]"
    monkeypatch.setenv("FI_BASE64_IMAGE_MAX_LENGTH", "-1")
    config = TraceConfig()
    assert config.base64_image_max_length == 0
    assert config.redact_value(BIG) == "[image-redacted]"


@pytest.mark.parametrize("container", [tuple, list], ids=["tuple", "list"])
@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
def test_sequence_pii_all_surfaces(sdk, container, pii):
    exporter, _, tracer = sdk
    original = container([EMAIL, "ordinary"])
    caller = {"users": original}
    ctx = SpanContext(trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(1))
    link = Link(ctx, attributes=caller)
    span = tracer(pii_redaction=pii).start_span("case", attributes=caller, links=[link])
    span.set_attribute("later", original)
    span.add_event("request", caller)
    span.end()
    result = one(exporter)
    expected = ("<EMAIL_ADDRESS>" if pii else EMAIL, "ordinary")
    assert tuple(result.attributes["users"]) == expected
    assert tuple(result.attributes["later"]) == expected
    assert tuple(result.events[0].attributes["users"]) == expected
    assert tuple(result.links[0].attributes["users"]) == expected
    assert original == container([EMAIL, "ordinary"])
    assert caller["users"] is original
    masked = TraceConfig(pii_redaction=pii).redact_value(original)
    assert type(masked) is container
    assert tuple(masked) == expected
    assert tuple(link.attributes["users"]) == (EMAIL, "ordinary")


def test_stronger_hide_is_independent(sdk):
    exporter, _, tracer = sdk
    key = "gen_ai.input.messages.0.message.contents.0.message_content.image"
    span = tracer(hide_input_images=True).start_span("case", attributes={key: at(MAX)})
    span.end()
    assert key not in one(exporter).attributes


@pytest.mark.parametrize("pii", [False, True], ids=["off", "on"])
def test_full_path_no_at_prose_resource_control(sdk, pii):
    start = time.process_time()
    for size in (16384, 65536):
        text = "ordinary prose " + "a" * size
        surfaces(sdk, text, text, pii_redaction=pii)
    assert time.process_time() - start < 5.0
