"""The shared privacy catalog on the copy of a span the zero-code agent sends to Future AGI.

Every case in the spec's fixture bundle runs through ``_privacy.scrub``, the function the export
sanitizer applies to every attribute value (the attribute key is the path, the span's other
attributes are the siblings), to every value inside JSON text in an attribute, and to every name,
status description and other text it sends. The bundle is read from the repository at test time.
"""

import base64
import itertools
import json
import logging
import re
import time
from pathlib import Path

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import SpanKind, Status, StatusCode

REPOSITORY = Path(__file__).resolve().parents[2]
SPEC = REPOSITORY / "spec" / "auto-instrumentation"

# Every site runs. Request and response bodies reach Future AGI as JSON text in attributes
# (input.value, output.value, raw.input, raw.output), which the sanitizer parses and walks with the
# same function, so the body cases run through it, and the header cases too: a flat object is a
# parsed body. Python has no transport capture, but every URL it exports, as an attribute value or
# inside JSON text, is checked with the same framed call the query cases make, so they run as
# exact URL attribute values.
EXPORT_SITES = (
    "request_headers", "request_query", "request_body", "response_body", "attribute_keys",
    "flattened_paths", "indexed_paths", "mime_siblings", "magic_bytes", "free_text_urls",
)
# Sites Python cannot run, and why: none.
WIRE_ONLY = {}


@pytest.fixture(scope="module")
def bundle():
    path = SPEC / "privacy-fixtures.json"
    assert path.is_file(), f"the privacy fixture bundle is missing at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _same(actual, expected):
    """Exact JSON equality: types, numbers and array order count; 1, 1.0 and true differ."""
    return json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_every_fixture_site_is_run_or_skipped_by_name(bundle):
    catalog = json.loads((SPEC / "privacy.json").read_text(encoding="utf-8"))
    assert (bundle["version"], bundle["catalog_version"], catalog["version"]) == (1, 3, 3)
    sites = set(catalog["fixture_sites"])
    # An unknown site fails here instead of being skipped.
    assert {case["input"]["site"] for case in bundle["cases"]} == sites
    assert set(EXPORT_SITES) | set(WIRE_ONLY) == sites
    assert not set(EXPORT_SITES) & set(WIRE_ONLY)


@pytest.mark.parametrize("site", sorted(EXPORT_SITES + tuple(WIRE_ONLY)))
def test_privacy_fixtures(bundle, site):
    if site in WIRE_ONLY:
        pytest.skip(WIRE_ONLY[site])
    from fi_instrumentation.auto import _privacy

    cases = [case for case in bundle["cases"] if case["input"]["site"] == site]
    assert cases
    # Free text is unframed (delimiter-limited scanning); every other site is an exact value.
    framed = site != "free_text_urls"
    failures = []
    for case in cases:
        inputs = case["input"]
        arguments = (inputs["path"], inputs.get("siblings"), inputs.get("known_secrets", ()))
        actual = _privacy.scrub(inputs["value"], *arguments, framed=framed)
        if not _same(actual, case["expected"]):
            failures.append(f"{case['name']}: got {actual!r}, want {case['expected']!r}")
            continue
        again = _privacy.scrub(actual, *arguments, framed=framed)
        if not _same(again, actual):
            failures.append(f"{case['name']}: not idempotent: {actual!r} became {again!r}")
    assert not failures, f"{len(failures)} of {len(cases)} {site} cases differ: " + "; ".join(failures[:5])


def test_the_wheel_carries_the_spec_catalog_byte_for_byte():
    import fi_instrumentation.auto as auto
    from fi_instrumentation.auto import _privacy

    shipped = Path(auto.__file__).resolve().parent / "wire" / "privacy.json"
    assert shipped.is_file(), f"{shipped} is not installed with the package"
    assert shipped.read_bytes() == (SPEC / "privacy.json").read_bytes()
    assert Path(_privacy.CATALOG_PATH).resolve() == shipped


def _span(attributes=None, name="chat", resource=None, scope=None, trace_state=None, events=(), status=None):
    context = trace_api.SpanContext(trace_id=0x1F, span_id=0x2F, is_remote=False,
                                    trace_flags=trace_api.TraceFlags(trace_api.TraceFlags.SAMPLED),
                                    trace_state=trace_state)
    return ReadableSpan(
        name=name, context=context, parent=None,
        resource=resource if resource is not None else Resource({"service.name": "checkout"}),
        attributes=attributes or {}, events=list(events), links=[], kind=SpanKind.INTERNAL,
        status=status or Status(StatusCode.UNSET), start_time=1, end_time=2,
        instrumentation_scope=scope or InstrumentationScope("traceai_test_app", "1.0"),
    )


def test_hide_markers_stay_as_the_hide_settings_wrote_them():
    from fi_instrumentation.auto import _privacy
    from fi_instrumentation.instrumentation.config import REDACTED_VALUE

    assert _privacy.HIDDEN == REDACTED_VALUE
    # Its spelling decodes like the start of an audio frame, but it is the hide setting's result.
    assert _privacy.scrub(REDACTED_VALUE, "input.value") == REDACTED_VALUE
    audio = "llm.input_messages.0.message.contents.0.message_content.audio.data"
    assert _privacy.scrub(REDACTED_VALUE, audio) == REDACTED_VALUE
    body = json.dumps({"messages": [{"role": "user", "content": REDACTED_VALUE}]})
    assert _privacy.scrub(body, "input.value") == body
    # A credential name still wins.
    assert _privacy.scrub(REDACTED_VALUE, "app.api_key") == "<redacted>"


def test_audio_token_counts_keep_their_numbers():
    from fi_instrumentation.auto import _privacy

    # Declared usage counters keep their numbers, though their last component names audio.
    assert _privacy.scrub(7, "gen_ai.usage.input_tokens.audio") == 7
    assert _privacy.scrub(7, "gen_ai.usage.output_tokens.audio") == 7
    # Any other audio-named number is still treated as an audio sample.
    assert _privacy.scrub(7, "metadata.input_tokens.audio") == "<omitted: media>"


def test_duplicate_json_members_never_keep_a_raw_value():
    from fi_instrumentation.auto import _privacy

    body = '{"api_key": "PLACEHOLDER-duplicate", "api_key": "<redacted>", "model": "m"}'
    clean = _privacy.scrub(body, "input.value")
    assert "PLACEHOLDER-duplicate" not in clean
    assert json.loads(clean) == {"api_key": "<redacted>", "model": "m"}
    # A discarded duplicate of any name is never sent unchecked either.
    clean = _privacy.scrub('{"note": "PLACEHOLDER-first", "note": "second"}', "input.value")
    assert json.loads(clean) == {"note": "second"}
    assert "PLACEHOLDER-first" not in clean


def test_credential_members_of_json_that_does_not_parse_are_redacted():
    from fi_instrumentation.auto import _privacy

    cut = '{"model": "m", "api_key": "PLACEHOLDER-cut-body", "messages": [{"role": "user", "content": "hel'
    clean = _privacy.scrub(cut, "input.value")
    assert clean == '{"model": "m", "api_key": "<redacted>", "messages": [{"role": "user", "content": "hel'
    # A Python repr of a dict, as some integrations record arguments.
    shown = "{'model': 'm', 'api_key': 'PLACEHOLDER-repr-value', 'headers': {'Authorization': 'Basic PLACEHOLDER'}}"
    clean = _privacy.scrub(shown, "input.value")
    assert clean == "{'model': 'm', 'api_key': '<redacted>', 'headers': {'Authorization': '<redacted>'}}"


def test_a_colon_heavy_url_is_scanned_in_linear_time():
    from fi_instrumentation.auto import _privacy

    value = "see http://" + ":" * 200000 + " for details"
    start = time.monotonic()
    assert _privacy.scrub(value, "output.value") == value
    assert time.monotonic() - start < 5


# Every value shape in the catalog, read for the work Python's backtracking engine can do on it,
# with the text that makes it do the most. Each scan must finish well inside SCAN_BOUND seconds; the
# bounded scans take milliseconds.
# - provider-api-key, \bsk-[A-Za-z0-9_-]{20,}\b: a start whose run has no word boundary 20 or more
#   characters on walks that run back; at most seven starts in one run can fail that way.
# - aws-access-key, \b(?:AKIA|ASIA)[A-Z0-9]{16}\b: a fixed length after the prefix, nothing to walk back.
# - jwt, \beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b: a start whose run is not
#   followed by a dot walks the whole run back, and every "eyJ" after a hyphen starts again in the
#   same run, so the work grows with the square of the run.
# - authorization-value, \b(?:Bearer|Basic)\s+[A-Za-z0-9+/_=-]{8,}: the spaces after the keyword are
#   walked back once, for that keyword only.
# - private-key-pem: a lazy scan to the END line or the end of the text, which always matches, once
#   per BEGIN line.
SHAPE_IDS = ("provider-api-key", "aws-access-key", "jwt", "authorization-value", "private-key-pem")
SCAN_BOUND = 0.5
# A letter outside ASCII: a word character, so there is no word boundary between it and a letter,
# but not one the shapes' runs take.
LETTER = "\N{LATIN SMALL LETTER E WITH ACUTE}"
WORST_CASES = {
    "jwt": ("eyJ-" * 25000, None),
    "jwt-after-a-first-segment": ("eyJabcdefgh." + "eyJ-" * 25000, None),
    "provider-api-key": ("sk-" * 7 + "a" * 100000 + LETTER, None),
    "provider-api-key-hyphens": ("sk-" + "-" * 100000, None),
    "aws-access-key": ("AKIA" * 25000, None),
    "authorization-value": (("Bearer" + " " * 1000 + "!") * 100, None),
    "private-key-pem": ("-----BEGIN PRIVATE KEY-----" + "-----END PRIVATE KEY----x" * 4000, "<redacted>"),
}


def test_every_value_shape_in_the_catalog_was_read_for_backtracking():
    catalog = json.loads((SPEC / "privacy.json").read_text(encoding="utf-8"))
    # A new shape fails here until its worst case is read and added above.
    assert tuple(item["id"] for item in catalog["credentials"]["value_shapes"]) == SHAPE_IDS


@pytest.mark.parametrize("case", sorted(WORST_CASES))
def test_value_shapes_are_scanned_in_bounded_time(case):
    from fi_instrumentation.auto import _privacy

    value, expected = WORST_CASES[case]
    start = time.monotonic()
    clean = _privacy.scrub(value, "input.value")
    elapsed = time.monotonic() - start
    assert clean == (value if expected is None else expected)
    assert elapsed < SCAN_BOUND, f"{case} took {elapsed:.2f}s"


def test_the_value_shapes_match_the_catalog_expressions_exactly():
    from fi_instrumentation.auto import _privacy

    catalog = json.loads((SPEC / "privacy.json").read_text(encoding="utf-8"))
    shapes = [re.compile(item["regex"], re.I if "i" in item["flags"] else 0)
              for item in catalog["credentials"]["value_shapes"]]

    def expected(text):
        for shape in shapes:
            text = shape.sub("<redacted>", text)
        return text

    # Every sequence of up to four pieces, then longer runs: starts after hyphens, several tokens in
    # one run, hyphens and a non-ASCII letter where a word boundary would end a token.
    pieces = ("eyJ", "eyJabcdefghij", ".abcdefghij", "abcdefghij", "sk-", "-", ".", LETTER, " ")
    samples = ["".join(parts) for size in range(1, 5) for parts in itertools.product(pieces, repeat=size)]
    samples += [
        "eyJ-" * 6 + "abcdefghij.abcdefghij.abcdefghij",
        "eyJabcdefghij.abcdefghij.abcdefghij-eyJabcdefghij.abcdefghij.abcdefghij",
        "eyJabcdefghij.abcdefghij.abcdefghij.eyJabcdefghij.abcdefghij.abcdefghij",
        "eyJabcdefghij.abcdefghij.abcd-efghij--",
        "x-eyJabcdefghij.abcdefghij.abcdefghij" + LETTER,
        LETTER + "eyJabcdefghij.abcdefghij.abcdefghij",
        "sk-" * 7 + "abcdefghijabcdefghij",
        "sk-" * 7 + "abcdefghijabcdefghij-",
        "sk-abcdefghijabcdefghij-sk-abcdefghijabcdefghij",
        "sk-abcdefghijabcdefghi" + LETTER,
        "-sk-abcdefghijabcdefghijk--",
    ]
    failures = []
    for sample in samples:
        # In prose, so no media, URL or other rule applies to the whole value.
        text = "note " + sample + " end"
        actual, want = _privacy.scrub(text, "note"), expected(text)
        if actual != want:
            failures.append(f"{text!r}: got {actual!r}, want {want!r}")
    assert not failures, f"{len(failures)} of {len(samples)} differ: " + "; ".join(failures[:5])


# Headerless 16-bit samples, as a raw or realtime audio part carries them: no media signature.
_PCM = base64.b64encode(bytes(range(64)) * 2).decode()
# The first bytes of a WAV file (a RIFF/WAVE signature) and of a JPEG.
_WAV = base64.b64encode(b"RIFF\x24\x08\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00" + bytes(40)).decode()
_JPEG = base64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01" + bytes(40)).decode()


def test_audio_in_a_cut_chat_body_is_omitted():
    from fi_instrumentation.auto import _privacy

    head = ('{"model": "m", "messages": [{"role": "user", "content": [{"type": "input_audio", '
            '"input_audio": {"data": "')
    tail = '", "format": "wav"}}, {"type": "text", "text": "what is said in this recor'
    for audio in (_PCM, _WAV):
        # A length limit cut the body, so it does not parse.
        cut = head + audio + tail
        assert _privacy.scrub(cut, "input.value") == head + f"<omitted: {len(audio)} base64 chars>" + tail


def test_an_image_in_a_cut_body_is_omitted():
    from fi_instrumentation.auto import _privacy

    head = ('{"contents": [{"role": "user", "parts": [{"text": "describe this"}, '
            '{"inline_data": {"mime_type": "image/jpeg", "data": "')
    # Cut inside the image: the string never closes.
    assert _privacy.scrub(head + _JPEG[:40], "input.value") == head + "<omitted: 40 base64 chars>"
    # A part with no signature of its own is omitted for the MIME type before it.
    chunk = base64.b64encode(bytes(range(1, 91))).decode()
    tail = '"}}, {"text": "and this one'
    assert _privacy.scrub(head + chunk + tail, "input.value") == (
        head + f"<omitted: {len(chunk)} base64 chars>" + tail)


def test_media_in_a_python_repr_is_omitted():
    from fi_instrumentation.auto import _privacy

    # A Python repr of a dict, as some integrations record arguments: it is not JSON.
    for audio in (_PCM, _WAV):
        shown = "{'type': 'input_audio', 'input_audio': {'data': '" + audio + "', 'format': 'wav'}}"
        assert _privacy.scrub(shown, "input.value") == (
            "{'type': 'input_audio', 'input_audio': {'data': "
            f"'<omitted: {len(audio)} base64 chars>', 'format': 'wav'}}}}")


def test_a_value_a_length_limit_may_have_cut_keeps_no_base64_run():
    from fi_instrumentation.auto import _privacy

    # A cut value no longer shows where media begins: every base64 run of eight or more characters
    # goes, words included, and a data: prefix without its comma goes too. Markers stay.
    cut = ('{"model": "m", "api_key": "PLACEHOLDER-cut-value", "note": "QUJDREVGR0hJ", '
           '"text": "summarise the attached recording", "image": "data:image/png;base6')
    assert _privacy.scrub(cut, "input.value", cut=True) == (
        '{"model": "m", "api_key": "<redacted>", "note": "<omitted: 12 base64 chars>", '
        '"text": "<omitted: 9 base64 chars> the <omitted: 8 base64 chars> <omitted: 9 base64 chars>", '
        '"image": "<omitted: data URI>')
    # A value no limit cut keeps its words.
    assert _privacy.scrub("summarise the attached recording", "input.value") == "summarise the attached recording"


def test_a_long_path_is_checked_in_bounded_time_and_drops_only_its_attribute():
    from fi_instrumentation.auto import _privacy

    # A long member name above many values, and an attribute name of many components.
    payload = json.dumps({"k" * 20000: ["a"] * 2000})
    deep = ".".join(["x"] * 70)
    start = time.monotonic()
    copy = _privacy.sanitize_span(_span({"input.value": "a prompt", "app.payload": payload, deep: "value"}))
    elapsed = time.monotonic() - start
    assert dict(copy.attributes) == {"input.value": "a prompt"}
    assert elapsed < 1, f"took {elapsed:.2f}s"


class _Loud:
    """An application object that must never be turned into text by the sanitizer."""

    calls = []

    def __str__(self):
        _Loud.calls.append("str")
        return "PLACEHOLDER-loud"

    def __repr__(self):
        _Loud.calls.append("repr")
        return "PLACEHOLDER-loud"


def test_unknown_attribute_values_are_dropped_without_calling_them():
    from fi_instrumentation.auto import _privacy

    _Loud.calls.clear()
    span = _span({"input.value": "a prompt", "app.object": _Loud(), "app.items": ("a", _Loud())})
    copy = _privacy.sanitize_span(span)
    assert dict(copy.attributes) == {"input.value": "a prompt"}
    assert _Loud.calls == []


class _ShownText(str):
    """Text whose own methods must never run when the sanitizer reads it."""

    def __str__(self):
        _Loud.calls.append("str")
        return "PLACEHOLDER-shown"

    __repr__ = __str__

    def __format__(self, spec):
        _Loud.calls.append("format")
        return "PLACEHOLDER-shown"


def test_a_text_subclass_is_sent_as_its_text_and_bytes_as_media():
    from fi_instrumentation.auto import _privacy

    _Loud.calls.clear()
    span = _span({"input.value": _ShownText("a prompt"), "app.raw": b"\x89PNG\r\n\x1a\n"})
    copy = _privacy.sanitize_span(span)
    assert dict(copy.attributes) == {"input.value": "a prompt", "app.raw": "<omitted: media>"}
    assert type(copy.attributes["input.value"]) is str
    assert _Loud.calls == []


def test_a_catalog_that_cannot_be_loaded_is_named_in_the_one_warning(monkeypatch, tmp_path, caplog):
    from fi_instrumentation.auto import _privacy

    # As in a zipped or frozen app that did not collect the package's data file.
    monkeypatch.setattr(_privacy, "CATALOG_PATH", str(tmp_path / "missing" / "privacy.json"))
    monkeypatch.setattr(_privacy, "_loaded", None)
    monkeypatch.setattr(_privacy, "_load_error", None)
    monkeypatch.setattr(_privacy, "_warned", False)
    with caplog.at_level(logging.WARNING, logger="fi_instrumentation.auto"):
        copy = _privacy.sanitize_span(_span({"input.value": "a prompt"}))
        _privacy.sanitize_span(_span({"input.value": "a prompt"}))
    assert copy.name == "traceai span"
    assert dict(copy.attributes) == {}
    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "privacy catalog could not be loaded (FileNotFoundError)" in warnings[0]


def test_an_attribute_too_deep_to_check_is_dropped_alone():
    from fi_instrumentation.auto import _privacy

    deep = "[" * 40 + "]" * 40
    copy = _privacy.sanitize_span(_span({"input.value": "a prompt", "app.deep": deep}))
    assert dict(copy.attributes) == {"input.value": "a prompt"}
    assert copy.name == "chat"


def test_a_span_that_cannot_be_checked_keeps_only_identity_and_timing(monkeypatch):
    from fi_instrumentation.auto import _privacy

    def unavailable():
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(_privacy, "_catalog", unavailable)
    span = _span({"input.value": "a prompt"}, events=[Event("retry", {"input.value": "a prompt"})],
                 status=Status(StatusCode.ERROR, "the model refused: a prompt"))
    copy = _privacy.sanitize_span(span)
    assert copy.name == "traceai span"
    assert dict(copy.attributes) == {}
    assert list(copy.events) == [] and list(copy.links) == []
    assert dict(copy.resource.attributes) == {}
    assert (copy.context.trace_id, copy.context.span_id) == (span.context.trace_id, span.context.span_id)
    assert (copy.start_time, copy.end_time, copy.kind) == (span.start_time, span.end_time, span.kind)
    assert copy.status.status_code == StatusCode.ERROR
    assert copy.status.description is None


def test_resource_scope_and_tracestate_are_masked_on_the_copy_only():
    from fi_instrumentation.auto import _privacy

    state = trace_api.TraceState([("vendor", "keep"), ("session", "PLACEHOLDER-trace-state")])
    resource = Resource({"service.name": "checkout", "deploy.api_key": "PLACEHOLDER-resource"})
    span = _span({"gen_ai.span.kind": "LLM"}, resource=resource, trace_state=state)
    copy = _privacy.sanitize_span(span)
    assert dict(copy.resource.attributes) == {"service.name": "checkout", "deploy.api_key": "<redacted>"}
    assert dict(copy.context.trace_state) == {"vendor": "keep", "session": "<redacted>"}
    assert copy.instrumentation_scope is span.instrumentation_scope
    # The span given to the sanitizer is not changed.
    assert span.resource.attributes["deploy.api_key"] == "PLACEHOLDER-resource"
    assert span.context.trace_state.get("session") == "PLACEHOLDER-trace-state"
    # A resource with nothing to mask is passed on as it is.
    plain = _span({}, resource=Resource({"service.name": "checkout"}))
    assert _privacy.sanitize_span(plain).resource is plain.resource
