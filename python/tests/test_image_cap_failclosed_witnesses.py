"""Fail-closed r4 witness controls (fi-security 628059fc; RL 2310ah). Lead-authored from the fail-closed r3 lens witnesses; RED on the r3 base."""
from fi_instrumentation.instrumentation._image_cap import cap_base64_images

BS = chr(92)
P = "QUJD" * 40
WHOLE = "[image-redacted]"


def cap(value, maximum=64):
    return cap_base64_images(value, maximum, "__REDACTED__")


def test_long_quoted_header_hidden_encoding_flag_is_redacted():
    head = 'data:image/png;name="a' + BS + '" ' + "q" * 1200 + '"'
    out = cap(head + ";ba" + BS + "u0073e64," + P)
    assert "QUJDQUJD" not in out


def test_long_quoted_header_two_layer_payload_is_redacted():
    head = 'data:image/png;name="a' + BS + '" ' + "q" * 1200 + '"'
    out = cap(head + ";base64," + (BS + "u005cu0051UJD") * 40)
    assert "UJD" * 40 not in out and out == WHOLE


def test_khmer_invisible_vowels_in_prefix_and_payload_are_removed():
    for z in (chr(0x17B4), chr(0x17B5)):
        assert "QUJDQUJD" not in cap("d" + z + "ata:image/png;base64," + P)
        assert "QUJDQUJD" not in cap("data:image/png;base64," + P + z + P)


def test_nonconvergent_value_after_three_passes_is_whole():
    assert cap("%252522%25252541%252522") == WHOLE


def test_backstop_redacts_per_string_token_in_strict_json():
    doc = '{"keep":"ordinary words","x":"' + BS + 'u0064ata:image/png;' + BS + 'u0062ase64;name=' + BS + '"a' + BS + '",' + P + '"}'
    out = cap(doc)
    assert out.startswith('{"keep":"ordinary words","x":"') and "QUJDQUJD" not in out


def test_benign_values_unchanged():
    for value in ("ordinary words", "see https://example.com/a,b?c=1", '{"a":"b","n":1}', "data:image/svg+xml,%3Csvg%3E",
                  "data:image/png;base64,QUJD", "emoji \U0001F468\u200D\U0001F469 family"):
        assert cap(value) == value


def test_default_ignorable_split_continuation_is_not_left_behind():
    for code in (0x17B4, 0x17B5, 0x115F, 0x3164, 0xFFA0, 0x1D173, 0xE0000):
        out = cap("data:image/png;base64," + P + chr(code) + P)
        assert "QUJDQUJD" not in out, hex(code)
        out = cap("d" + chr(code) + "ata:image/png;base64," + P)
        assert "QUJDQUJD" not in out, hex(code)


def test_backstop_measures_payload_not_escaped_header():
    url = 'data:image/png;name="sample";base64,' + P
    for doc in (_json_dumps("[" + url + "]"), _json_dumps(_json_dumps(url))):
        assert cap(doc, len(url)) == doc
        assert cap(doc, len(url) + 1) == doc
    out = cap(_json_dumps(url), 64)
    assert "QUJDQUJD" not in out and isinstance(__import__("json").loads(out), str)


from json import dumps as _json_dumps  # noqa: E402
