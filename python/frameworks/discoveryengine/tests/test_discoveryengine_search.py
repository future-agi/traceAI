"""J1, J2, J4 and AC-03: SearchServiceClient.search and search_lite on the real gRPC transport."""

from __future__ import annotations

import pytest
from google.api_core import exceptions as core_exceptions
from google.api_core import retry as retries
from google.cloud.discoveryengine_v1 import SearchRequest
from opentelemetry.trace import StatusCode

from _discoveryengine_support import (
    CONTENT_MARKERS,
    PAGES,
    SEARCH_RESULTS,
    SECOND_PAGE_RESULTS,
    SERVING_CONFIG,
    UNAVAILABLE_ONCE,
    FakeDiscoveryEngine,
    attrs,
    instrumented,
    search_client,
    search_request,
)

QUERY = "QUERY-TEXT-OFF-BY-DEFAULT"


@pytest.fixture()
def fake():
    with FakeDiscoveryEngine() as server:
        yield server


@pytest.mark.parametrize("method", ["search", "search_lite"])
def test_one_retriever_span_with_serving_config_and_result_count(fake, method):
    with instrumented() as traced:
        pager = getattr(search_client(fake), method)(request=search_request(QUERY))

    assert len(pager.results) == SEARCH_RESULTS
    span = traced.one()
    assert span.name == "discoveryengine.{0}".format(method)
    values = attrs(span)
    assert values == {
        "fi.span.kind": "RETRIEVER",
        "discoveryengine.serving_config": SERVING_CONFIG,
        "discoveryengine.result_count": SEARCH_RESULTS,
    }
    assert span.status.status_code is StatusCode.OK
    assert span.events == ()
    # Query text is content: off by default, so it is nowhere on the span.
    wire = traced.wire()
    assert QUERY not in wire
    for marker in CONTENT_MARKERS:
        assert marker not in wire, marker


def test_search_lite_and_search_are_distinguished_by_span_name_only(fake):
    with instrumented() as traced:
        client = search_client(fake)
        client.search(request=search_request())
        client.search_lite(request=search_request())

    search, lite = traced.spans()
    assert (search.name, lite.name) == ("discoveryengine.search", "discoveryengine.search_lite")
    assert attrs(search) == attrs(lite)
    assert fake.methods() == ["Search", "SearchLite"]


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda client, req: client.search(req), id="positional-dict"),
        pytest.param(lambda client, req: client.search(request=SearchRequest(req)), id="keyword-message"),
        pytest.param(lambda client, req: client.search(SearchRequest(req)), id="positional-message"),
        pytest.param(
            lambda client, req: type(client).search(client, request=req), id="called-through-the-class"
        ),
    ],
)
def test_the_request_is_read_however_it_is_passed(fake, call):
    with instrumented(capture_query=True) as traced:
        call(search_client(fake), search_request("how is it passed"))

    values = attrs(traced.one())
    assert values["discoveryengine.serving_config"] == SERVING_CONFIG
    assert values["input.value"] == "how is it passed"


def test_a_request_without_serving_config_or_query_records_neither(fake):
    with instrumented(capture_query=True) as traced:
        search_client(fake).search()

    values = attrs(traced.one())
    assert values == {"fi.span.kind": "RETRIEVER", "discoveryengine.result_count": SEARCH_RESULTS}


def test_iterating_a_paged_result_keeps_one_span_per_search_call(fake):
    # J4: the pager fetches the next page with the same RPC, not through
    # search(), so the second RPC is not a second span. The count is the
    # first page's, which is all the call itself returned.
    with instrumented() as traced:
        pager = search_client(fake).search(request=search_request(PAGES))
        results = list(pager)

    assert len(results) == SEARCH_RESULTS + SECOND_PAGE_RESULTS
    assert fake.methods() == ["Search", "Search"]
    span = traced.one()
    assert span.name == "discoveryengine.search"
    assert attrs(span)["discoveryengine.result_count"] == SEARCH_RESULTS


def test_client_retries_stay_inside_one_span(fake):
    retry = retries.Retry(
        predicate=retries.if_exception_type(core_exceptions.ServiceUnavailable),
        initial=0.01,
        maximum=0.02,
        timeout=10,
    )
    with instrumented() as traced:
        pager = search_client(fake).search(request=search_request(UNAVAILABLE_ONCE), retry=retry)

    assert len(pager.results) == SEARCH_RESULTS
    assert fake.methods() == ["Search", "Search"]
    span = traced.one()
    assert span.status.status_code is StatusCode.OK
    assert span.events == ()


def test_the_serving_config_is_capped_at_1_kb(fake):
    from traceai_discoveryengine import _wrappers

    long_config = SERVING_CONFIG + "/" + "x" * 2000
    with instrumented() as traced:
        search_client(fake).search(request=search_request(serving_config=long_config))

    assert fake.calls[0].request.serving_config == long_config
    value = attrs(traced.one())["discoveryengine.serving_config"]
    assert value == long_config[: _wrappers.MAX_VALUE_BYTES]


def test_no_model_token_or_cost_attributes(fake):
    with instrumented(capture_query=True) as traced:
        search_client(fake).search(request=search_request())

    for key in attrs(traced.one()):
        assert not any(word in key for word in ("model", "token", "cost", "usage")), key
