"""Fail-closed literal-output obligations. Source-authored first; runtime NOT_RUN."""

import json

import pytest

from fi_instrumentation.instrumentation import _image_cap


A = "__REDACTED__"
B = "[image-redacted]"
BS = chr(92)
P = "QUJD" * 40
H = "data:image/png;base64,"


def cap(value, maximum=64, image_key=False):
    return _image_cap.cap_base64_images(value, maximum, A, image_key)


def wrap(value, depth):
    for _ in range(depth):
        value = '{"opaque":' + json.dumps(value) + ',"outside":"keep"}'
    return value


@pytest.mark.parametrize("raw,expected", (
    ("before " + H + P + " after", "before __REDACTED__ after"),
    ('data:image/svg+xml;name="a;base64,' + P + '",%3Csvg%3E', '__REDACTED__",%3Csvg%3E'),
    ('data:image/svg+xml stray;name="a' + BS + '";base64,' + P + '",%3Csvg%3E', '__REDACTED__",%3Csvg%3E'),
    ('data:image/svg+xml; name="a' + BS + '";base64,' + P + '",%3Csvg%3E', '__REDACTED__",%3Csvg%3E'),
    ('data:image/svg+xml;name' + BS + 'u003d"a;base64,' + P + '",%3Csvg%3E', '__REDACTED__",%3Csvg%3E'),
    ('data:image/x stray data:image/svg+xml;name="a;base64,' + P + '",%3Csvg%3E', '__REDACTED__",%3Csvg%3E'),
    ('data:image/png;name="a' + BS + '" b";base64,' + P, '__REDACTED__'),
    ('data:image/png;name="a' + BS * 2 + '";base64,' + P, '__REDACTED__'),
    ("DaTa: \tIMAGE/png, ignored;BaSe64," + P, "__REDACTED__"),
    ('data:image/svg+xml;name="hold data:image/png;name="a;base64,QUJD";base64,' + P + " suffix", "__REDACTED__ suffix"),
    ("data:image/" + "x " * 124 + ";base64," + P, "__REDACTED__"),
))
def test_raw_witness_outputs(raw, expected):
    assert cap(raw) == expected
    assert cap(wrap(raw, 1)) == wrap(expected, 1)


@pytest.mark.parametrize("prefix", (
    BS + "u0064ata:image/png;base64,",
    "data:image" + BS + "/png;base64,",
    "%64ata%3Aimage%2Fpng%3Bbase64%2C",
    "&#100;ata:image/png;base64,",
    "&#x64;ata:image/png;base64,",
    "data&colon;image&sol;png&semi;base64&comma;",
    "da" + chr(0x200B) + "ta:image/png;base64,",
    "data:" + chr(0x2060) + "ima" + chr(0xFEFF) + "ge/png;base64,",
    "%25252564ata:image/png;base64,",
    "%26%23x5c%3Bu0064ata:image/png;base64,",
))
def test_decoded_only_witnesses_plain_and_json(prefix):
    raw = "caption " + prefix + P + " tail"
    assert cap(raw) == "[image-redacted]"
    assert cap('{"value":' + json.dumps(raw) + ',"keep":1}') == '{"value":"[image-redacted]","keep":1}'
    assert cap('{' + json.dumps(raw) + ':0,"keep":1}') == '{"[image-redacted]":0,"keep":1}'


@pytest.mark.parametrize("hidden", (BS + "u0064ata:image/", "data:image" + BS + "/"))
@pytest.mark.parametrize("padding", (120, 520))
@pytest.mark.parametrize("maximum", (64, 194, 195, 196))
def test_hidden_prefix_pairs_use_original_extent(hidden, padding, maximum):
    raw = hidden + "x " * padding + hidden + 'png;name="a' + BS + '" b";base64,' + P
    # The C2 RAW parameter grammar adds the earlier prefix independently of C1.
    assert cap(raw, maximum) == "[image-redacted]"


def test_earlier_open_quote_cannot_shelter_hidden_prefix():
    hidden = BS + "u0064ata:image/"
    raw = hidden + 'png stray;name=",' + hidden + 'png;name="a' + BS + '" b";base64,' + P
    assert cap(raw, 64) == "[image-redacted]"
    assert cap(raw, 194) == "[image-redacted]"


@pytest.mark.parametrize("header", (H, "data:image/png stray;base64,"))
def test_every_flag_and_prefix_is_considered(header):
    assert cap(header + header + P) == "__REDACTED__"


def test_cap25_and_overlap_union_oracles_agree():
    # The broad earlier prefix also reaches the later flag; intervals overlap.
    assert cap(H + "QUJD" + H + P, 25) == "__REDACTED__"
    assert cap(H + "QUJD", 26) == "data:image/png;base64,QUJD"
    assert cap(H + "QUJD", 25) == "__REDACTED__"
    assert cap("caption %64ata:image/png;base64,QUJD tail", 26) == "[image-redacted]"
    assert cap("caption %64ata:image/png;base64,QUJD tail", 28) == "[image-redacted]"


@pytest.mark.parametrize("maximum", (26, 64, 4096, 32000))
def test_raw_exact_and_below_cap(maximum):
    assert cap(H + "QUJD", maximum) == "data:image/png;base64,QUJD"


@pytest.mark.parametrize("raw", (H, 'data:image/svg+xml; name="a' + BS + '";base64,",%3Csvg%3E',
    "prefix " + BS + 'u0064ata:image/svg+xml;name="a' + BS + '";base64,",%3Csvg%3E suffix'))
def test_empty_payload_never_counts(raw):
    assert cap(raw, 0) == "[image-redacted]"


@pytest.mark.parametrize("encoded", (BS + "u0064ata:", "%64ata:", "&#100;ata:"))
def test_adjacent_decoded_header_is_not_hidden_by_partial_edit(encoded):
    raw = H + P + encoded + "image/png;base64," + P
    assert cap(raw) == "[image-redacted]"
    assert cap(wrap(raw, 1)) == wrap("[image-redacted]", 1)
    assert cap('{' + json.dumps(raw) + ':0,"keep":1}') == '{"[image-redacted]":0,"keep":1}'


@pytest.mark.parametrize("invisible", ("%E2%80%8B", "%C2%AD", "%EF%BB%BF"))
def test_utf8_invisible_payload_continuation(invisible):
    raw = H + P + invisible + P + " suffix"
    assert cap(raw) == "[image-redacted]"
    assert cap(wrap(raw, 1)) == wrap("[image-redacted]", 1)


@pytest.mark.parametrize("code", (0x034F, 0x180F, 0xFE00, 0xFE0F, 0xE0100, 0xE01EF))
def test_zero_width_selector_ranges(code):
    assert cap("d" + chr(code) + "ata:image/png;base64," + P) == "[image-redacted]"
    assert cap("data:image/png;ba" + chr(code) + "se64," + P) == "[image-redacted]"
    assert cap(wrap("d" + chr(code) + "ata:image/png;base64," + P, 1)) == wrap("[image-redacted]", 1)


@pytest.mark.parametrize("encoded", (BS + "u0051", "%51", "&#81;", "&#x51;"))
def test_exact_transport_run_endpoints(encoded):
    assert cap("left " + H + encoded * 160 + " right") == "left __REDACTED__ right"


@pytest.mark.parametrize("payload", ("QUJD" + "%41" * 160, "QUJD" + "&#65;" * 160, "QUJD" + "&#x41;" * 160, P + "%2FQUJD"))
def test_hedge_encoded_payload_witnesses(payload):
    assert cap("left " + H + payload + " right") == "left __REDACTED__ right"


def test_trailing_data_letters_and_crlf_are_payload():
    assert cap(H + P + "data: caption") == "__REDACTED__: caption"
    assert cap("left " + H + "QUJD_-+/== right", 0) == "[image-redacted]"
    assert cap("[photo](" + H + "QUJD\r\nQUJD\nQUJD)", 1) == "[image-redacted]"


@pytest.mark.parametrize("header", ("data:image/png;BASE64;name=ordinary,", 'data:image/png;name="' + "q" * 4096 + '";base64,'))
def test_genuine_nonfinal_and_long_headers_stay_positive(header):
    assert cap("prefix " + header + P + " suffix") == "prefix __REDACTED__ suffix"
    assert cap("[photo](" + header + P + ")", 1) == "[photo](__REDACTED__)"
    assert cap('{"type":"image","url":' + json.dumps(header + P) + ',"note":"keep"}', 1) == '{"type":"image","url":"__REDACTED__","note":"keep"}'


@pytest.mark.parametrize("tail", ('png;BASE64;name="a' + BS + '" b",', 'png;name="a' + BS + '" b' + "q" * 300 + '";base64,', 'png;name="a' + BS + '" b' + "q" * 4096 + '";base64,'))
def test_reference_r2_hidden_prefix_positive_tail_regression(tail):
    raw = BS + "u0064ata:image/" + tail + P
    assert cap(raw) == "[image-redacted]"
    assert cap(wrap(raw, 1)) == wrap("[image-redacted]", 1)


@pytest.mark.parametrize("raw", (
    "ordinary prose", "https://example.invalid/a?note=image", "data:audio/wav;base64," + P,
    "data:image/svg+xml,%3Csvg%3E", "data:image/png;name=base64," + P,
    "data:image/png;base64suffix," + P, "data:image/png;base64=ordinary," + P,
    'data:image/png;name="x;base64;y",' + P, "__REDACTED__ [image-redacted] are literal words",
    "ordinary " + BS + "u0061 %62 &#99; " + chr(0x200B), "[not JSON and no image]",
    '{not JSON and no image}', '"not JSON and no image',
    '{ "n":1e400,"same":1,"same":2,"lone":"' + BS + 'uD800"}',
    "left DaTa: arbitrary IMAGE/png, ignored;BaSe64," + P + " right",
))
def test_benign_byte_identity(raw):
    overredacted = (
        "data:image/svg+xml,%3Csvg%3E",
        "data:image/png;name=base64," + P,
        "data:image/png;base64suffix," + P,
        "data:image/png;base64=ordinary," + P,
        'data:image/png;name="x;base64;y",' + P,
        "left DaTa: arbitrary IMAGE/png, ignored;BaSe64," + P + " right",
    )
    assert cap(raw, 0) == ("[image-redacted]" if raw in overredacted else raw)


@pytest.mark.parametrize("raw", ('["data:image/png;name=", ";base64,QUJD"]', '{"data:image/png;name=":";base64,QUJD"}'))
@pytest.mark.parametrize("depth", (0, 1, 3))
def test_complete_q1_tokens_never_join(raw, depth):
    value = wrap(raw, depth)
    assert cap(value, 0) == value


@pytest.mark.parametrize("raw,expected", (
    ('["data:image/png;name=", ";base64,QUJD"', '["__REDACTED__"'),
    ('{"data:image/png;name=":";base64,QUJD"', '{"__REDACTED__"'),
))
def test_unfinished_partitions_are_not_q1(raw, expected):
    assert cap(raw, 0) == "[image-redacted]"
    assert cap(raw, 64) == raw


@pytest.mark.parametrize("raw", (
    BS + 'u0020{"data:image/png;name=":";base64,' + P + '"}',
    BS + 'u007b"data:image/png;name=":";base64,' + P + '"' + BS + 'u007d',
    '%7B"data:image/png;name=":";base64,' + P + '"%7D',
    '&lbrace;"data:image/png;name=":";base64,' + P + '"&rbrace;',
))
def test_revealed_q1_precedes_irreversible_edits(raw):
    assert cap(raw, 0) == raw
    assert cap(wrap(raw, 1), 0) == wrap(raw, 1)


def test_json_keys_collisions_order_and_lexemes():
    raw = '{ "n":1e400,"same":1,"same":2,"' + H + P + '":0,"v":"before ' + H + P + ' after","esc":"' + BS + 'u0061"}'
    expected = '{ "n":1e400,"same":1,"same":2,"__REDACTED__":0,"v":"before __REDACTED__ after","esc":"' + BS + 'u0061"}'
    assert cap(raw) == expected
    assert [k for k, v in json.loads(expected, object_pairs_hook=list)] == ["n", "same", "same", "__REDACTED__", "v", "esc"]
    assert cap('{"' + H + P + '":1,"data:image/jpeg;base64,' + P + '":2,"tail":3}') == '{"__REDACTED__":1,"__REDACTED__":2,"tail":3}'


@pytest.mark.parametrize("before,after", (
    ('{"type":"image" "data":"', '"}'),
    ('{"type":"image",' + BS + 'u0022data' + BS + 'u0022:' + BS + 'u0022', BS + 'u0022]'),
    ('{"type":"image" ' + BS + 'u0022data' + BS + 'u0022:' + BS + 'u0022', BS + 'u0022}'),
    ('[' + BS + 'u007b' + BS + 'u0022type' + BS + 'u0022:' + BS + 'u0022image' + BS + 'u0022 ' + BS + 'u0022data' + BS + 'u0022:' + BS + 'u0022', BS + 'u0022' + BS + 'u007d]'),
))
def test_declared_recovery_on_each_revealed_view(before, after):
    assert cap(before + P + after) == before + "__REDACTED__" + after
    assert cap(before + P + after, 160) == before + P + after
    ordinary = before.replace("image", "text") + P + after
    assert cap(ordinary) == ordinary


@pytest.mark.parametrize("escaped", (BS + "t", BS + "b", BS + "f", BS + "u0000"))
def test_legal_escaped_controls_in_malformed_document(escaped):
    inner = '{"type":"image","data":"' + P + escaped + P + '","note":"keep",}'
    assert cap(wrap(inner, 1)) == wrap(inner, 1)


@pytest.mark.parametrize("control", range(32))
def test_original_raw_control_damage_is_preserved(control):
    raw = '{"type":"image","data":"' + P + chr(control) + P + '","note":"keep"}'
    assert cap(raw) == '{"type":"image","data":"__REDACTED__","note":"keep"}'
    assert cap(raw, 321) == raw
    assert cap(raw.replace('"image"', '"text"', 1)) == raw.replace('"image"', '"text"', 1)


def test_declared_context_survives_transport_and_array_barriers():
    raw = '{"type":"image","data":' + json.dumps(BS + "u0051UJD" + P) + ',"note":"keep"}'
    assert cap(raw) == '{"type":"image","data":"__REDACTED__","note":"keep"}'
    assert cap(raw.replace('"image"', '"text"', 1)) == raw.replace('"image"', '"text"', 1)
    assert cap(raw, 164) == raw
    assert cap(P, 160, True) == P
    assert cap(P, 159, True) == "__REDACTED__"
    assert cap(P, 0) == P
    raw = '{"type":"image","' + P + '":"keep","source":{"data":"' + P + '"},"other":{"data":"' + P + '"},"content":["' + P + '"]}'
    expected = '{"type":"image","' + P + '":"keep","source":{"data":"__REDACTED__"},"other":{"data":"' + P + '"},"content":["' + P + '"]}'
    assert cap(raw) == expected


def test_three_pass_limit_and_oversize():
    assert cap("ordinary %2541", 0) == "ordinary %2541"
    assert cap("ordinary %252541", 0) == "ordinary %252541"
    assert cap("ordinary %25252541", 0) == "[image-redacted]"
    assert cap(BS + "u005cu005cu005cu0078ordinary", 0) == "[image-redacted]"
    assert cap('{"opaque":"ordinary %25252541","outside":"keep"}', 0) == '{"opaque":"[image-redacted]","outside":"keep"}'
    assert _image_cap._SCAN_CAP == 1048576
    assert _image_cap._WORK_FACTOR == 32
    assert cap("x" * 1048576, 1049000) == "x" * 1048576
    assert cap("x" * 1048577, 1049000) == "[image-redacted]"
    assert cap('{"x":"' + "x" * 1048576 + '"}', 1049000) == "[image-redacted]"


def test_private_physical_count_and_sequence_semantics():
    counted = _image_cap._cap_base64_images_counted
    assert counted("__REDACTED__ [image-redacted]", 0, A) == ("__REDACTED__ [image-redacted]", 0)
    separator = " " * 1100
    assert counted(H + P + separator + H + P, 64, A) == ("__REDACTED__" + separator + "__REDACTED__", 2)
    assert counted(H + P + H + P, 64, A) == ("__REDACTED__", 1)
    assert counted(H + P + "%64ata:image/png;base64," + P, 64, A) == ("[image-redacted]", 1)
    assert counted('{"a":"%64ata:image/png;base64,' + P + '","b":"' + H + P + '"}', 64, A) == ('{"a":"[image-redacted]","b":"__REDACTED__"}', 2)
    original = ["ordinary", H + P, "%64ata:image/png;base64," + P, 7]
    before = list(original)
    assert counted(original, 64, A) == (["ordinary", "__REDACTED__", "[image-redacted]", 7], 2)
    assert original == before
    assert cap(tuple(original)) == ("ordinary", "__REDACTED__", "[image-redacted]", 7)
    benign = ("ordinary", 7)
    assert cap(benign) is benign


@pytest.mark.parametrize("maximum", (-1, True, 1.5, None))
def test_public_validation(maximum):
    assert cap("ordinary", maximum) == "ordinary"


def test_fixed_marker_and_arbitrary_intervening_header_text():
    with pytest.raises(ValueError):
        _image_cap.cap_base64_images("ordinary", 64, B)
    raw = "data:image/svg+xml,%3Csvg%3E ;base64,QUJD"
    assert cap(raw, 0) == "[image-redacted]"
    assert cap('{"note":"' + raw + '","keep":1}', 0) == '{"note":"[image-redacted]","keep":1}'
    at = "data:image/" + "x" * 1004 + " ;base64,"
    beyond = "data:image/" + "x" * 1005 + " ;base64,"
    assert len(at) == 1024
    assert cap(at + P) == "__REDACTED__"
    assert cap(beyond + P) == "__REDACTED__"


def test_manual_export_marks_values_without_exporting_a_count():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from fi_instrumentation.instrumentation._tracers import FITracer
    from fi_instrumentation.instrumentation.config import TraceConfig

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(
        base64_image_max_length=64, pii_redaction=False, hide_error_details=False))
    try:
        span = tracer.start_span("manual-image-count", attributes={"raw": H + P})
        span.set_attribute("decoded", "%64ata:image/png;base64," + P)
        span.add_event("manual-image", {"image": H + P})
        span.end()
        finished = exporter.get_finished_spans()
        assert len(finished) == 1
        assert finished[0].attributes["raw"] == "__REDACTED__"
        assert finished[0].attributes["decoded"] == "[image-redacted]"
        assert finished[0].events[0].attributes["image"] == "__REDACTED__"
        assert "traceai.capture.dropped_content_count" not in finished[0].attributes
        assert all("traceai.capture.dropped_content_count" not in event.attributes for event in finished[0].events)
    finally:
        provider.shutdown()
