"""Additive helper controls; existing SDK/exporter/performance tests stay immutable."""
import json

import pytest

from fi_instrumentation.instrumentation._image_cap import cap_base64_images

MAX = 256
MARK = "__REDACTED__"
PREFIX = "data:image/png;base64,"
PAYLOAD = "QUJD" * 512 + "RESOURCECANARY"
BIG = PREFIX + PAYLOAD
SLASH = chr(92)


def dump(value):
    return json.dumps(value, separators=(",", ":"))


def at(length):
    return PREFIX + "A" * (length - len(PREFIX))


def delayed(layers, tail):
    # Each decode removes only five characters; quoted wrappers alone grow
    # exponentially and do not expose the slow-shrinking repeated-decode class.
    return SLASH + "u005c" * layers + tail


def hidden_image(layers):
    return delayed(layers, "u0064ata:image/png;base64," + PAYLOAD)


def ordinary(layers):
    return delayed(layers, "u0078ordinary text " + "z" * 2048)


def escaped(text):
    return "".join(SLASH + "u" + format(ord(c), "04x") for c in text)


def cap(value, maximum=MAX, image_key=False):
    return cap_base64_images(value, maximum, MARK, image_key)


def test_bound_sensitivity_exhausted_ordinary_is_not_returned_uninspected():
    value = ordinary(256)
    assert "data:image/" not in value
    assert cap(value) == "[image-redacted]"
    # The unlimited baseline returns value: this detects a missing resource bound.


def test_affordable_ordinary_encoding_is_preserved():
    for value in (ordinary(2), "ordinary " + "x" * 65536, SLASH + "q" + "safe " * 1000):
        assert cap(value) == value


@pytest.mark.parametrize("layers", [128, 256, 512])
def test_exhaustion_covers_hidden_image_and_suffix(layers):
    value = hidden_image(layers) + " ordinary suffix " + BIG
    assert cap(value) == "[image-redacted]"
    raw = '{"opaque":' + dump(value) + ',"note":"keep","exact":' + dump(at(MAX)) + '}'
    expected = '{"opaque":"' + "[image-redacted]" + '","note":"keep","exact":' + dump(at(MAX)) + '}'
    assert cap(raw) == expected


def test_outer_budgets_are_independent():
    fields = dict(before=ordinary(2), expensive=ordinary(256), after=ordinary(2), url=at(MAX), type="image", id="QUJD")
    raw = dump(fields)
    fields["expensive"] = "[image-redacted]"
    assert cap(raw) == dump(fields)


def test_fallback_subsumes_mapped_edits_and_pending_encoded_children():
    # The LIFO worklist edits last before exhausting on first. Fallback must
    # union the partial edit, not expose its remaining suffix or pending children.
    inner = dump(dict(first=hidden_image(256), pending=BIG, last=BIG))
    raw = dump(dict(opaque=inner, ordinary="retain", shallow=BIG))
    expected_inner = dump(dict(first="[image-redacted]", pending=MARK, last=MARK))
    assert cap(raw) == dump(dict(opaque=expected_inner, ordinary="retain", shallow=MARK))


def test_unterminated_outer_token_exhaustion_covers_damaged_suffix():
    content = dump(hidden_image(256) + " suffix " + BIG)[1:-1]
    raw = '{"ordinary":"retain","opaque":"' + content
    assert cap(raw) == "[image-redacted]"


@pytest.mark.parametrize("depth", [1, 4, 9])
def test_affordable_nested_json_preserves_original_tokens(depth):
    raw = ('{ "id":9007199254740993,"n":1e400,"same":1,"same":2,"s":"'
           + SLASH + 'uD800","data":"' + PAYLOAD + '","media_type":"image/png" }')
    expected = raw.replace(PAYLOAD, MARK)
    for _ in range(depth):
        raw, expected = dump(raw), dump(expected)
    assert cap(raw) == expected


def test_large_escaped_payload_decoded_units_and_original_offsets():
    payload = "QUJD" * 4096
    encoded = escaped(payload)
    raw = '{"note":"' + SLASH + 'u0061' + SLASH + '/b","data":"' + encoded + '","media_type":"image/png"}'
    assert cap(raw, len(payload)) == raw
    assert cap(raw, len(payload) + 1) == raw
    assert cap(raw, len(payload) - 1) == raw.replace(encoded, MARK)


def test_many_edits_through_large_escape_indexes_are_lossless():
    encoded = escaped(BIG[:1679])
    raw = '["' + '","'.join([encoded] * 48) + '"]'
    expected = dump([MARK] * 48)
    for _ in range(3):
        raw, expected = dump(raw), dump(expected)
    assert len(raw) == 1_048_527
    assert cap(raw) == expected


@pytest.mark.parametrize("depth", [6000, 65536])
def test_deep_malformed_arrays_inspect_populated_leaves(depth):
    raw = "[" * depth + dump(BIG) + ',{"data":' + dump(PAYLOAD) + ',"type":"image","name":"keep"}'
    expected = "[" * depth + dump(MARK) + ',{"data":' + dump(MARK) + ',"type":"image","name":"keep"}'
    assert cap(raw) == expected
    empty = "[" * depth
    assert cap(empty) == empty


def test_deep_objects_mismatched_closers_and_source_declarations():
    raw = ('{"source":{"data":' + dump(PAYLOAD) + '},"deep":' + "{" * 6000
           + '"url":' + dump(BIG) + "]" * 6000 + ',"type":"image"}')
    assert cap(raw) == raw.replace(PAYLOAD, MARK, 1).replace(BIG, MARK)


def test_compressed_arrays_do_not_invent_image_inheritance():
    fields = dict(type="image", source=[[[dict(data=PAYLOAD)]]], url=BIG)
    raw = dump(fields)
    fields["url"] = MARK
    assert cap(raw) == dump(fields)


def test_many_short_tokens_keep_late_declaration_and_over_cap_payload():
    raw = '{"ordinary":[' + '"a",' * 20000 + '"a"],"data":' + dump(PAYLOAD) + ',"type":"image"}'
    assert cap(raw) == raw.replace(PAYLOAD, MARK)


def test_long_quoted_headers_and_wrapped_payloads_have_no_truncation_budget():
    wrapped = "\r\n".join(PAYLOAD[i:i + 16] for i in range(0, len(PAYLOAD), 16))
    url = 'data:image/png;name="' + "a" * 65536 + '";base64,' + wrapped
    raw = "prefix " + url + " ordinary caption"
    assert cap(raw) == "[image-redacted]"
    assert cap(raw, len(url)) == raw
    assert cap(raw, len(url) + 1) == raw


@pytest.mark.parametrize("container", [list, tuple])
def test_sequence_elements_are_isolated_and_immutable(container):
    raw = container([ordinary(256), at(MAX), ordinary(2), BIG])
    before = container(raw)
    result = cap(raw)
    assert type(result) is container
    assert result == container(["[image-redacted]", at(MAX), ordinary(2), MARK])
    assert raw == before


def test_flattened_image_key_keeps_its_existing_boundary_semantics():
    assert cap(PAYLOAD, len(PAYLOAD), image_key=True) == PAYLOAD
    assert cap(PAYLOAD, len(PAYLOAD) - 1, image_key=True) == MARK
    assert cap(hidden_image(256), image_key=True) == "[image-redacted]"
