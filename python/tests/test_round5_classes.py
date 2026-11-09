"""Round 5b additive controls. Authored RED-first; execution is a separate gate."""

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags

from fi_instrumentation.instrumentation import _image_cap
from fi_instrumentation.instrumentation._errors import ExceptionSummary, clean_error_description
from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig
from fi_instrumentation.instrumentation.pii_redaction import redact_pii_in_string


MARK = "__REDACTED__"
B = chr(92)
BODY = "QUJD" * 40
HEADERS = (
    'data:image/png;name="a' + B + '"b";base64,',
    'data:image/png;name="a' + B * 2 + '";base64,',
)
LOSSLESS = '{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"lone":"' + B + 'uD800","value":'


def cap(value, maximum=64):
    return _image_cap.cap_base64_images(value, maximum, MARK)


def transport(value, kind):
    if kind == "plain":
        return "prefix " + value + " suffix"
    if kind == "bracket":
        return "[photo](" + value + ")"
    if kind == "string":
        return json.dumps(value)
    document = LOSSLESS + json.dumps(value) + '} '
    if kind == "object":
        return document
    assert kind == "nested"
    return '{"opaque":' + json.dumps(document) + ',"outside":"keep"}'


def assert_routes(value, expected, maximum=64, pii=False, stacktrace_expected=None):
    cfg = TraceConfig(base64_image_max_length=maximum, pii_redaction=pii, hide_error_details=False)
    assert cfg.redact_value(value) == expected
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=cfg)
    context = SpanContext(trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(1))
    try:
        span = tracer.start_span("round5b", attributes={"creation": value}, links=[Link(context, attributes={"link": value})])
        span.set_attribute("setter", value)
        span.add_event("round5b", {"event": value})
        span.record_exception(ValueError(value))
        span.set_status(Status(StatusCode.ERROR, value))
        span.end()
        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        result = finished[0]
        assert result.attributes["creation"] == expected
        assert result.attributes["setter"] == expected
        assert result.links[0].attributes["link"] == expected
        events = [event for event in result.events if event.name == "round5b"]
        assert len(events) == 1
        assert events[0].attributes["event"] == expected
        errors = [event for event in result.events if event.name == "exception"]
        assert len(errors) == 1
        assert errors[0].attributes["exception.message"] == expected
        assert errors[0].attributes["exception.stacktrace"] == (
            "ValueError: " + expected + "\n" if stacktrace_expected is None else stacktrace_expected
        )
        assert result.status.status_code == StatusCode.ERROR
        assert result.status.description == expected
    finally:
        provider.shutdown()


@pytest.mark.parametrize("header", HEADERS, ids=("escaped-quote", "escaped-backslash"))
@pytest.mark.parametrize("kind", ("plain", "bracket", "string", "object", "nested"))
@pytest.mark.parametrize("delta", (-1, 0, 1), ids=("over", "exact", "below"))
def test_mime_quoted_pair_routes(header, kind, delta):
    url = header + BODY
    expected = transport(MARK if delta < 0 else url, kind)
    stacktrace_expected = None
    if kind == "nested" and header == HEADERS[1]:
        stacktrace_expected = "[image-redacted]"
    elif kind not in ("plain", "bracket"):
        stacktrace_expected = "ValueError: " + transport(MARK, kind) + "\n"
    if kind == "plain" and delta >= 0:
        expected = "[image-redacted]"
        stacktrace_expected = "[image-redacted]"
    assert_routes(transport(url, kind), expected, len(url) + delta, stacktrace_expected=stacktrace_expected)


@pytest.mark.parametrize("prefix", (B + "u0064ata:image/", "data:image" + B + "/"))
@pytest.mark.parametrize("kind", ("plain", "bracket", "string", "object", "nested"))
def test_transport_revealed_prefix_preserves_mime_ownership(prefix, kind):
    url = prefix + 'png;name="a' + B + '" b";base64,' + BODY
    expected = "[image-redacted]" if kind in ("plain", "bracket") else transport("[image-redacted]", kind)
    assert_routes(transport(url, kind), expected, stacktrace_expected="[image-redacted]")


@pytest.mark.parametrize("header", (
    'data:image/png;name="x;base64;y",',
    'data:image/png;name="x' + B + '";base64;y",',
    'data:image/png;base64suffix,',
    'data:image/png;base64=ordinary,',
    'data:image/png;name=base64,',
))
def test_false_encoding_flags_with_alphabetic_body(header):
    value = "prefix " + header + BODY + " suffix"
    assert_routes(value, "[image-redacted]", 0, stacktrace_expected="[image-redacted]")


def test_transport_revealed_false_flag_has_no_payload_run():
    value = "prefix " + B + 'u0064ata:image/svg+xml;name="a' + B + '";base64,",%3Csvg%3E suffix'
    assert_routes(value, "[image-redacted]", 0, stacktrace_expected="[image-redacted]")


@pytest.mark.parametrize("depth", (0, 1, 3))
@pytest.mark.parametrize("delta", (-1, 0, 1))
@pytest.mark.parametrize("escaped", (False, True))
def test_member_name_urls_keep_original_lexemes(depth, delta, escaped):
    url = "data:image/png;base64," + BODY
    encoded = url.replace("d", B + "u0064", 1).replace("/", B + "/").replace("Q", B + "u0051") if escaped else url
    raw = '{"' + encoded + '":0,"ordinary":"keep","n":1e400,"same":1,"same":2}'
    expected = '{"__REDACTED__":0,"ordinary":"keep","n":1e400,"same":1,"same":2}' if delta < 0 else raw
    for _ in range(depth):
        raw, expected = json.dumps(raw), json.dumps(expected)
    assert_routes(raw, expected, len(url) + delta, stacktrace_expected="[image-redacted]" if escaped else None)


def test_ordinary_member_names_never_inherit_payload_context():
    raw = '{"type":"image","' + BODY + '":"keep","data":"' + BODY + '","note":"retain"}'
    expected = '{"type":"image","' + BODY + '":"keep","data":"__REDACTED__","note":"retain"}'
    assert_routes(raw, expected)


def test_escaped_schema_key_still_declares_payload_context():
    raw = '{"t' + B + 'u0079pe":"image","d' + B + 'u0061ta":"' + BODY + '","note":"keep"}'
    expected = '{"t' + B + 'u0079pe":"image","d' + B + 'u0061ta":"__REDACTED__","note":"keep"}'
    assert_routes(raw, expected)


@pytest.mark.parametrize("control", range(32))
def test_raw_json_controls_damage_declared_image_payload(control):
    payload = BODY + chr(control) + BODY
    raw = '{"type":"image","data":"' + payload + '","note":"keep"}'
    assert cap(raw) == '{"type":"image","data":"__REDACTED__","note":"keep"}'
    assert cap(raw, len(payload)) == raw
    ordinary = '{"type":"text","data":"' + payload + '","note":"keep"}'
    assert cap(ordinary) == ordinary


@pytest.mark.parametrize("control", (0, 9, 13))
def test_raw_control_damage_exported(control):
    raw = '{"type":"image","data":"' + BODY + chr(control) + BODY + '","note":"keep"}'
    assert_routes(raw, '{"type":"image","data":"__REDACTED__","note":"keep"}')


@pytest.mark.parametrize("escaped", (B + "t", B + "b", B + "u0000"))
def test_valid_escaped_non_base64_controls_are_not_raw_damage(escaped):
    raw = '{"type":"image","data":"' + BODY + escaped + BODY + '","note":"keep"}'
    assert cap(raw) == raw


@pytest.mark.parametrize("body", (BODY, BODY.replace("Q", B + "u0051"), "QUJD" + B + "r" + B + "n" + BODY))
def test_quote_agnostic_backstop_covers_malformed_mime(body):
    raw = 'prefix data:image/png;name="a" b";base64,' + body + ' suffix'
    expected = "[image-redacted]" if body.startswith("QUJD" + B + "r") else 'prefix __REDACTED__ suffix'
    assert_routes(raw, expected, stacktrace_expected="[image-redacted]" if expected == "[image-redacted]" else None)


@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_backstop_malformed_header_boundary(delta):
    url = 'data:image/png;name="a" b";base64,' + BODY
    expected = 'prefix __REDACTED__ suffix' if delta < 0 else '[image-redacted]'
    assert_routes('prefix ' + url + ' suffix', expected, len(url) + delta, stacktrace_expected='[image-redacted]' if delta >= 0 else None)


@pytest.mark.parametrize("delta", (-1, 0, 1))
@pytest.mark.parametrize("kind", ("plain", "object", "nested"))
def test_stable_backstop_counts_mime_pair_at_one_unit_boundary(delta, kind):
    url = 'data:image/png;name="a' + B + '" b";oops "broken";base64,' + BODY
    expected = transport(MARK if delta < 0 else url, kind)
    stacktrace_expected = None if kind == "plain" else "ValueError: " + transport(MARK, kind) + "\n"
    if kind == "plain" and delta >= 0:
        expected = "[image-redacted]"
        stacktrace_expected = "[image-redacted]"
    assert_routes(transport(url, kind), expected, len(url) + delta, stacktrace_expected=stacktrace_expected)


@pytest.mark.parametrize("depth", (1, 2, 4))
def test_raw_backstop_does_not_count_transport_inflation(depth):
    url = HEADERS[0] + BODY
    raw = json.dumps(url)
    for _ in range(depth):
        raw = json.dumps(raw)
    assert cap(raw, len(url)) == raw
    assert cap(raw, len(url) + 1) == raw


def test_backstop_requires_a_payload_not_only_an_oversized_header():
    raw = 'data:image/png;name="' + "a" * 80 + '" broken;base64,",%3Csvg%3E'
    assert cap(raw, 0) == "[image-redacted]"


def test_backstop_preserves_protected_unfinished_object_partition():
    # Known specification conflict: a fully quote-agnostic RAW backstop joins
    # these two distinct tokens. Keep this expectation visible, not weakened.
    raw = '{"data:image/png;name=":";base64,QUJD"'
    assert cap(raw, 0) == "[image-redacted]"


def test_backstop_linear_overlap_control_small_first(monkeypatch):
    scan_unit = getattr(_image_cap, "_backstop_unit", None)
    assert callable(scan_unit), "the raw/decoded supplemental scan must exist"
    consumed = 0

    def counted(text, index, stop):
        nonlocal consumed
        char, end = scan_unit(text, index, stop)
        consumed += end - index
        return char, end

    monkeypatch.setattr(_image_cap, "_backstop_unit", counted)
    for n in (1, 8, 64, 512):
        raw = "data:image/png;" * n + 'name="a" b";base64,' + BODY + " suffix"
        consumed = 0
        result = cap(raw)
        assert result.endswith("__REDACTED__ suffix")
        assert consumed <= 8 * len(raw)


class CountedDescription(str):
    """Fail before a native comparison would exceed linear comparison volume."""

    def __new__(cls, value, allowance):
        instance = super().__new__(cls, value)
        instance.allowance = allowance
        instance.volume = 0
        return instance

    def startswith(self, prefix, *args):
        self.volume += len(prefix)
        assert self.volume <= self.allowance, "unbounded full-message retries"
        return super().startswith(prefix, *args)


def test_error_description_small_first_structural_resource_control():
    for n in (1, 8, 128, 512):
        message = "\n" + "EchoError: \n" * n + "tail"
        raw = "EchoError: " + message + "\nEchoError: " + message
        summary = ExceptionSummary(("EchoError",), message)
        counted = CountedDescription(raw, 32 * (len(raw) + len(message) + len("EchoError")))
        expected = (message + "\n" + message).strip("\n") if n < 128 else raw
        assert clean_error_description(counted, [summary]) == expected


@pytest.mark.parametrize("description,summaries,expected", (
    ("ValueError: boom", [ExceptionSummary(("ValueError",), "boom")], "boom"),
    ("ValueError: KeyError: k", [ExceptionSummary(("ValueError",), "KeyError: k"), ExceptionSummary(("KeyError",), "k")], "KeyError: k"),
    ("ValueError: boom and more", [ExceptionSummary(("ValueError",), "boom")], "ValueError: boom and more"),
    ("see ValueError: boom", [ExceptionSummary(("ValueError",), "boom")], "see ValueError: boom"),
    ("ValueError: first\nOther: third\nline", [ExceptionSummary(("ValueError",), "first"), ExceptionSummary(("Other",), "third\nline")], "first\nthird\nline"),
    ("ValueError: ", [ExceptionSummary(("ValueError",), None)], None),
    ("ValueError:", [ExceptionSummary(("ValueError",), None)], None),
    ("keep Traceback (most recent call last):\nprivate-path", [], "keep"),
))
def test_error_description_exact_ownership(description, summaries, expected):
    assert clean_error_description(description, summaries) == expected


def test_error_many_summary_work_is_bounded_small_first():
    for count in (1, 4, 256):
        summaries = [ExceptionSummary(("E" + str(i),), "m" + str(i)) for i in range(count)]
        raw = "\n".join(summary.type_names[0] + ": " + summary.message for summary in summaries)
        units = len(raw) + sum(len(s.message) + len(s.type_names[0]) + 2 for s in summaries)
        counted = CountedDescription(raw, 32 * units)
        expected = "\n".join(s.message for s in summaries) if count < 256 else raw
        assert clean_error_description(counted, summaries) == expected


def test_error_exhaustion_still_removes_traceback():
    message = "\n" + "EchoError: \n" * 512 + "tail"
    raw = "EchoError: " + message + "\nEchoError: " + message + "\nTraceback (most recent call last):\nprivate-path"
    assert clean_error_description(raw, [ExceptionSummary(("EchoError",), message)]) == message


def test_error_resource_case_real_exporter():
    class EchoError(Exception):
        pass

    message = "\n" + "EchoError: \n" * 512 + "tail"
    raw = "EchoError: " + message + "\nEchoError: " + message
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(pii_redaction=False, hide_error_details=False))
    try:
        span = tracer.start_span("error-resource")
        span.record_exception(EchoError(message))
        span.set_status(Status(StatusCode.ERROR, raw))
        span.end()
        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        assert finished[0].status.description == raw
    finally:
        provider.shutdown()


@pytest.mark.parametrize("text,expected", (
    ("5551234567", "<PHONE_NUMBER>"),
    ("15551234567", "<PHONE_NUMBER>"),
    ("(555)1234567", "<PHONE_NUMBER>"),
    ("+1(555)1234567", "<PHONE_NUMBER>"),
    ("555 123 4567", "<PHONE_NUMBER>"),
    ("123-45-6789", "<SSN>"),
    ("4111 1111 1111 1111", "<CREDIT_CARD>"),
    ("192.168.1.1", "<IP_ADDRESS>"),
    ("sk_live_abcdefghijklmnopqrst", "<API_KEY>"),
    ("alice@example.com", "<EMAIL_ADDRESS>"),
    ("ordinary prose", "ordinary prose"),
))
def test_pii_admission_census(text, expected):
    assert redact_pii_in_string(text) == expected
    assert_routes(text, expected, pii=True)
    assert TraceConfig(pii_redaction=False).redact_value(text) == text
