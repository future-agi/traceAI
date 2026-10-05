"""instrument() options: TraceConfig hide flags and the capture switches."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

from fi_instrumentation import REDACTED_VALUE, TraceConfig  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from _parallel_support import (  # noqa: E402
    FAIL_ECHO,
    NOTICE_ECHO,
    PARALLEL_KEY,
    SEARCH_ID,
    WARN,
    WARN_ECHO,
    WARNING_MESSAGE,
    WARNING_TYPES,
    FakeParallel,
    async_client,
    attrs,
    instrumented,
    sync_client,
)
from traceai_parallel import ParallelInstrumentor  # noqa: E402

# Dropped by hide_inputs. input.value is kept as TraceConfig's placeholder.
INPUT_KEYS = ("gen_ai.retrieval.query", "parallel.urls", "parallel.objective")
EMAIL = "jane.doe@example.com"
EMAIL_TOKEN = "<EMAIL_ADDRESS>"
# Extract without search_queries: the first URL makes the fake fail and quote
# the request back.
URL_ECHO = "https://x.example/" + FAIL_ECHO + "?token=url-secret"


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


def _error_texts(span):
    """The three recorded error texts: status description, exception message, stacktrace."""
    (event,) = span.events
    assert event.name == "exception"
    return [
        span.status.description,
        event.attributes["exception.message"],
        event.attributes["exception.stacktrace"],
    ]


def _failed(traced, call, **kwargs):
    """Run a call the fake rejects; return the vendor error and the span."""
    with pytest.raises(Exception) as raised:
        call(**kwargs)
    assert type(raised.value).__name__ == "BadRequestError"
    return raised.value, traced.spans()[-1]


@pytest.mark.parametrize("pii", [False, True], ids=["hide", "hide-and-pii"])
def test_hide_inputs_removes_the_query_from_error_text(fake, pii):
    # FAIL_ECHO makes the fake quote the query back in its error message, which
    # reaches the status description and the exception event.
    query = FAIL_ECHO + " " + EMAIL
    config = TraceConfig(pii_redaction=pii, hide_inputs=True)
    with instrumented(config=config) as traced:
        error, span = _failed(traced, sync_client(fake).search, search_queries=[query])

    # The caller still gets the server's own message.
    assert "rejected request: " + query in str(error)
    assert attrs(span)["input.value"] == REDACTED_VALUE
    assert span.status.status_code is StatusCode.ERROR
    for text in _error_texts(span):
        assert "rejected request: " + REDACTED_VALUE in text
        assert FAIL_ECHO not in text
    wire = traced.wire()
    assert EMAIL not in wire
    # The whole query is replaced before the PII pass, so no token is left.
    assert EMAIL_TOKEN not in wire
    assert "jane.doe" not in wire


@pytest.mark.parametrize(
    "operation, kwargs",
    [
        (
            "search",
            {
                "search_queries": [FAIL_ECHO + " first-secret", "second-secret"],
                "objective": "objective-secret",
            },
        ),
        (
            "extract",
            {"urls": [URL_ECHO, "https://x.example/b-secret"], "objective": "objective-secret"},
        ),
        (
            "extract",
            {
                "urls": ["https://x.example/a-secret", "https://x.example/b-secret"],
                "search_queries": [FAIL_ECHO + " focus-secret"],
            },
        ),
    ],
    ids=["search-queries-objective", "extract-urls-objective", "extract-urls-queries"],
)
def test_hide_inputs_removes_every_query_url_and_objective_from_error_text(
    fake, operation, kwargs
):
    # The capture switches are off: the inputs are removed whether or not the
    # span would have carried them as attributes.
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        error, span = _failed(traced, getattr(sync_client(fake), operation), **kwargs)

    assert "secret" in str(error)
    # Each input becomes one placeholder; the server's own words stay.
    quoted = "rejected request: " + " | ".join([REDACTED_VALUE] * 3)
    for text in _error_texts(span):
        assert quoted in text
    assert "secret" not in traced.wire()


def test_hidden_inputs_are_replaced_longest_first_in_one_pass(fake):
    # The objective contains the first query, and the second query is part of
    # the placeholder: each input still becomes exactly one placeholder.
    query = FAIL_ECHO + " alpha"
    kwargs = {"search_queries": [query, "RED"], "objective": query + " beta"}
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        _, span = _failed(traced, sync_client(fake).search, **kwargs)

    description, message, _ = _error_texts(span)
    quoted = "rejected request: " + " | ".join([REDACTED_VALUE] * 3) + "'"
    assert quoted in description
    assert quoted in message
    assert "alpha" not in traced.wire()


def test_hide_inputs_removes_the_query_after_the_key_is_redacted(fake):
    # The query carries the API key. The error text has the key redacted
    # first, so the query is matched as it reads after that.
    query = "{0} keyquery-secret {1}".format(FAIL_ECHO, PARALLEL_KEY)
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        _, span = _failed(traced, sync_client(fake).search, search_queries=[query])

    for text in _error_texts(span):
        assert "rejected request: " + REDACTED_VALUE in text
    wire = traced.wire()
    assert PARALLEL_KEY not in wire
    assert "keyquery-secret" not in wire


def test_pii_redaction_covers_error_text_when_inputs_are_not_hidden(fake):
    query = FAIL_ECHO + " " + EMAIL
    with instrumented(config=TraceConfig(pii_redaction=True)) as traced:
        error, span = _failed(traced, sync_client(fake).search, search_queries=[query])

    assert EMAIL in str(error)
    for text in _error_texts(span):
        assert "rejected request: {0} {1}".format(FAIL_ECHO, EMAIL_TOKEN) in text
    assert EMAIL not in traced.wire()


def test_without_hide_inputs_error_text_is_recorded_as_thrown(fake):
    # Control for the hide_inputs tests: the same inputs stay in error text.
    kwargs = {
        "urls": ["https://x.example/a", "https://x.example/b"],
        "search_queries": [FAIL_ECHO + " focus"],
        "objective": "goal",
    }
    with instrumented() as traced:
        error, span = _failed(traced, sync_client(fake).extract, **kwargs)

    message = str(error)
    expected = "rejected request: {0} focus | https://x.example/a | https://x.example/b | goal"
    assert expected.format(FAIL_ECHO) in message
    description, recorded, stacktrace = _error_texts(span)
    assert description == "BadRequestError: " + message
    assert recorded == message
    assert message in stacktrace
    assert REDACTED_VALUE not in traced.wire()


@pytest.mark.parametrize("hide", [False, True], ids=["shown", "hidden"])
def test_hide_inputs_removes_inputs_from_warning_messages(fake, hide):
    # NOTICE_ECHO makes the fake quote the request in a warning message.
    query = NOTICE_ECHO + " notice-secret"
    with instrumented(config=TraceConfig(hide_inputs=hide)) as traced:
        sync_client(fake).search(search_queries=[query], objective="goal-secret")

    (event,) = traced.one().events
    message = event.attributes["parallel.warning.message"]
    if hide:
        assert message == "about your request: {0} | {0}".format(REDACTED_VALUE)
        assert "secret" not in traced.wire()
    else:
        assert message == "about your request: {0} | goal-secret".format(query)


@pytest.mark.parametrize("pii", [False, True], ids=["hide", "hide-and-pii"])
def test_async_hide_inputs_removes_inputs_from_error_and_warning_text(fake, pii):
    calls = [
        ("search", {"search_queries": [FAIL_ECHO + " " + EMAIL], "objective": "goal-secret"}),
        ("extract", {"urls": [URL_ECHO], "objective": "goal-secret"}),
        ("search", {"search_queries": [NOTICE_ECHO + " notice-secret"]}),
    ]

    async def run_async() -> None:
        client = async_client(fake)
        try:
            for method, kwargs in calls[:2]:
                with pytest.raises(Exception):
                    await getattr(client, method)(**kwargs)
            method, kwargs = calls[2]
            await getattr(client, method)(**kwargs)
        finally:
            await client.close()

    config = TraceConfig(hide_inputs=True, pii_redaction=pii)
    with instrumented(config=config) as traced:
        client = sync_client(fake)
        for method, kwargs in calls[:2]:
            _failed(traced, getattr(client, method), **kwargs)
        method, kwargs = calls[2]
        getattr(client, method)(**kwargs)
        sync_spans = traced.spans()
        traced.exporter.clear()
        asyncio.run(run_async())
        async_spans = traced.spans()

    wire = "".join(span.to_json() for span in sync_spans + async_spans)

    assert len(sync_spans) == len(async_spans) == 3
    for sync_span, async_span in zip(sync_spans[:2], async_spans[:2]):
        sync_texts, async_texts = _error_texts(sync_span), _error_texts(async_span)
        # Status description and exception message match the sync call's;
        # the stacktraces differ only in their frames.
        assert async_texts[:2] == sync_texts[:2]
        for text in async_texts:
            assert "rejected request: " + REDACTED_VALUE in text
    (event,) = async_spans[2].events
    assert event.attributes["parallel.warning.message"] == "about your request: " + REDACTED_VALUE
    assert "secret" not in wire
    assert "jane.doe" not in wire


def test_unreadable_inputs_under_hide_inputs_record_no_error_text(fake, monkeypatch):
    # If the inputs to hide cannot be read, no free error text is recorded,
    # and the caller still gets the vendor's own error.
    from traceai_parallel import _wrappers

    def unreadable(*_args, **_kwargs):
        raise RuntimeError("cannot read the inputs")

    monkeypatch.setattr(_wrappers, "_hidden_inputs", unreadable)
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        error, span = _failed(
            traced, sync_client(fake).search, search_queries=[FAIL_ECHO + " unread-secret"]
        )

    assert "unread-secret" in str(error)
    expected = ["BadRequestError: " + REDACTED_VALUE, REDACTED_VALUE, REDACTED_VALUE]
    assert _error_texts(span) == expected
    assert "secret" not in traced.wire()


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
