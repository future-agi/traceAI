"""uninstrument() restores every wrapped method by identity and stops tracing."""

from __future__ import annotations

import asyncio

from google.cloud.discoveryengine_v1 import (
    ConversationalSearchServiceAsyncClient,
    ConversationalSearchServiceClient,
    SearchServiceAsyncClient,
    SearchServiceClient,
)

from _discoveryengine_support import (
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    async_answer_client,
    async_search_client,
    instrumented,
    search_client,
    search_request,
)

METHODS = (
    (SearchServiceClient, "search"),
    (SearchServiceClient, "search_lite"),
    (SearchServiceAsyncClient, "search"),
    (SearchServiceAsyncClient, "search_lite"),
    (ConversationalSearchServiceClient, "answer_query"),
    (ConversationalSearchServiceAsyncClient, "answer_query"),
)


def _class_methods():
    return {(cls.__name__, name): vars(cls)[name] for cls, name in METHODS}


def _every_call(fake):
    search_client(fake).search(request=search_request())
    search_client(fake).search_lite(request=search_request())
    answer_client(fake).answer_query(request=answer_request())

    async def call():
        search = async_search_client(fake)
        answer = async_answer_client(fake)
        try:
            await search.search(request=search_request())
            await search.search_lite(request=search_request())
            await answer.answer_query(request=answer_request())
        finally:
            await search.transport.close()
            await answer.transport.close()

    asyncio.run(call())


def test_every_wrapped_method_is_defined_on_its_own_class():
    # The async clients define their own methods (they call the transport,
    # not the sync client), so wrapping both classes cannot double-wrap one
    # function.
    methods = _class_methods()
    assert len(methods) == 6
    assert len({id(method) for method in methods.values()}) == 6


def test_instrument_wraps_and_uninstrument_restores_all_six_methods():
    originals = _class_methods()

    with instrumented():
        wrapped = _class_methods()
        assert all(wrapped[key] is not originals[key] for key in originals)
        assert all(wrapped[key].__wrapped__ is originals[key] for key in originals)

    restored = _class_methods()
    assert all(restored[key] is originals[key] for key in originals), [
        key for key in originals if restored[key] is not originals[key]
    ]


def test_no_spans_after_uninstrument():
    with FakeDiscoveryEngine() as fake:
        with instrumented() as traced:
            pass
        _every_call(fake)

    assert len(fake.calls) == 6
    assert traced.spans() == []


def test_instrument_uninstrument_cycles_do_not_stack_wrappers():
    originals = _class_methods()
    with FakeDiscoveryEngine() as fake:
        for _ in range(3):
            with instrumented() as traced:
                _every_call(fake)
            assert len(traced.spans()) == 6
            assert _class_methods() == originals


def test_a_client_made_before_instrument_is_traced():
    # The methods are wrapped on the class, so construction order does not matter.
    with FakeDiscoveryEngine() as fake:
        client = search_client(fake)
        with instrumented() as traced:
            client.search(request=search_request())

    assert traced.names() == ["discoveryengine.search"]


def test_a_method_bound_while_instrumented_stops_tracing_after_uninstrument():
    with FakeDiscoveryEngine() as fake:
        client = search_client(fake)
        with instrumented() as traced:
            bound = client.search
            bound(request=search_request())
        bound(request=search_request())

    assert len(fake.calls) == 2
    assert traced.names() == ["discoveryengine.search"]
