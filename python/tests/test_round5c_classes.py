"""Round 5c literal controls; source-authored before implementation, not executed."""

import json

import pytest

from fi_instrumentation.instrumentation import _image_cap
from fi_instrumentation.instrumentation.config import TraceConfig


MARK = "__REDACTED__"
B = chr(92)
BODY = "QUJD" * 40


def cap(value, maximum=64):
    return _image_cap.cap_base64_images(value, maximum, MARK)


def wrap(value, depth):
    for _ in range(depth):
        value = '{"opaque":' + json.dumps(value) + ',"outside":"keep"}'
    return value


def escaped(value):
    return "".join(B + "u" + format(ord(char), "04x") for char in value)


@pytest.mark.parametrize("raw", (
    '["data:image/png;name=", ";base64,QUJD"',
    '{"data:image/png;name=":";base64,QUJD"',
    '["data:image/png;name=", ";base64,QUJD"]',
    '{"data:image/png;name=":";base64,QUJD"}',
))
@pytest.mark.parametrize("depth", (0, 1, 3))
@pytest.mark.parametrize("maximum", (0, 1, 64))
def test_q1_independent_tokens_never_form_a_header(raw, depth, maximum):
    value = wrap(raw, depth)
    if not raw.endswith(("}", "]")) and maximum == 0:
        assert cap(value, maximum) == wrap("[image-redacted]", depth)
    elif not raw.endswith(("}", "]")) and maximum < 64:
        assert cap(value, maximum) == wrap(raw[:2] + '__REDACTED__"', depth)
    else:
        assert cap(value, maximum) == value


@pytest.mark.parametrize("depth", (0, 1, 3))
def test_q1_long_nested_value_keeps_all_original_syntax(depth):
    raw = ('{ "data:image/png;name=":";base64,' + BODY
           + '","n":1e400,"same":1,"same":2,"lone":"' + B + 'uD800"}')
    value = wrap(raw, depth)
    assert cap(value, 64) == value


@pytest.mark.parametrize("layers", (1, 2, 3))
def test_q1_transport_revealed_document_is_fenced_before_any_edit(layers):
    value = '{"data:image/png;name=":";base64,' + BODY + '"}'
    for _ in range(layers):
        # Only syntax is transported; keep the misleading header literal.
        value = value.replace(B, B + B).replace('"', B + "u0022")
    assert cap(value, 0) == value


@pytest.mark.parametrize("depth", (0, 1, 3))
@pytest.mark.parametrize("in_key", (False, True))
@pytest.mark.parametrize("encoded", (False, True))
def test_q1_complete_header_inside_one_token_is_redacted(depth, in_key, encoded):
    url = "data:image/png;base64," + BODY
    token = escaped(url) if encoded else url
    if in_key:
        raw = '{"' + token + '":0,"outside":"keep"}'
        expected = '{"__REDACTED__":0,"outside":"keep"}'
    else:
        raw = '{"value":"' + token + '","outside":"keep"}'
        expected = '{"value":"__REDACTED__","outside":"keep"}'
    assert cap(wrap(raw, depth)) == wrap(expected, depth)
    assert cap(wrap(raw, depth), len(url)) == wrap(raw, depth)


@pytest.mark.parametrize("raw,expected", (
    ('{"a":"b"}', True),
    ('{"a":"b"', False),
    ('["a", "b"', False),
    ('{"a":"unfinished', False),
    ('{"a":"' + B + 'u00', False),
    ('{"a":"' + B + 'q"}', False),
    ('{"a":"raw\tcontrol"}', False),
    ('{"a":"b"]', False),
    ('{"a":"b",}', False),
    ('["a",]', False),
    ('{{"a":"b"}}', False),
    ('"a" "b"', False),
    ('[bare "a"]', False),
    ('{"a" "b"}', False),
    ('{"a":01}', False),
    ('{"a":NaN}', False),
    ('["a"] ', False),
))
def test_q1_fence_requires_grammar_or_unambiguous_eof_prefix(raw, expected):
    assert _image_cap._has_backstop_fence(raw) is expected


@pytest.mark.parametrize("raw", (
    '{"data:image/png;name=":";base64,' + BODY + '"]',
    '{"data:image/png;name=":";base64,' + BODY + '",}',
))
def test_q4_non_fenced_document_keeps_whole_view_backstop(raw):
    expected = '{"__REDACTED__"]' if raw.endswith('"]') else '{"__REDACTED__",}'
    assert cap(raw) == expected


@pytest.mark.parametrize("depth", (0, 1, 3))
@pytest.mark.parametrize("encoded", (False, True))
def test_a001_short_malformed_run_does_not_skip_later_header(depth, encoded):
    header = "data:image/png stray;base64,"
    value = header + header + BODY
    expected = "__REDACTED__"
    if encoded:
        value = value.replace("data:", B + "u0064ata:")
        expected = "[image-redacted]"
    assert cap(wrap(value, depth)) == wrap(expected, depth)


@pytest.mark.parametrize("depth", (0, 1, 3))
@pytest.mark.parametrize("first_body,maximum", (("", 24), ("QUJD", 25)))
def test_a001_precise_run_cannot_consume_next_prefix(depth, first_body, maximum):
    header = "data:image/png;base64,"
    value = header + first_body + header + BODY
    expected = "__REDACTED__"
    assert cap(wrap(value, depth), maximum) == wrap(expected, depth)


@pytest.mark.parametrize("depth", (0, 1, 3))
@pytest.mark.parametrize("prefix", (B + "u0064ata:image/", "data:image" + B + "/"))
@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_b001_later_hidden_header_reacquires_literal_pair_ownership(depth, prefix, delta):
    # 520 filler pairs move the first comma outside Python's 1024-unit window.
    rejected = prefix + "x " * 520
    image = prefix + 'png;name="a' + B + '" b";base64,' + BODY
    assert len('data:image/png;name="a' + B + '" b";base64,' + BODY) == 195
    value = rejected + image
    expected = "[image-redacted]"
    assert cap(wrap(value, depth), 195 + delta) == wrap(expected, depth)


def test_b001_original_ts_spacing_is_inside_python_backstop_window():
    prefix = B + "u0064ata:image/"
    value = prefix + "x " * 120 + prefix + 'png;name="a' + B + '" b";base64,' + BODY
    # Unlike TS's 256-unit window, Python admits the first malformed header.
    assert cap(value, 194) == "[image-redacted]"


@pytest.mark.parametrize("header", (
    'data:image/svg+xml stray;name="a' + B + '";base64,',
    'data:image/svg+xml; name="a' + B + '";base64,',
))
@pytest.mark.parametrize("depth", (0, 1, 3))
def test_a003_b002_ambiguous_decoy_is_redacted_under_q4(header, depth):
    value = header + BODY + '",%3Csvg%3E'
    expected = '__REDACTED__",%3Csvg%3E'
    assert cap(wrap(value, depth)) == wrap(expected, depth)


@pytest.mark.parametrize("pair", ("", B + '"'))
@pytest.mark.parametrize("depth", (0, 1, 3))
def test_q4_unambiguous_literal_quoted_decoy_is_preserved(pair, depth):
    value = 'data:image/svg+xml;name="a' + pair + ';base64,' + BODY + '",%3Csvg%3E'
    raw = wrap(value, depth)
    assert cap(raw, 0) == wrap("[image-redacted]", depth)


def test_q4_ambiguous_header_still_requires_a_payload_run():
    value = 'data:image/svg+xml; name="a' + B + '";base64,",%3Csvg%3E'
    assert cap(value, 0) == "[image-redacted]"


@pytest.mark.parametrize("depth", (0, 1))
def test_q1_and_q4_config_route_keeps_source_input(depth):
    raw = wrap('{"split":"data:image/png;name=","tail":";base64,' + BODY
               + '","real":"data:image/png;base64,' + BODY + '"}', depth)
    expected = wrap('{"split":"data:image/png;name=","tail":";base64,' + BODY
                    + '","real":"__REDACTED__"}', depth)
    before = raw
    cfg = TraceConfig(base64_image_max_length=64, pii_redaction=False, hide_error_details=False)
    assert cfg.redact_value(raw) == expected
    assert raw == before


@pytest.mark.parametrize("depth", (0, 1, 3))
def test_q4_unquoted_transport_cannot_manufacture_decoy_ownership(depth):
    value = 'data:image/svg+xml;name=' + B + 'u0022a;base64,' + BODY + B + 'u0022,%3Csvg%3E'
    expected = '__REDACTED__' + B + 'u0022,%3Csvg%3E'
    assert cap(wrap(value, depth)) == wrap(expected, depth)


@pytest.mark.parametrize("depth", (0, 1, 3))
def test_q4_unrelated_transport_does_not_taint_literal_decoy(depth):
    value = ('note ' + B + 'u0022 outside data:image/svg+xml;name="a;base64,'
             + BODY + '",%3Csvg%3E')
    raw = wrap(value, depth)
    assert cap(raw, 0) == wrap("[image-redacted]", depth)


def test_q4_later_literal_decoy_cannot_certify_an_earlier_ambiguous_header():
    value = ('data:image/x stray data:image/svg+xml;name="a;base64,'
             + BODY + '",%3Csvg%3E')
    assert cap(value) == '__REDACTED__",%3Csvg%3E'


def test_overlap_cursor_and_quote_guard_small_first(monkeypatch):
    original = _image_cap._backstop_unit
    consumed = 0

    def counted(text, index, stop):
        nonlocal consumed
        char, end = original(text, index, stop)
        consumed += end - index
        return char, end

    monkeypatch.setattr(_image_cap, "_backstop_unit", counted)
    for count in (1, 8, 64, 512):
        value = "data:image/png;" * count + 'name="a" b";base64,' + BODY + " suffix"
        consumed = 0
        assert cap(value).endswith("__REDACTED__ suffix")
        assert consumed <= 8 * len(value)
