"""parallel.extract spans from the real parallel-web client against the loopback fake."""

from __future__ import annotations

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

from opentelemetry.trace import StatusCode  # noqa: E402

from _parallel_support import (  # noqa: E402
    CONTENT_MARKERS,
    EXTRACT_ID,
    FULL_CONTENT,
    PARALLEL_KEY,
    SESSION_ID,
    FakeParallel,
    attrs,
    instrumented,
    sync_client,
)

SECRET_URL = "https://x.example/a?token=URL-SECRET"


@pytest.fixture()
def fake():
    with FakeParallel() as server:
        yield server


def test_extract_records_the_url_count_not_the_urls_or_page_text(fake):
    with instrumented() as traced:
        response = sync_client(fake).extract(
            urls=[SECRET_URL, "https://x.example/b", "https://x.example/missing"],
            objective="EXTRACT-OBJECTIVE",
        )

    assert fake.paths() == ["/v1/extract"]
    assert response.results[0].full_content == FULL_CONTENT

    span = traced.one()
    assert span.name == "parallel.extract"
    assert span.status.status_code is StatusCode.OK
    assert attrs(span) == {
        "fi.span.kind": "RETRIEVER",
        "parallel.url_count": 3,
        "parallel.result_count": 2,
        "parallel.failed_url_count": 1,
        "parallel.extract_id": EXTRACT_ID,
        "parallel.session_id": SESSION_ID,
    }
    wire = traced.wire()
    for marker in CONTENT_MARKERS + ("URL-SECRET", "x.example", "EXTRACT-OBJECTIVE"):
        assert marker not in wire, marker


def test_extract_with_search_queries_records_them_like_search(fake):
    query = "focus {0}".format(PARALLEL_KEY)
    with instrumented() as traced:
        sync_client(fake).extract(urls=["https://x.example/a"], search_queries=[query, "pricing"])

    values = attrs(traced.one())
    assert values["parallel.query_count"] == 2
    assert values["gen_ai.retrieval.query"] == "focus [redacted]\npricing"
    assert values["input.value"] == values["gen_ai.retrieval.query"]
    assert "parallel.mode" not in values
    assert PARALLEL_KEY not in traced.wire()


def test_extract_without_queries_sets_no_query_attributes(fake):
    with instrumented() as traced:
        sync_client(fake).extract(urls=["https://x.example/a"])

    values = attrs(traced.one())
    for key in ("parallel.query_count", "gen_ai.retrieval.query", "input.value"):
        assert key not in values, key


def test_capture_urls_records_at_most_20_redacted_capped_urls(fake):
    urls = ["https://x.example/{0}".format(i) for i in range(25)]
    urls[0] = "https://x.example/k?key={0}".format(PARALLEL_KEY)
    urls[1] = "https://x.example/" + "\u20ac" * 500
    with instrumented(capture_urls=True) as traced:
        sync_client(fake).extract(urls=urls)

    values = attrs(traced.one())
    assert values["parallel.url_count"] == 25
    captured = list(values["parallel.urls"])
    assert len(captured) == 20
    assert captured[0] == "https://x.example/k?key=[redacted]"
    assert all(len(url.encode("utf-8")) <= 1024 for url in captured)
    assert captured[1] == urls[1][: len(captured[1])]
    assert captured[2:] == urls[2:20]
    assert PARALLEL_KEY not in traced.wire()


def test_capture_urls_adds_nothing_to_search_spans(fake):
    with instrumented(capture_urls=True) as traced:
        sync_client(fake).search(search_queries=["plain search"])

    assert "parallel.urls" not in attrs(traced.one())


def test_capture_objective_records_the_redacted_capped_objective(fake):
    objective = "Find pricing for {0} ".format(PARALLEL_KEY) + "\u20ac" * 500
    with instrumented(capture_objective=True) as traced:
        client = sync_client(fake)
        client.search(search_queries=["q"], objective=objective)
        client.extract(urls=["https://x.example/a"], objective=objective)

    for span in traced.spans():
        recorded = attrs(span)["parallel.objective"]
        assert recorded.startswith("Find pricing for [redacted] \u20ac")
        assert len(recorded.encode("utf-8")) <= 1024
    assert PARALLEL_KEY not in traced.wire()


def test_urls_given_as_a_tuple_are_counted(fake):
    with instrumented() as traced:
        sync_client(fake).extract(urls=("https://x.example/a", "https://x.example/b"))

    assert attrs(traced.one())["parallel.url_count"] == 2


def test_a_non_sequence_urls_value_is_not_iterated_by_the_wrapper(fake):
    # parallel-web 1.x cannot send a generator (json raises TypeError); the
    # wrapper must not consume it first or count it, and the vendor's error
    # reaches the caller unchanged.
    consumed = []

    def urls():
        consumed.append(True)
        yield "https://x.example/a"

    with instrumented(capture_urls=True) as traced:
        generator = urls()
        with pytest.raises(TypeError, match="not JSON serializable"):
            sync_client(fake).extract(urls=generator)

    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    assert "parallel.url_count" not in attrs(span)
    assert "parallel.urls" not in attrs(span)
    assert fake.calls == []
    # Neither the wrapper nor json iterated it.
    assert consumed == []
