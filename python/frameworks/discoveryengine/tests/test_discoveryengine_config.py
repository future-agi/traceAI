"""Content capture, TraceConfig hide flags and PII redaction, including error text."""

from __future__ import annotations

import asyncio

import pytest
from google.api_core import exceptions as core_exceptions

from _discoveryengine_support import (
    FAIL_DENIED,
    SERVING_CONFIG,
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    async_search_client,
    attrs,
    event,
    instrumented,
    search_client,
    search_request,
)
from traceai_discoveryengine import DiscoveryEngineInstrumentor, _wrappers

EMAIL = "jane.doe@example.com"
EMAIL_TOKEN = "<EMAIL_ADDRESS>"
REDACTED_VALUE = "__REDACTED__"
DENIED_QUERY = FAIL_DENIED + " ask about " + EMAIL


@pytest.fixture()
def fake():
    with FakeDiscoveryEngine() as server:
        yield server


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_PII_REDACTION"):
        monkeypatch.delenv(name, raising=False)


def _error_texts(span):
    exception = event(span, "exception") or {}
    return (
        span.status.description or "",
        exception.get("exception.message", ""),
        exception.get("exception.stacktrace", ""),
    )


def _denied(fake, traced_options):
    with instrumented(**traced_options) as traced:
        with pytest.raises(core_exceptions.PermissionDenied) as raised:
            search_client(fake).search(request=search_request(DENIED_QUERY))
    # The server echoed the query; the caller still sees it.
    assert DENIED_QUERY in str(raised.value)
    return traced.one()


def test_query_text_is_off_by_default_everywhere_including_error_text(fake):
    span = _denied(fake, {})
    values = attrs(span)
    assert "input.value" not in values and "gen_ai.retrieval.query" not in values
    for text in _error_texts(span):
        assert text
        assert DENIED_QUERY not in text
        assert EMAIL not in text
    assert REDACTED_VALUE in span.status.description
    assert DENIED_QUERY not in span.to_json()


def test_capture_query_records_it_and_keeps_it_in_error_text(fake):
    # Control for the test above: with capture on, the query reaches the
    # span and the server's echo of it stays in the error text.
    span = _denied(fake, {"capture_query": True})
    values = attrs(span)
    assert values["input.value"] == DENIED_QUERY
    assert values["gen_ai.retrieval.query"] == DENIED_QUERY
    for text in _error_texts(span):
        assert DENIED_QUERY in text


def _config(**fields):
    from fi_instrumentation import TraceConfig

    return TraceConfig(**fields)


@pytest.mark.parametrize("source", ["config", "env"])
def test_hide_inputs_wins_over_capture_query_everywhere(fake, monkeypatch, source):
    options = {"capture_query": True}
    if source == "config":
        options["config"] = _config(hide_inputs=True)
    else:
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    span = _denied(fake, options)

    values = attrs(span)
    assert values["input.value"] == REDACTED_VALUE
    assert "gen_ai.retrieval.query" not in values
    for text in _error_texts(span):
        assert DENIED_QUERY not in text and EMAIL not in text
    assert DENIED_QUERY not in span.to_json()


def test_hide_inputs_without_capture_records_no_placeholder(fake):
    with instrumented(config=_config(hide_inputs=True)) as traced:
        search_client(fake).search(request=search_request("plain"))

    assert "input.value" not in attrs(traced.one())


def test_hide_outputs_changes_nothing_because_no_output_text_is_recorded(fake):
    with instrumented(capture_query=True) as plain:
        answer_client(fake).answer_query(request=answer_request("same"))
    with instrumented(capture_query=True, config=_config(hide_outputs=True)) as hidden:
        answer_client(fake).answer_query(request=answer_request("same"))

    assert attrs(hidden.one()) == attrs(plain.one())


@pytest.mark.parametrize("source", ["config", "env"])
def test_pii_redaction_covers_the_query_and_the_error_text(fake, monkeypatch, source):
    options = {"capture_query": True}
    if source == "config":
        options["config"] = _config(pii_redaction=True)
    else:
        monkeypatch.setenv("FI_PII_REDACTION", "true")
    span = _denied(fake, options)

    assert attrs(span)["input.value"] == FAIL_DENIED + " ask about " + EMAIL_TOKEN
    for text in _error_texts(span):
        assert EMAIL not in text
        assert EMAIL_TOKEN in text
    assert EMAIL not in span.to_json()


def test_pii_straddling_the_cap_is_replaced_before_the_cut(fake):
    query = "x" * (_wrappers.MAX_VALUE_BYTES - 8) + " " + EMAIL
    with instrumented(capture_query=True, config=_config(pii_redaction=True)) as traced:
        search_client(fake).search(request=search_request(query))

    value = attrs(traced.one())["input.value"]
    assert "jane" not in value
    assert len(value.encode("utf-8")) <= _wrappers.MAX_VALUE_BYTES


def test_pii_redaction_also_matches_a_project_number_in_resource_names(fake):
    # Documented: the phone pattern matches the last ten digits of a GCP
    # project number, so resource names lose part of it with pii_redaction.
    serving_config = SERVING_CONFIG.replace("projects/test-project/", "projects/123456789012/")
    with instrumented(config=_config(pii_redaction=True)) as traced:
        search_client(fake).search(request=search_request(serving_config=serving_config))

    value = attrs(traced.one())["discoveryengine.serving_config"]
    assert value == serving_config.replace("123456789012", "12<PHONE_NUMBER>")


def test_the_query_is_capped_on_a_character_boundary(fake):
    query = "\u20ac" * 600  # 1800 bytes of UTF-8
    with instrumented(capture_query=True) as traced:
        search_client(fake).search(request=search_request(query))

    value = attrs(traced.one())["input.value"]
    assert value == "\u20ac" * (_wrappers.MAX_VALUE_BYTES // 3)
    assert len(value.encode("utf-8")) == 1023


def test_using_the_default_config_matches_an_explicit_default(fake):
    with instrumented() as implicit:
        search_client(fake).search(request=search_request())
    with instrumented(config=_config()) as explicit:
        search_client(fake).search(request=search_request())

    assert attrs(implicit.one()) == attrs(explicit.one())


def test_async_calls_honour_the_same_settings(fake):
    async def call():
        client = async_search_client(fake)
        try:
            with pytest.raises(core_exceptions.PermissionDenied):
                await client.search(request=search_request(DENIED_QUERY))
        finally:
            await client.transport.close()

    with instrumented(capture_query=True, config=_config(hide_inputs=True)) as traced:
        asyncio.run(call())

    span = traced.one()
    assert attrs(span)["input.value"] == REDACTED_VALUE
    assert DENIED_QUERY not in span.to_json()


@pytest.mark.parametrize(
    "options, message",
    [
        ({"config": {"hide_inputs": True}}, "config must be a fi_instrumentation.TraceConfig"),
        ({"capture_query": "yes"}, "capture_query must be a bool"),
        ({"capture_query": 1}, "capture_query must be a bool"),
    ],
)
def test_bad_options_raise_type_error_and_wrap_nothing(options, message):
    from google.cloud.discoveryengine_v1 import SearchServiceClient

    before = vars(SearchServiceClient)["search"]
    instrumentor = DiscoveryEngineInstrumentor()
    with pytest.raises(TypeError, match=message):
        instrumentor.instrument(**options)
    try:
        assert vars(SearchServiceClient)["search"] is before
        assert not instrumentor.is_instrumented_by_opentelemetry
    finally:
        instrumentor.uninstrument()
    assert vars(SearchServiceClient)["search"] is before
