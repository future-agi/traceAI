"""The shared privacy catalog on the copy of a span the zero-code agent sends to Future AGI.

Every case in the spec's fixture bundle runs through ``_privacy.scrub``, the function the export
sanitizer applies to every attribute value (the attribute key is the path, the span's other
attributes are the siblings), to every value inside JSON text in an attribute, and to every name,
status description and other text it sends. The bundle is read from the repository at test time.
"""

import json
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

# Sites Python's export has. Request and response bodies reach Future AGI as JSON text in
# attributes (input.value, output.value, raw.input, raw.output); the sanitizer parses that text
# and walks it with the same function, so the body cases run through it too.
EXPORT_SITES = (
    "request_body", "response_body", "attribute_keys", "flattened_paths", "indexed_paths",
    "mime_siblings", "magic_bytes", "free_text_urls",
)
# Sites only a wire capture has, and why Python skips them.
WIRE_ONLY = {
    "request_headers": (
        "Python's zero-code agent has no transport capture: it never sees the headers a client "
        "sends, and the request roots it forwards lose their captured request and response headers "
        "before the catalog runs"),
    "request_query": (
        "Python's zero-code agent has no transport capture: it never sees the URL of a request a "
        "client sends, and the request roots it forwards lose their query strings before the catalog "
        "runs; URLs inside exported attribute values and text are covered by the attribute and "
        "free-text cases"),
}


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
