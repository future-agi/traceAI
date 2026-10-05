"""Scope: only Search and Extract are traced (PRD NG1, AC-07)."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

from opentelemetry.trace import StatusCode  # noqa: E402

from _parallel_support import (  # noqa: E402
    FakeParallel,
    async_client,
    attrs,
    instrumented,
    sync_client,
)


@pytest.fixture()
def fake():
    with FakeParallel() as server:
        yield server


def test_task_run_is_not_traced(fake):
    with instrumented() as traced:
        run = sync_client(fake).task_run.create(input="What is OTLP?", processor="base")

    assert run.run_id == "run_1"
    assert fake.paths() == ["/v1/tasks/runs"]
    assert traced.spans() == []


def test_the_client_has_no_beta_search_or_extract_and_v1beta_is_not_traced(fake):
    client = sync_client(fake)
    # parallel-web 1.x has no beta search/extract method (0.x had one); a raw
    # request to the beta path is not a wrapped call and gets no span.
    assert not hasattr(client.beta, "search")
    assert not hasattr(client.beta, "extract")
    with instrumented() as traced:
        with pytest.raises(Exception):
            client.post("/v1beta/search", body={"search_queries": ["q"]}, cast_to=object)

    assert fake.paths() == ["/v1beta/search"]
    assert traced.spans() == []


def test_search_and_extract_call_the_v1_paths(fake):
    with instrumented() as traced:
        client = sync_client(fake)
        client.search(search_queries=["q"])
        client.extract(urls=["https://x.example/a"])

    assert fake.paths() == ["/v1/search", "/v1/extract"]
    assert [span.name for span in traced.spans()] == ["parallel.search", "parallel.extract"]


def test_copies_of_the_client_are_traced(fake):
    with instrumented() as traced:
        sync_client(fake).with_options(timeout=5).search(search_queries=["q"])
        sync_client(fake).copy().extract(urls=["https://x.example/a"])

    assert [span.name for span in traced.spans()] == ["parallel.search", "parallel.extract"]


def test_with_raw_response_is_traced_without_response_attributes(fake):
    with instrumented() as traced:
        raw = sync_client(fake).with_raw_response.search(search_queries=["q"])
        assert len(raw.parse().results) == 2

    span = traced.one()
    assert span.name == "parallel.search"
    assert span.status.status_code is StatusCode.OK
    values = attrs(span)
    assert values["parallel.query_count"] == 1
    # The raw response is not parsed by the wrapper: counts and ids are unknown.
    for key in ("parallel.result_count", "parallel.search_id", "parallel.session_id"):
        assert key not in values


def test_with_streaming_response_is_traced_until_the_response_is_returned(fake):
    with instrumented() as traced:
        with sync_client(fake).with_streaming_response.extract(
            urls=["https://x.example/a"]
        ) as response:
            assert response.http_response.status_code == 200
            assert len(response.parse().results) == 1

    span = traced.one()
    assert span.name == "parallel.extract"
    assert span.status.status_code is StatusCode.OK
    assert "parallel.result_count" not in attrs(span)


def test_async_raw_and_streaming_responses_are_traced(fake):
    async def call() -> None:
        client = async_client(fake)
        try:
            raw = await client.with_raw_response.search(search_queries=["q"])
            assert len((await raw.parse()).results) == 2
            async with client.with_streaming_response.extract(
                urls=["https://x.example/a"]
            ) as response:
                assert response.http_response.status_code == 200
        finally:
            await client.close()

    with instrumented() as traced:
        asyncio.run(call())

    assert [span.name for span in traced.spans()] == ["parallel.search", "parallel.extract"]


def test_a_raw_response_wrapper_made_before_instrument_stays_untraced(fake):
    # parallel-web binds client.search when with_raw_response is first read
    # (a cached property), so that wrapper keeps the method it saw. The README
    # documents this.
    client = sync_client(fake)
    raw_before = client.with_raw_response
    with instrumented() as traced:
        raw_before.search(search_queries=["q"])
        client.search(search_queries=["q"])

    assert [span.name for span in traced.spans()] == ["parallel.search"]


def test_a_raw_response_wrapper_made_while_instrumented_stops_tracing_after_uninstrument(fake):
    client = sync_client(fake)
    with instrumented() as traced:
        raw_during = client.with_raw_response
        raw_during.search(search_queries=["q"])
    raw_during.search(search_queries=["q"])

    assert len(fake.calls) == 2
    assert [span.name for span in traced.spans()] == ["parallel.search"]
