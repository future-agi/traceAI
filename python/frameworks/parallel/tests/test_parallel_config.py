"""instrument() options: TraceConfig hide flags and the capture switches."""

from __future__ import annotations

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

from fi_instrumentation import TraceConfig  # noqa: E402

from _parallel_support import (  # noqa: E402
    PARALLEL_KEY,
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

INPUT_KEYS = ("gen_ai.retrieval.query", "input.value", "parallel.urls", "parallel.objective")


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
