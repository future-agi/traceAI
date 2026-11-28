"""The pair rule groups of the shared privacy spec on the copy of a span sent to Future AGI.

Each test covers one rule group of the spec (the credential-named pair rule in text, whole-URL values, URL pairs after '#', a second '?'
and in the path, the extended name vocabulary, JSON quoted names, map keys, the name depth limit, fail-closed bounds, and the time bound).
The expected values are the spec reference's output for the same input; every value is synthetic. ``framed=False`` is free text.
"""

import time

import pytest

PARAMS = "value, path, framed, secrets, expected"


def _check(value, path, framed, secrets, expected):
    from fi_instrumentation.auto import _privacy

    assert _privacy.scrub(value, path, None, secrets, framed=framed) == expected
    # The output is stable: scrubbing it again changes nothing.
    assert _privacy.scrub(expected, path, None, secrets, framed=framed) == expected


@pytest.mark.parametrize(PARAMS, [
        ('login failed: password=SYNTHETIC_PRIVATE_1122 for user', '', False, (),
         'login failed: password=<redacted>'),
        ('api_key=<redacted> then x', '', False, (),
         'api_key=<redacted>'),
        ('note token: SYNTHETIC_PRIVATE_1122;rest ok', '', False, (),
         'note token: <redacted>'),
        ('secret=a b c SYNTHETIC_PRIVATE_1122 end', '', False, (),
         'secret=<redacted>'),
        ('x-api-key=SYNTHETIC_PRIVATE_1122&other=1', '', False, (),
         'x-api-key=<redacted>'),
        ('{"msg": "retry password=SYNTHETIC_PRIVATE_1122 now"}', 'input.value', True, (),
         '{"msg":"retry password=<redacted>"}'),
        ('dbpassword = SYNTHETIC_PRIVATE_1122', '', False, (),
         'dbpassword = <redacted>'),
        ('seen SYNTHETIC_PRIVATE_1122 then password=SYNTHETIC_PRIVATE_1122 tail', '', False, ('SYNTHETIC_PRIVATE_1122',),
         'seen <redacted> then password=<redacted>'),
])
def test_pair_rule_to_true_value_end(value, path, framed, secrets, expected):
    """Pair rule: a credential-named name=value or name: value runs to the true end of its value."""
    _check(value, path, framed, secrets, expected)


@pytest.mark.parametrize(PARAMS, [
        ('https://example.test/a/b?next=password%3DSYNTHETIC_PRIVATE_1122', '', True, (),
         'https://example.test/a/b?next=password%3D<redacted>'),
        ('https://example.test/?q=x&redirect=https://example.test/?password=SYNTHETIC_PRIVATE_1122', '', True, (),
         'https://example.test/?q=x&redirect=https://example.test/?password=<redacted>'),
        ('https://example.test/a?x=1&token=SYNTHETIC_PRIVATE_1122&note=password=SYNTHETIC_PRIVATE_1122', '', True, (),
         'https://example.test/a?x=1&token=<redacted>&note=password=<redacted>'),
        ('https://example.test/a?q=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl&note=password=SYNTHETIC_PRIVATE_1122', '', True, (),
         'https://example.test/a?q=<redacted>&note=password=<redacted>'),
        ('https://user:SYNTHETIC_PRIVATE_1122@example.test/p/password=SYNTHETIC_PRIVATE_1122', '', True, (),
         'https://<redacted>@example.test/p/password=<redacted>'),
        ('https://example.test/a;password=SYNTHETIC_PRIVATE_1122?x=1', '', True, (),
         'https://example.test/a;password=<redacted>?x=1'),
])
def test_whole_url_pair_rule_skips_only_catalog_spans(value, path, framed, secrets, expected):
    """Whole-URL value: the pair rule runs over the URL text and skips only spans the catalog itself replaced."""
    _check(value, path, framed, secrets, expected)


@pytest.mark.parametrize(PARAMS, [
        ('https://example.test/page#password=SYNTHETIC_PRIVATE_1122&a=1', '', True, (),
         'https://example.test/page#password=<redacted>&a=1'),
        ('https://example.test/p?a=1?password=SYNTHETIC_PRIVATE_1122', '', True, (),
         'https://example.test/p?a=1?password=<redacted>'),
        ('https://example.test/password=SYNTHETIC_PRIVATE_1122/x', '', True, (),
         'https://example.test/password=<redacted>/x'),
        ('https://example.test/password%3DSYNTHETIC_PRIVATE_1122/x', '', True, (),
         'https://example.test/password%3D<redacted>/x'),
        ('see https://example.test/p#token=SYNTHETIC_PRIVATE_1122 ok', '', False, (),
         'see https://example.test/p#token=<redacted>'),
        ('see https://example.test/p?a=1?secret=SYNTHETIC_PRIVATE_1122 ok', '', False, (),
         'see https://example.test/p?a=1?secret=<redacted>'),
        ('see https://example.test/password=SYNTHETIC_PRIVATE_1122/x ok', '', False, (),
         'see https://example.test/password=<redacted>'),
])
def test_url_pairs_after_fragment_second_query_and_path(value, path, framed, secrets, expected):
    """URL pairs after '#', after a second '?' and as a path segment are replaced like query pairs, as URL values and inside text."""
    _check(value, path, framed, secrets, expected)


@pytest.mark.parametrize(PARAMS, [
        ('https://example.test/?dbPass=SYNTHETIC_PRIVATE_1122', '', True, (),
         'https://example.test/?dbPass=<redacted>'),
        ('https://example.test/#user-pin=SYNTHETIC_PRIVATE_1122', '', True, (),
         'https://example.test/#user-pin=<redacted>'),
        ('https://example.test/pass=SYNTHETIC_PRIVATE_1122/x', '', True, (),
         'https://example.test/pass=<redacted>/x'),
        ('SYNTHETIC_PRIVATE_1122', 'db.pwd', True, (),
         '<redacted>'),
        ('SYNTHETIC_PRIVATE_1122', 'X-Pin', True, (),
         '<redacted>'),
        ('{"passphrase": "SYNTHETIC_PRIVATE_1122"}', 'input.value', True, (),
         '{"passphrase":"<redacted>"}'),
        ({'passcode': 'SYNTHETIC_PRIVATE_1122'}, 'input.value', True, (),
         {'passcode': '<redacted>'}),
        ('run passwd=SYNTHETIC_PRIVATE_1122 now', '', False, (),
         'run passwd=<redacted>'),
        ('a db.pwd: SYNTHETIC_PRIVATE_1122 b', '', False, (),
         'a db.pwd: <redacted>'),
])
def test_vocabulary_pwd_pass_passphrase_passwd_passcode_pin(value, path, framed, secrets, expected):
    """pwd, pass, passphrase, passwd, passcode and pin count as credential names at every site."""
    _check(value, path, framed, secrets, expected)


@pytest.mark.parametrize(PARAMS, [
        ('note: "password": "SYNTHETIC_PRIVATE_1122" and more', '', False, (),
         'note: "password": "<redacted>" and more'),
        ("note: 'password': 'SYNTHETIC_PRIVATE_1122' tail ok", '', False, (),
         "note: 'password': '<redacted>' tail ok"),
        ('note: \\"password\\": \\"SYNTHETIC_PRIVATE_1122\\" tail ok', '', False, (),
         'note: \\"password\\": \\"<redacted>\\" tail ok'),
        ('note: &quot;password&quot;: &quot;SYNTHETIC_PRIVATE_1122&quot; tail ok', '', False, (),
         'note: &quot;password&quot;: &quot;<redacted>&quot; tail ok'),
        ('note: "pin": 123456, "x": 1 tail ok', '', False, (),
         'note: "pin": "<redacted>", "x": 1 tail ok'),
        ('note: "pin": 123456} tail', '', False, (),
         'note: "pin": "<redacted>"} tail'),
        ('note: "token": true, next', '', False, (),
         'note: "token": "<redacted>", next'),
        ('note: "token": null] end', '', False, (),
         'note: "token": "<redacted>"] end'),
        ('note: "token": {"a": "SYNTHETIC_PRIVATE_1122", "b": 1} after', '', False, (),
         'note: "token": "<redacted>"'),
        ('note: "token": [1, "SYNTHETIC_PRIVATE_1122"] after', '', False, (),
         'note: "token": "<redacted>"'),
        ('note: "token": SYNTHETIC_PRIVATE_1122 and words, more', '', False, (),
         'note: "token": "<redacted>"'),
        ('note: "password": "a\\"bSYNTHETIC_PRIVATE_1122" tail ok', '', False, (),
         'note: "password": "<redacted>" tail ok'),
])
def test_json_quoted_names_value_end(value, path, framed, secrets, expected):
    """JSON quoted names: a quoted value ends at its closing quote, a scalar before , } ] or the end, anything else is prose."""
    _check(value, path, framed, secrets, expected)


@pytest.mark.parametrize(PARAMS, [
        ({'https://example.test/?password=SYNTHETIC_PRIVATE_1122': 1, 'k': 2}, 'input.value', True, (),
         {'https://example.test/?password=<redacted>': '<redacted>', 'k': 2}),
        ({'password=SYNTHETIC_PRIVATE_1122': 'v', 'ok': 'w'}, 'input.value', True, (),
         {'password=<redacted>': '<redacted>', 'ok': 'w'}),
        ({'a password=SYNTHETIC_PRIVATE_1122': 'v1', 'a password=SYNTHETIC_PRIVATE_11222': 'v2', 'z': '3'}, 'input.value', True, (),
         {'a password=<redacted>': '<redacted>', 'z': '3'}),
        ({'https://example.test/?token=a1': 'v', 'https://example.test/?token=b2': 'w'}, 'input.value', True, (),
         {'https://example.test/?token=<redacted>': '<redacted>'}),
        ('{"x password=SYNTHETIC_PRIVATE_1122": "v1", "x password=SYNTHETIC_PRIVATE_112": "v2", "z": "3"}', 'input.value', True, (),
         '{"x password=<redacted>":"<redacted>","z":"3"}'),
])
def test_map_keys_go_through_text_rules(value, path, framed, secrets, expected):
    """Map keys go through the export text rules; keys that collide after scrubbing keep one key with the placeholder value."""
    _check(value, path, framed, secrets, expected)


@pytest.mark.parametrize(PARAMS, [
        ('x', 'a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a', True, (),
         '<redacted>'),
        ('x', 'a[1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1][1]', True, (),
         '<redacted>'),
        ({'k': 'v'}, 'a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a', True, (),
         '<redacted>'),
        ('see a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a=SYNTHETIC_PRIVATE_1122 tail', '', False, (),
         '<redacted>'),
        ('see a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a.a: SYNTHETIC_PRIVATE_1122', '', False, (),
         '<redacted>'),
])
def test_depth_over_limit_redacts_whole_value(value, path, framed, secrets, expected):
    """More than limits.max_depth name components make the whole exported value the placeholder."""
    _check(value, path, framed, secrets, expected)


@pytest.mark.parametrize(PARAMS, [
        ('x aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=SYNTHETIC_PRIVATE_1122 tail', '', False, (),
         'x aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=<redacted>'),
        ('x passwordaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=SYNTHETIC_PRIVATE_1122 tail', '', False, (),
         'x passwordaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=<redacted>'),
        ('https://example.test/p?a?????????=SYNTHETIC_PRIVATE_1122&b=1', '', True, (),
         'https://example.test/p?a?????????=<redacted>&b=1'),
        ('https://example.test/p?aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=SYNTHETIC_PRIVATE_1122', '', True, (),
         'https://example.test/p?aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=<redacted>'),
        ('https://example.test/p?q=1??????????=SYNTHETIC_PRIVATE_1122', '', True, (),
         'https://example.test/p?q=1??????????=<redacted>'),
])
def test_bounds_fail_closed(value, path, framed, secrets, expected):
    """A pair name past its 128-character window, or a name after '?' with more than 8 '?', is undecided: its value is replaced."""
    _check(value, path, framed, secrets, expected)


# 1.6M characters of each unit, repeated. The first four run as free text and as a whole URL; the
# others are the spec's timing shapes for name gaps, escapes, --name line breaks, single-dash flags
# and quoted names with '=', and run as free text. Each scrub must finish within TIME_BOUND seconds:
# the reads are linear, and on one core of a CI runner the slowest input takes about five seconds.
TIME_BOUND = 20
ADVERSARIAL_SIZE = 1_600_000
ADVERSARIAL_UNITS = {
    "question_runs": "a" * 120 + "?" * 9 + "=",
    "equals_runs": "x=" * 64,
    "colon_runs": ":" * 64 + "x",
    "long_names": "n" * 200 + "=",
}
ADVERSARIAL_TEXT_UNITS = {
    "gap_encoded": "a \t %3D",
    "encoded_gap_json": "a" + "\\t" * 60 + ":",
    "readback_hex": "ab%41%42%43cd%3D",
    "line_break_escapes": "--token\n" + "%41" * 40 + "=v ",
    "line_break_word": "--token\nv ",
    "single_dash": "-token v ",
    "word_separators": "--token a=b:c%3Dd ",
    "quoted_equals": '"model"=v ',
}


def _repeat(unit):
    return (unit * (ADVERSARIAL_SIZE // len(unit) + 1))[:ADVERSARIAL_SIZE]


def _timed(value, framed):
    from fi_instrumentation.auto import _privacy

    start = time.perf_counter()
    _privacy.scrub(value, "", None, (), framed=framed)
    return time.perf_counter() - start


@pytest.mark.parametrize("framed", [False, True], ids=["free_text", "whole_url"])
def test_adversarial_input_is_scrubbed_in_bounded_time(framed):
    """1.6M characters of adversarial input finish within TIME_BOUND seconds.

    This one may already pass before the pair rule is ported: it guards the new rule against super-linear scans.
    """
    units = dict(ADVERSARIAL_UNITS, **({} if framed else ADVERSARIAL_TEXT_UNITS))
    for label, unit in units.items():
        text = _repeat(unit)
        elapsed = _timed("https://example.test/p?" + text if framed else text, framed)
        print("%s framed=%s: %.2fs" % (label, framed, elapsed))
        assert elapsed < TIME_BOUND, (label, framed, elapsed)


def test_an_environment_name_past_the_depth_limit_does_not_stop_the_known_secrets(monkeypatch):
    """Each credential-named environment value is registered; a name of more than limits.max_depth components is not read,
    so its value is registered too (fail closed), and the names after it are still read."""
    from fi_instrumentation.auto import _privacy

    monkeypatch.setattr(_privacy, "_KNOWN", _privacy._KnownSecrets())
    monkeypatch.setattr(_privacy.os, "environ", {
        "A_API_TOKEN": "SYNTHETIC_ENV_FIRST_1122",
        ".".join(["x"] * 40): "SYNTHETIC_ENV_DEEP_1122",
        "Z_API_TOKEN": "SYNTHETIC_ENV_LAST_1122",
        "MODEL_NAME": "SYNTHETIC_ENV_MODEL_1122",
    })
    _privacy.seed_known_secrets()
    known = set(_privacy._KNOWN.snapshot())
    assert {"SYNTHETIC_ENV_FIRST_1122", "SYNTHETIC_ENV_DEEP_1122", "SYNTHETIC_ENV_LAST_1122"} <= known
    assert "SYNTHETIC_ENV_MODEL_1122" not in known


# The value shapes read whitespace as str.isspace, word characters as Python's re (combining marks, joiners and connector
# punctuation are none), and the i flag with Unicode case folding. Each expected text is the spec reference's output.
@pytest.mark.parametrize("value, expected", [
    ("́sk-TAIL1abcdefghijklmnopq", "́<redacted>"),
    ("Baſic TAIL2abcdefgh", "<redacted>"),
    ("‍AKIATAIL3ABCDEFGHIJK‌", "‍<redacted>‌"),
    ("‿eyJTAIL4abcd.TAIL4efgh.TAIL4ijkl", "‿<redacted>"),
    ("Bearer TAIL5abcdefgh", "<redacted>"),
    ("BEARER TAIL6abcdefgh̶", "<redacted>̶"),
    ("٣sk-TAIL7abcdefghijklmnopq", "٣sk-TAIL7abcdefghijklmnopq"),
    ("Basic TAIL8abcdefgh", "<redacted>"),
])
@pytest.mark.parametrize("path, framed", [("", False), ("output.value", True)])
def test_value_shapes_read_unicode_as_the_reference_does(value, expected, path, framed):
    _check(value, path, framed, (), expected)


# A JSON value followed by more text is no structured JSON: the whole string goes through the text rules.
@pytest.mark.parametrize("value", [
    "{} password=TAILV tail", "[1] token: TAILV", '{"a":1} trailing password=TAILV', '{"a":"x"} password=TAILV',
    "{}api_keydata:TAILVTAILV",
])
def test_json_followed_by_text_keeps_no_marker(value):
    import json

    from fi_instrumentation.auto import _privacy

    for path in ("", "input.value", "output.value", "http.url"):
        for framed in (True, False):
            assert "TAILV" not in _privacy.scrub(value, path, None, (), framed=framed)
    assert "TAILV" not in json.dumps(_privacy.scrub(json.dumps({"m": value}), "input.value"))
    assert "TAILV" not in json.dumps(_privacy.scrub({"m": value}, "input.value"))


# Known secrets are applied one at a time, longest first, as the reference applies them: a credential name that an
# overlapping or touching secret does not cover stays readable, and the value after it is replaced.
@pytest.mark.parametrize("value, secrets, expected", [
    ("D1XDZ11Bauth=TAILV6", ("XDZ11Bau", "D1XDZ11B"), "<redacted>auth=<redacted>"),
    ("note YXXBDC1api_key: TAILV43 tail", ("api_key:", "YXXBDC1ap"), "note <redacted>i_key: <redacted>"),
    ("x AAAA-BBBB-01-CCCC password=TAILV7 y", ("AAAA-BBBB-01", "BBBB-01-CCCC"), "x <redacted>-CCCC password=<redacted>"),
])
def test_overlapping_known_secrets_leave_the_credential_pair_readable(value, secrets, expected):
    _check(value, "", False, secrets, expected)
