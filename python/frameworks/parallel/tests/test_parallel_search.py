"""parallel.search spans from the real parallel-web client against the loopback fake."""

from __future__ import annotations

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

from opentelemetry.trace import StatusCode  # noqa: E402

from _parallel_support import (  # noqa: E402
    CONTENT_MARKERS,
    EXCERPT,
    MALFORMED,
    PARALLEL_KEY,
    SEARCH_ID,
    SEARCH_RESULTS,
    SESSION_ID,
    USAGE,
    USAGE_ITEMS,
    WARN,
    WARNING_MESSAGE,
    WARNING_TYPES,
    FakeParallel,
    attrs,
    instrumented,
    sync_client,
)


@pytest.fixture()
def fake():
    with FakeParallel() as server:
        yield server


def test_search_emits_one_retriever_span_with_the_documented_attributes(fake):
    with instrumented() as traced:
        result = sync_client(fake).search(
            search_queries=["open telemetry", "retrieval tracing"], mode="turbo"
        )

    # The real SDK sent the request and parsed the fake's content.
    assert fake.paths() == ["/v1/search"]
    assert result.results[0].excerpts == [EXCERPT]

    span = traced.one()
    assert span.name == "parallel.search"
    assert span.parent is None
    assert span.status.status_code is StatusCode.OK
    assert attrs(span) == {
        "fi.span.kind": "RETRIEVER",
        "parallel.mode": "turbo",
        "parallel.query_count": 2,
        "gen_ai.retrieval.query": "open telemetry\nretrieval tracing",
        "input.value": "open telemetry\nretrieval tracing",
        "parallel.result_count": SEARCH_RESULTS,
        "parallel.search_id": SEARCH_ID,
        "parallel.session_id": SESSION_ID,
    }
    assert span.events == ()


def test_search_without_mode_records_no_mode(fake):
    with instrumented() as traced:
        sync_client(fake).search(search_queries=["no mode given"])

    values = attrs(traced.one())
    # The server default is not invented on the span.
    assert "parallel.mode" not in values
    assert values["parallel.query_count"] == 1


def test_search_records_no_response_content(fake):
    with instrumented() as traced:
        sync_client(fake).search(search_queries=["content check"], objective="RESEARCH-GOAL")

    wire = traced.wire()
    for marker in CONTENT_MARKERS + ("RESEARCH-GOAL",):
        assert marker not in wire, marker
    assert not any(key.startswith(("gen_ai.usage", "llm.", "gen_ai.request.model")) for key in attrs(traced.one()))


def test_request_session_id_is_recorded_and_echoed(fake):
    with instrumented() as traced:
        sync_client(fake).search(search_queries=["session"], session_id="session_from_caller")

    assert attrs(traced.one())["parallel.session_id"] == "session_from_caller"


def test_api_key_embedded_in_a_query_is_redacted_on_the_span_only(fake):
    query = "find {0} please".format(PARALLEL_KEY)
    with instrumented() as traced:
        sync_client(fake).search(search_queries=[query, "second"])

    values = attrs(traced.one())
    assert values["gen_ai.retrieval.query"] == "find [redacted] please\nsecond"
    assert values["input.value"] == values["gen_ai.retrieval.query"]
    assert PARALLEL_KEY not in traced.wire()
    # Redaction is on the span only; the vendor still got the caller's query.
    assert fake.calls[0][2]["search_queries"][0] == query
    assert fake.calls[0][1]["x-api-key"] == PARALLEL_KEY


def test_keys_from_default_headers_and_extra_headers_are_redacted(fake):
    header_key = "placeholder-header-key-must-not-be-exported"
    call_key = "placeholder-call-key-must-not-be-exported"
    client = sync_client(fake, default_headers={"X-Api-Key": header_key})
    with instrumented() as traced:
        client.search(
            search_queries=[header_key, call_key, PARALLEL_KEY],
            extra_headers={"x-api-key": call_key},
        )

    assert attrs(traced.one())["input.value"] == "[redacted]\n[redacted]\n[redacted]"
    wire = traced.wire()
    for key in (header_key, call_key, PARALLEL_KEY):
        assert key not in wire


def test_key_read_from_the_environment_is_redacted(fake, monkeypatch):
    from parallel import Parallel

    env_key = "placeholder-env-key-must-not-be-exported"
    monkeypatch.setenv("PARALLEL_API_KEY", env_key)
    with instrumented() as traced:
        Parallel(base_url=fake.origin, max_retries=0).search(search_queries=["k " + env_key])

    assert attrs(traced.one())["input.value"] == "k [redacted]"
    assert env_key not in traced.wire()


@pytest.mark.parametrize(
    "queries",
    [
        ["a" * 2000],
        ["\u20ac" * 500],  # 3 UTF-8 bytes each: 1024 is not a character boundary
        ["\U0001f50d" * 300],  # 4 UTF-8 bytes each
        ["x" + "\u00e9" * 600],  # 2-byte characters after an odd offset
        ["q" * 600, "r" * 600],  # the cap applies to the joined value
    ],
    ids=["ascii", "3-byte", "4-byte", "2-byte-offset", "two-queries"],
)
def test_query_is_capped_at_1kb_of_utf8_on_a_character_boundary(fake, queries):
    joined = "\n".join(queries)
    with instrumented() as traced:
        sync_client(fake).search(search_queries=queries)

    values = attrs(traced.one())
    recorded = values["gen_ai.retrieval.query"]
    assert values["input.value"] == recorded
    assert len(recorded.encode("utf-8")) <= 1024
    # The longest whole-character prefix that fits: one more character would not.
    assert recorded == joined[: len(recorded)]
    assert len(joined[: len(recorded) + 1].encode("utf-8")) > 1024
    # The count is of the caller's queries, not of what fit under the cap.
    assert values["parallel.query_count"] == len(queries)


def test_key_is_redacted_before_the_cap_so_no_partial_key_survives(fake):
    # The key straddles the 1 KB boundary: capping first would leave a prefix.
    query = "a" * 1000 + PARALLEL_KEY
    with instrumented() as traced:
        sync_client(fake).search(search_queries=[query])

    recorded = attrs(traced.one())["input.value"]
    assert recorded == "a" * 1000 + "[redacted]"
    assert PARALLEL_KEY[:12] not in traced.wire()


def test_queries_given_as_a_tuple_are_recorded(fake):
    with instrumented() as traced:
        sync_client(fake).search(search_queries=("one", "two", "three"))

    values = attrs(traced.one())
    assert values["parallel.query_count"] == 3
    assert values["input.value"] == "one\ntwo\nthree"


def test_usage_items_are_recorded_only_when_the_response_has_them(fake):
    with instrumented() as traced:
        client = sync_client(fake)
        client.search(search_queries=[USAGE])
        client.search(search_queries=["plain search"])

    with_usage, without_usage = (attrs(span) for span in traced.spans())
    assert list(with_usage["parallel.usage.names"]) == [name for name, _ in USAGE_ITEMS]
    assert list(with_usage["parallel.usage.counts"]) == [count for _, count in USAGE_ITEMS]
    assert "parallel.usage.names" not in without_usage
    assert "parallel.usage.counts" not in without_usage
    # Usage is a SKU count, not tokens or cost: nothing a collector would price.
    for values in (with_usage, without_usage):
        assert not any("token" in key or "cost" in key for key in values)


def test_warnings_become_span_events_and_do_not_fail_the_span(fake):
    with instrumented() as traced:
        sync_client(fake).search(search_queries=[WARN])

    span = traced.one()
    assert span.status.status_code is StatusCode.OK
    assert attrs(span)["parallel.warning_count"] == 2
    assert [event.name for event in span.events] == ["parallel.warning", "parallel.warning"]
    assert [dict(event.attributes) for event in span.events] == [
        {"parallel.warning.type": WARNING_TYPES[0], "parallel.warning.message": WARNING_MESSAGE},
        {"parallel.warning.type": WARNING_TYPES[1], "parallel.warning.message": WARNING_MESSAGE},
    ]
    # Warning detail can carry content; it is never recorded.
    assert EXCERPT not in traced.wire()


def test_unknown_counts_and_ids_are_omitted_not_zero(fake):
    with instrumented() as traced:
        sync_client(fake).search(search_queries=[MALFORMED])

    values = attrs(traced.one())
    for key in (
        "parallel.result_count",
        "parallel.search_id",
        "parallel.session_id",
        "parallel.warning_count",
        "parallel.usage.names",
    ):
        assert key not in values, key
    assert traced.one().status.status_code is StatusCode.OK
