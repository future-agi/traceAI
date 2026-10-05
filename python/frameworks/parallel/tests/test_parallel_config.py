"""instrument() options: TraceConfig hide flags and the capture switches."""

from __future__ import annotations

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

from fi_instrumentation import REDACTED_VALUE, TraceConfig  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from _parallel_support import (  # noqa: E402
    FAIL_ECHO,
    PARALLEL_KEY,
    SEARCH_ID,
    WARN,
    WARN_ECHO,
    WARNING_MESSAGE,
    WARNING_TYPES,
    FakeParallel,
    attrs,
    instrumented,
    sync_client,
)
from traceai_parallel import ParallelInstrumentor  # noqa: E402

# Dropped by hide_inputs. input.value is kept as TraceConfig's placeholder.
INPUT_KEYS = ("gen_ai.retrieval.query", "parallel.urls", "parallel.objective")
EMAIL = "jane.doe@example.com"
EMAIL_TOKEN = "<EMAIL_ADDRESS>"


@pytest.fixture()
def fake():
    with FakeParallel() as server:
        yield server


def _calls(fake):
    client = sync_client(fake)
    client.search(search_queries=["secret query", "two"], objective="secret goal", mode="fast")
    client.extract(
        urls=["https://x.example/secret", "https://x.example/b"],
        search_queries=["secret focus"],
        objective="secret goal",
    )


def test_hide_inputs_drops_every_request_content_key_but_keeps_counts(fake):
    options = {"config": TraceConfig(hide_inputs=True), "capture_urls": True, "capture_objective": True}
    with instrumented(**options) as traced:
        _calls(fake)

    search, extract = (attrs(span) for span in traced.spans())
    for values in (search, extract):
        for key in INPUT_KEYS:
            assert key not in values, key
        # The query is replaced by the TraceConfig placeholder, not dropped.
        assert values["input.value"] == REDACTED_VALUE == "__REDACTED__"
    assert search["parallel.query_count"] == 2
    assert search["parallel.mode"] == "fast"
    assert extract["parallel.url_count"] == 2
    assert extract["parallel.query_count"] == 1
    assert "secret" not in traced.wire()


def test_fi_hide_inputs_environment_variable_is_honoured(fake, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    with instrumented(capture_urls=True, capture_objective=True) as traced:
        _calls(fake)

    for span in traced.spans():
        for key in INPUT_KEYS:
            assert key not in attrs(span), key
        assert attrs(span)["input.value"] == REDACTED_VALUE
    assert "secret" not in traced.wire()


def test_hide_inputs_records_no_placeholder_without_queries(fake):
    # Extract without search_queries has no query to hide.
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        sync_client(fake).extract(urls=["https://x.example/a"])

    assert "input.value" not in attrs(traced.one())


@pytest.mark.parametrize("source", ["config", "env"])
def test_pii_redaction_removes_an_email_from_the_query(fake, monkeypatch, source):
    options = {}
    if source == "config":
        options["config"] = TraceConfig(pii_redaction=True)
    else:
        monkeypatch.setenv("FI_PII_REDACTION", "true")
    query = "find {0} for {1}".format(PARALLEL_KEY, EMAIL)
    with instrumented(**options) as traced:
        sync_client(fake).search(search_queries=[query, "second"])

    values = attrs(traced.one())
    # The key and the email are each replaced once, by their own placeholder.
    expected = "find [redacted] for {0}\nsecond".format(EMAIL_TOKEN)
    assert values["gen_ai.retrieval.query"] == expected
    assert values["input.value"] == expected
    wire = traced.wire()
    assert EMAIL not in wire
    assert PARALLEL_KEY not in wire
    # Redaction is on the span only; the vendor still got the caller's query.
    assert fake.calls[0][2]["search_queries"][0] == query
    # The patterns also match ids (the README says so): ten digits look like a
    # phone number.
    assert SEARCH_ID == "search_0123456789"
    assert values["parallel.search_id"] == "search_<PHONE_NUMBER>"


def test_without_pii_redaction_the_email_is_recorded(fake):
    # Control for the test above: PII redaction is off by default.
    with instrumented() as traced:
        sync_client(fake).search(search_queries=["for " + EMAIL])

    assert attrs(traced.one())["input.value"] == "for " + EMAIL


def test_pii_is_redacted_before_the_cap_so_no_partial_email_survives(fake):
    # The email straddles the 1 KB cap: redacting after the cap would leave
    # "jane.doe@exa", which no longer looks like an email.
    query = "a" * 1010 + " " + EMAIL
    with instrumented(config=TraceConfig(pii_redaction=True)) as traced:
        sync_client(fake).search(search_queries=[query])

    recorded = attrs(traced.one())["input.value"]
    assert recorded == ("a" * 1010 + " " + EMAIL_TOKEN)[:1024]
    assert "jane.doe" not in traced.wire()


def test_pii_redaction_covers_error_text_and_the_hide_placeholder(fake):
    # FAIL_ECHO makes the fake echo the query into its error message, which
    # reaches the status description and the exception event.
    query = FAIL_ECHO + " " + EMAIL
    config = TraceConfig(pii_redaction=True, hide_inputs=True)
    with instrumented(config=config) as traced:
        with pytest.raises(Exception) as raised:
            sync_client(fake).search(search_queries=[query])

    assert EMAIL in str(raised.value)
    span = traced.one()
    assert attrs(span)["input.value"] == REDACTED_VALUE
    assert span.status.status_code is StatusCode.ERROR
    assert EMAIL_TOKEN in span.status.description
    (event,) = span.events
    assert EMAIL_TOKEN in event.attributes["exception.message"]
    assert EMAIL_TOKEN in event.attributes["exception.stacktrace"]
    assert EMAIL not in traced.wire()


def test_capture_switches_record_inputs_when_inputs_are_not_hidden(fake):
    # Control for the two tests above: the same calls do carry the content.
    with instrumented(capture_urls=True, capture_objective=True) as traced:
        _calls(fake)

    search, extract = (attrs(span) for span in traced.spans())
    assert search["input.value"] == "secret query\ntwo"
    assert search["parallel.objective"] == "secret goal"
    assert list(extract["parallel.urls"]) == ["https://x.example/secret", "https://x.example/b"]
    assert extract["input.value"] == "secret focus"


def test_hide_outputs_drops_warning_messages_but_keeps_types(fake):
    with instrumented(config=TraceConfig(hide_outputs=True)) as traced:
        sync_client(fake).search(search_queries=[WARN])

    span = traced.one()
    assert attrs(span)["parallel.warning_count"] == 2
    assert [dict(event.attributes) for event in span.events] == [
        {"parallel.warning.type": WARNING_TYPES[0]},
        {"parallel.warning.type": WARNING_TYPES[1]},
    ]
    assert WARNING_MESSAGE not in traced.wire()
    # Inputs are not outputs: the query is still recorded.
    assert attrs(span)["input.value"] == WARN


def test_warning_messages_are_redacted_and_capped(fake):
    # WARN_ECHO makes the fake echo the request's x-api-key into an oversized
    # warning message.
    with instrumented() as traced:
        sync_client(fake).search(search_queries=[WARN_ECHO])

    (event,) = traced.one().events
    message = event.attributes["parallel.warning.message"]
    assert message.startswith("echo [redacted] \u20ac")
    assert len(message.encode("utf-8")) <= 1024
    assert PARALLEL_KEY not in traced.wire()


@pytest.mark.parametrize(
    "options",
    [
        {"config": {"hide_inputs": True}},
        {"config": "TraceConfig"},
        {"capture_urls": "yes"},
        {"capture_objective": 1},
    ],
    ids=["config-dict", "config-str", "capture-urls-str", "capture-objective-int"],
)
def test_invalid_options_raise_type_error_and_wrap_nothing(options):
    from parallel import _client

    before = vars(_client.Parallel)["search"]
    with pytest.raises(TypeError):
        ParallelInstrumentor().instrument(**options)
    assert vars(_client.Parallel)["search"] is before
    # A failed instrument() leaves the instrumentor usable.
    with instrumented():
        assert vars(_client.Parallel)["search"] is not before
    assert vars(_client.Parallel)["search"] is before
