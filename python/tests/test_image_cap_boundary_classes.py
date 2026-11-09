"""Boundary-class controls; run RED on the pinned base before applying the fix."""

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags

from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig


MARK = "__REDACTED__"
URL = 'data:image/png;name="sample";base64,' + "QUJD" * 40
PREFIXES = [
    ("bracket", "[", "]"),
    ("markdown", "[photo](", ")"),
    ("brace", "{photo}(", ")"),
    ("quote", '"photo"(', ")"),
    ("open-quote", '"photo (', ")"),
    ("ordinary", "diagnostic ", " caption retained"),
]
TRANSPORTS = ("direct", "string", "object", "nested")
LOSSLESS_PREFIX = '{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"lone":"\\uD800","esc":"\\u0061\\/b","value":'


def transport(value, kind, ensure_ascii=True):
    if kind == "direct":
        return value
    quoted = json.dumps(value, ensure_ascii=ensure_ascii)
    if kind == "string":
        return quoted
    document = LOSSLESS_PREFIX + quoted + "}"
    if kind == "object":
        return document
    assert kind == "nested"
    return '{"opaque":' + json.dumps(document, ensure_ascii=ensure_ascii) + ',"outside":"keep"}'


def config(maximum):
    return TraceConfig(
        base64_image_max_length=maximum,
        pii_redaction=False,
        hide_error_details=False,
    )


def assert_routes(value, expected, maximum, stacktrace_expected=None):
    cfg = config(maximum)
    assert cfg.redact_value(value) == expected
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=cfg)
    context = SpanContext(
        trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(1)
    )
    try:
        span = tracer.start_span(
            "image-boundary",
            attributes={"boundary.creation": value},
            links=[Link(context, attributes={"boundary.link": value})],
        )
        span.set_attribute("boundary.setter", value)
        span.add_event("boundary", attributes={"boundary.event": value})
        span.record_exception(ValueError(value))
        span.set_status(Status(StatusCode.ERROR, value))
        span.end()
        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        exported = finished[0]
        assert exported.attributes["boundary.creation"] == expected
        assert exported.attributes["boundary.setter"] == expected
        assert len(exported.links) == 1
        assert exported.links[0].attributes["boundary.link"] == expected
        event = next(event for event in exported.events if event.name == "boundary")
        assert event.attributes["boundary.event"] == expected
        exceptions = [event for event in exported.events if event.name == "exception"]
        assert len(exceptions) == 1
        assert exceptions[0].attributes["exception.message"] == expected
        assert exceptions[0].attributes["exception.stacktrace"].endswith(
            stacktrace_expected if stacktrace_expected is not None else "ValueError: " + expected + "\n"
        )
        assert exported.status.status_code == StatusCode.ERROR
        assert exported.status.description == expected
    finally:
        provider.shutdown()


BOUNDARY_CASES = [
    pytest.param(prefix, suffix, kind, delta, id=f"{name}-{kind}-{label}")
    for name, prefix, suffix in PREFIXES
    for kind in TRANSPORTS
    for label, delta in (("over", -1), ("exact", 0), ("below", 1))
]


@pytest.mark.parametrize("prefix,suffix,kind,delta", BOUNDARY_CASES)
def test_json_prefix_routes(prefix, suffix, kind, delta):
    value = transport(prefix + URL + suffix, kind)
    expected = transport(prefix + (MARK if delta < 0 else URL) + suffix, kind)
    stacktrace_expected = None if kind == "direct" else "ValueError: " + transport(prefix + MARK + suffix, kind) + "\n"
    if prefix == "diagnostic " and delta >= 0:
        expected = transport("[image-redacted]", kind)
        if kind == "direct":
            stacktrace_expected = "[image-redacted]"
    assert_routes(value, expected, len(URL) + delta, stacktrace_expected=stacktrace_expected)


FALSE_FLAGS = (
    "name=base64",
    "name=prebase64post",
    'name="base64"',
    'name="x;base64;y"',
    "name='x;BASE64;y'",
    "base64=ordinary",
    "base64suffix",
    'name="x,;base64;y"',
    'name="x\\\";base64;y"',
)
NON_BASE64_BODY = "%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%2F%3E"


@pytest.mark.parametrize("parameter", FALSE_FLAGS, ids=[f"parameter-{i}" for i in range(len(FALSE_FLAGS))])
@pytest.mark.parametrize("maximum", (0, 1))
def test_parameter_is_not_encoding_flag(parameter, maximum):
    value = "data:image/svg+xml;" + parameter + "," + NON_BASE64_BODY
    assert_routes(value, "[image-redacted]" if maximum == 0 else value, maximum,
                  stacktrace_expected="[image-redacted]" if maximum == 0 else None)


TRUE_HEADERS = (
    "data:image/png;base64,",
    'DATA:IMAGE/PNG;name="a,b;c";BaSe64,',
    "data:image/png;BASE64;name=ordinary,",
    "data:image/png;name='a;b,c';base64,",
    'data:image/png;name="' + "q" * 4096 + '";base64,',
)


@pytest.mark.parametrize("header", TRUE_HEADERS, ids=[f"header-{i}" for i in range(len(TRUE_HEADERS))])
@pytest.mark.parametrize("maximum", (0, 1))
def test_genuine_encoding_flag_whole_marker(header, maximum):
    value = "[photo](" + header + "QUJD\r\nQUJD\nQUJD" + ")"
    assert_routes(value, "[image-redacted]", maximum, stacktrace_expected="[image-redacted]")


@pytest.mark.parametrize("maximum", (64, 32000))
@pytest.mark.parametrize("kind", TRANSPORTS)
@pytest.mark.parametrize("ascii_transport", (True, False), ids=("ascii", "literal"))
@pytest.mark.parametrize("delta", (-1, 0, 1), ids=("over", "exact", "below"))
def test_surrogate_pair_python_length(maximum, kind, ascii_transport, delta):
    header = 'data:image/png;name="' + chr(0x1F680) + '";base64,'
    url = header + "A" * (maximum - len(header))
    assert len(url) == maximum
    value = transport("[" + url + "]", kind, ascii_transport)
    expected = transport("[" + (MARK if delta < 0 else url) + "]", kind, ascii_transport)
    assert config(maximum + delta).redact_value(value) == expected


@pytest.mark.parametrize("escaped", ("\\uD800", "\\uDC00", "\\uDC00\\uD800", "\\uD800\\u0061"), ids=("high", "low", "reversed", "high-ascii"))
def test_lone_and_unrelated_surrogates_are_lossless(escaped):
    raw = '{"lone":"' + escaped + '","note":"\\u0061\\/b","image":' + json.dumps(URL) + ',"n":1e400,"same":1,"same":2}'
    expected = raw.replace(json.dumps(URL), json.dumps(MARK))
    assert config(64).redact_value(raw) == expected
    assert config(len(URL)).redact_value(raw) == raw


@pytest.mark.parametrize("pair", ("\\ud83d\\ude80", "\\uD83D\\uDE80"), ids=("lower", "upper"))
def test_surrogate_mapping_with_neighbor_escapes(pair):
    header = 'data:image/png;name="a' + chr(0x1F680) + 'z";base64,'
    url = header + "QUJD" * 40
    encoded = json.dumps(url).replace("\\ud83d\\ude80", pair).replace("QUJD", "\\u0051UJD")
    raw = '{ "before":"\\u0061\\/b", "url":' + encoded + ',"after":"\\uD800"}'
    assert config(len(url)).redact_value(raw) == raw
    assert config(len(url) + 1).redact_value(raw) == raw
    assert config(len(url) - 1).redact_value(raw) == raw.replace(encoded, json.dumps(MARK))


@pytest.mark.parametrize("prefix", ("[photo](", "{photo}(", '"photo"('), ids=("bracket", "brace", "quote"))
def test_raw_escape_transport_not_lexical_header_fragments(prefix):
    escaped = URL.replace("/", "\\/").replace("Q", "\\u0051")
    raw = prefix + escaped + ")"
    assert config(len(URL)).redact_value(raw) == "[image-redacted]"
    assert config(len(URL) - 1).redact_value(raw) == "[image-redacted]"


@pytest.mark.parametrize("raw", (
    '{"type":"image","data":"' + "QUJD" * 40 + '"',
    '{"type":"image","data":"' + "QUJD" * 40,
), ids=("unclosed-object", "unclosed-payload"))
def test_malformed_declared_image_context_is_still_fail_closed(raw):
    payload = "QUJD" * 40
    assert config(64).redact_value(raw) == raw.replace(payload, MARK)


@pytest.mark.parametrize("maximum", (0, 1, 64))
def test_unquoted_raw_gap_escapes_replace_whole_url(maximum):
    url = "data:image/png;base64," + "QUJD" * 40
    escaped = url.replace("Q", "\\u0051")
    raw = "[" + escaped + "]"
    assert config(maximum).redact_value(raw) == ("[image-redacted]" if maximum == 0 else "[" + MARK + "]")
    assert config(len(url)).redact_value(raw) == "[" + MARK + "]"


def delayed(layers):
    return "\\" + "u005c" * layers + "u0078ordinary text " + "z" * 2048


def test_exhaustion_is_whole_scope_and_outer_siblings_are_independent():
    exact = "data:image/png;base64," + "A" * (256 - len("data:image/png;base64,"))
    inner = json.dumps({"expensive": delayed(256), "exact": exact, "id": 9007199254740993})
    raw = json.dumps({"opaque": inner, "outside": "keep", "exact": exact, "affordable": delayed(2)})
    expected_inner = json.dumps({"expensive": "[image-redacted]", "exact": exact, "id": 9007199254740993})
    expected = json.dumps({"opaque": expected_inner, "outside": "keep", "exact": exact, "affordable": delayed(2)})
    assert config(256).redact_value(raw) == expected
    assert config(256).redact_value(delayed(256)) == "[image-redacted]"
    assert config(256).redact_value(delayed(2)) == delayed(2)


def test_ordinary_and_valid_lexical_documents_are_not_dropped():
    cfg = config(0)
    for value in (
        "ordinary " * 100,
        '["ordinary", {"n":1e400,"n":9007199254740993,"lone":"\\uD800"}]',
        '{"caption":"brackets [ and { and quotes \\\" are ordinary","note":"keep"}',
        '"ordinary quoted text"',
        "[not JSON and no image]",
        "{not JSON and no image}",
        '"not JSON and no image',
    ):
        assert cfg.redact_value(value) == value


def test_caller_exception_override_precedence_is_unchanged():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=config(64))
    try:
        span = tracer.start_span("override")
        span.record_exception(ValueError("[" + URL + "]"), attributes={"exception.message": "caller override"})
        span.end()
        events = exporter.get_finished_spans()[0].events
        assert len(events) == 1
        assert events[0].attributes["exception.message"] == "caller override"
        assert URL not in events[0].attributes["exception.stacktrace"]
    finally:
        provider.shutdown()


# Round two is additive: recoverable truncation must not change outer ownership.
@pytest.mark.parametrize("delta", (-1, 0, 1), ids=("over", "exact", "below"))
def test_truncated_document_keeps_quoted_mime_sibling_and_token_budget(delta):
    hidden = "\\" + "u005c" * 256 + "u0064ata:image/png;base64," + "QUJD" * 512 + "CAPR2CANARY"
    damaged = json.dumps(hidden + " suffix " + URL)[1:-1]
    before = LOSSLESS_PREFIX + json.dumps("[" + URL + "]")
    after = ',"affordable":' + json.dumps(delayed(2)) + ',"opaque":"'
    raw = before + after + damaged
    expected = "[image-redacted]"
    assert config(len(URL) + delta).redact_value(raw) == expected


@pytest.mark.parametrize("prefix", ("[photo](", '"photo (', '{"caption":"photo '),
                         ids=("bracket-prose", "quote-prose", "object-prose"))
@pytest.mark.parametrize("name", ("sample", "", "]"), ids=("word", "empty", "punctuation"))
@pytest.mark.parametrize("escaped", (False, True), ids=("raw", "escaped"))
@pytest.mark.parametrize("delta", (-1, 0, 1), ids=("over", "exact", "below"))
def test_truncated_prose_keeps_whole_quoted_mime_url(prefix, name, escaped, delta):
    url = 'data:image/png;name="' + name + '";base64,' + "QUJD" * 40
    encoded = url.replace('"', '\\"').replace("/", "\\/").replace("Q", "\\u0051") if escaped else url
    raw = prefix + encoded
    expected = "[image-redacted]" if escaped else prefix + MARK if delta < 0 else raw
    assert config(len(url) + delta).redact_value(raw) == expected


@pytest.mark.parametrize("prefix,suffix", (
    ('{"ordinary":"retain","opaque":', ''),
    ('{"ordinary":"retain","opaque":', ']'),
    ('{{"ordinary":"retain","opaque":', ']]'),
    ('["retain",', ''),
), ids=("unfinished-object", "mismatched-closer", "repeated-object", "unfinished-array"))
def test_recoverable_shape_retains_independent_exhaustion(prefix, suffix):
    raw = prefix + json.dumps(delayed(256)) + suffix
    expected = "[image-redacted]"
    assert config(256).redact_value(raw) == expected


@pytest.mark.parametrize("prefix", (
    '["retain",bare ',
    '{"ordinary":"retain","opaque":bare ',
    '"label" ',
), ids=("array-prose", "object-prose", "adjacent-root"))
def test_rejected_prose_truncation_still_exhausts_whole_value(prefix):
    raw = prefix + json.dumps(delayed(256)) + " suffix " + URL
    assert config(256).redact_value(raw) == "[image-redacted]"


@pytest.mark.parametrize("kind", ("string", "object", "nested"))
def test_nested_truncated_document_keeps_original_outer_scope(kind):
    inner = '{"ordinary":"retain","opaque":"' + json.dumps(delayed(256) + " suffix " + URL)[1:-1]
    raw = transport(inner, kind)
    expected = transport("[image-redacted]", kind)
    assert config(256).redact_value(raw) == expected


@pytest.mark.parametrize("raw", (
    '["data:image/png;name=", ";base64,QUJD"',
    '{"data:image/png;name=":";base64,QUJD"',
), ids=("unfinished-array", "unfinished-object"))
@pytest.mark.parametrize("maximum", (0, 1, 64))
def test_unfinished_partitions_do_not_fabricate_an_image(raw, maximum):
    if maximum == 0:
        assert config(maximum).redact_value(raw) == "[image-redacted]"
    elif maximum < 64:
        assert config(maximum).redact_value(raw) == raw[:2] + '__REDACTED__"'
    else:
        assert config(maximum).redact_value(raw) == raw
