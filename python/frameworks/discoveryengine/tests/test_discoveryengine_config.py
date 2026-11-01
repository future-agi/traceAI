"""Query capture follows TraceConfig (on by default, FI_HIDE_INPUTS hides it); PII redaction; error text."""

from __future__ import annotations

import asyncio

import pytest
from google.api_core import exceptions as core_exceptions
from opentelemetry.trace import StatusCode

from _discoveryengine_support import (
    FAIL_DENIED,
    FAIL_DETAILS,
    NATIVE_ERROR_POLICY,
    SERVING_CONFIG,
    FakeDiscoveryEngine,
    assert_hidden_query_error,
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
# An apostrophe, a double quote, a backslash and a non-ASCII letter: the
# status detail the fake adds quotes it escaped (protobuf text format).
QUOTED_QUERY = FAIL_DETAILS + " Zelda's \"quoted\" a\\b caf\xe9"
QUOTED_DETAIL = r'Zelda\'s \"quoted\" a\\b caf' + "\xe9"
OTHER_CONTENT_HIDE_FLAGS = (
    "hide_outputs",
    "hide_input_messages",
    "hide_output_messages",
    "hide_input_text",
    "hide_output_text",
)


@pytest.fixture()
def fake():
    with FakeDiscoveryEngine() as server:
        yield server


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    flags = ("hide_inputs", "hide_error_details", "pii_redaction", *OTHER_CONTENT_HIDE_FLAGS)
    for flag in flags:
        monkeypatch.delenv("FI_" + flag.upper(), raising=False)


def _error_texts(span):
    exception = event(span, "exception") or {}
    # No stack trace is ever recorded: frames carry local file paths.
    assert "exception.stacktrace" not in exception
    return (
        span.status.description or "",
        exception.get("exception.message", ""),
    )


def _denied(fake, traced_options):
    with instrumented(**traced_options) as traced:
        with pytest.raises(core_exceptions.PermissionDenied) as raised:
            search_client(fake).search(request=search_request(DENIED_QUERY))
    # The server echoed the query; the caller still sees it.
    assert DENIED_QUERY in str(raised.value)
    return traced.one()


def test_the_query_is_recorded_by_default_and_kept_in_error_text(fake):
    # TraceConfig default (DEFAULT_HIDE_INPUTS = False), as in every traceAI
    # instrumentor: the query reaches the span and the server's echo of it
    # stays in the error text.
    span = _denied(fake, {})
    values = attrs(span)
    assert values["input.value"] == DENIED_QUERY
    assert values["gen_ai.retrieval.query"] == DENIED_QUERY
    for text in _error_texts(span):
        assert DENIED_QUERY in text


@pytest.mark.parametrize("source", ["config", "env"])
def test_hide_inputs_removes_the_query_everywhere_including_error_text(fake, monkeypatch, source):
    # Control for the test above: FI_HIDE_INPUTS (or TraceConfig) hides it.
    options = {}
    if source == "config":
        options["config"] = _config(hide_inputs=True)
    else:
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    span = _denied(fake, options)
    values = attrs(span)
    assert values["input.value"] == REDACTED_VALUE
    assert "gen_ai.retrieval.query" not in values
    assert_hidden_query_error(span, "google.api_core.exceptions.PermissionDenied")
    for text in _error_texts(span):
        assert DENIED_QUERY not in text
        assert EMAIL not in text
    assert DENIED_QUERY not in span.to_json()


def _quoted(fake, traced_options):
    with instrumented(**traced_options) as traced:
        with pytest.raises(core_exceptions.InvalidArgument) as raised:
            search_client(fake).search(request=search_request(QUOTED_QUERY))
    # google-api-core appends the BadRequest detail, escaped, to the message.
    assert QUOTED_QUERY in str(raised.value)
    assert QUOTED_DETAIL in str(raised.value)
    return traced.one()


def test_the_server_echo_of_the_query_includes_an_escaped_copy(fake):
    # Control for the test below: by default (query recorded) both copies are kept.
    span = _quoted(fake, {})
    for text in _error_texts(span):
        assert QUOTED_QUERY in text
        assert QUOTED_DETAIL in text


@pytest.mark.parametrize("source", ["config", "env"])
def test_escaped_copies_of_the_query_are_removed_from_error_text(fake, monkeypatch, source):
    options = _hidden_options(monkeypatch, source)
    span = _quoted(fake, options)
    assert_hidden_query_error(span, "google.api_core.exceptions.InvalidArgument")
    for text in _error_texts(span):
        assert "Zelda" not in text
        assert "quoted" not in text
    assert "Zelda" not in span.to_json()


# ASCII control characters other than \t, \n and \r, and DEL: protobuf text
# format writes them as 3-digit octal (\013, \033, \177), which none of the
# repr, JSON or CHexEscape copies matches.
CONTROL_QUERY = FAIL_DETAILS + " Zelda\x0bcontrol\x1bcopy\x7fend"
CONTROL_DETAIL = r"Zelda\013control\033copy\177end"


def test_the_status_detail_writes_control_characters_as_octal(fake):
    # Control: the escaped copy really is in the error the caller sees.
    with instrumented():
        with pytest.raises(core_exceptions.InvalidArgument) as raised:
            search_client(fake).search(request=search_request(CONTROL_QUERY))
    assert CONTROL_DETAIL in str(raised.value)


@pytest.mark.parametrize("source", ["config", "env"])
def test_octal_escaped_copies_of_the_query_are_removed_from_error_text(fake, monkeypatch, source):
    options = _hidden_options(monkeypatch, source)
    with instrumented(**options) as traced:
        with pytest.raises(core_exceptions.InvalidArgument) as raised:
            search_client(fake).search(request=search_request(CONTROL_QUERY))
    assert CONTROL_DETAIL in str(raised.value)
    span = traced.one()
    assert_hidden_query_error(span, "google.api_core.exceptions.InvalidArgument")
    for text in _error_texts(span):
        assert "Zelda" not in text and "control" not in text, text
    assert "Zelda" not in span.to_json()


def _config(**fields):
    from fi_instrumentation import TraceConfig

    return TraceConfig(**fields)


def _hidden_options(monkeypatch, source):
    if source == "config":
        return {"config": _config(hide_inputs=True)}
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    return {}


def test_hide_inputs_records_only_the_placeholder(fake):
    with instrumented(config=_config(hide_inputs=True)) as traced:
        search_client(fake).search(request=search_request("plain"))

    values = attrs(traced.one())
    assert values["input.value"] == REDACTED_VALUE
    assert "gen_ai.retrieval.query" not in values
    assert "plain" not in traced.wire()


def test_capture_query_is_ignored_with_a_warning(fake, caplog):
    # The pre-release package-specific switch is gone (one privacy switch:
    # TraceConfig). Passing it changes nothing and logs why.
    with caplog.at_level("WARNING", logger="traceai_discoveryengine"):
        with instrumented(capture_query=False) as traced:
            search_client(fake).search(request=search_request("still recorded"))

    assert attrs(traced.one())["input.value"] == "still recorded"
    assert "capture_query is ignored" in caplog.text


def test_hide_outputs_changes_nothing_because_no_output_text_is_recorded(fake):
    with instrumented() as plain:
        answer_client(fake).answer_query(request=answer_request("same"))
    with instrumented(config=_config(hide_outputs=True)) as hidden:
        answer_client(fake).answer_query(request=answer_request("same"))

    assert attrs(hidden.one()) == attrs(plain.one())


@pytest.mark.parametrize("source", ["config", "env"])
@pytest.mark.parametrize("flag", OTHER_CONTENT_HIDE_FLAGS)
def test_other_content_hide_flags_follow_core_error_policy(fake, monkeypatch, flag, source):
    # These flags do not hide a retrieval query, but native core derives error
    # privacy from all content-hide flags, not only hide_inputs. No opt-out.
    plain = _denied(fake, {})
    options = {}
    if source == "config":
        options["config"] = _config(**{flag: True})
    else:
        monkeypatch.setenv("FI_" + flag.upper(), "true")
    span = _denied(fake, options)
    assert attrs(span)["input.value"] == DENIED_QUERY
    assert attrs(span)["discoveryengine.error.code"] == 403
    assert attrs(span)["discoveryengine.error.status"] == "PERMISSION_DENIED"
    assert span.status.status_code is StatusCode.ERROR
    assert [item.name for item in span.events] == ["exception"]
    if NATIVE_ERROR_POLICY:
        assert span.status.description is None
        assert event(span, "exception") == {
            "exception.type": "google.api_core.exceptions.PermissionDenied"
        }
    else:
        assert span.status.description == plain.status.description
        assert event(span, "exception") == event(plain, "exception")
        for text in _error_texts(span):
            assert DENIED_QUERY in text


@pytest.mark.parametrize("source", ["config", "env"])
def test_pii_redaction_covers_the_query_and_the_error_text(fake, monkeypatch, source):
    options = {}
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
    with instrumented(config=_config(pii_redaction=True)) as traced:
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
    with instrumented() as traced:
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

    with instrumented(config=_config(hide_inputs=True)) as traced:
        asyncio.run(call())

    span = traced.one()
    assert attrs(span)["input.value"] == REDACTED_VALUE
    assert_hidden_query_error(span, "google.api_core.exceptions.PermissionDenied")
    assert DENIED_QUERY not in span.to_json()


@pytest.mark.parametrize(
    "options, message",
    [
        ({"config": {"hide_inputs": True}}, "config must be a fi_instrumentation.TraceConfig"),
        ({"config": "hide"}, "config must be a fi_instrumentation.TraceConfig"),
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
