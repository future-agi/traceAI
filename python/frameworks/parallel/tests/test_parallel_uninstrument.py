"""uninstrument() restores every wrapped method by identity and stops tracing."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to test its instrumentor")

from parallel import _client as parallel_client  # noqa: E402

from _parallel_support import (  # noqa: E402
    FakeParallel,
    async_client,
    instrumented,
    sync_client,
)

CLIENTS = ("Parallel", "AsyncParallel")
METHODS = ("search", "extract")


def _class_methods():
    return {
        (client, method): vars(getattr(parallel_client, client))[method]
        for client in CLIENTS
        for method in METHODS
    }


def test_every_wrapped_method_is_defined_on_its_own_class():
    # AsyncParallel defines its own twins; nothing is reached only through
    # inheritance, so wrapping both classes cannot double-wrap one function.
    assert len(_class_methods()) == 4


def test_instrument_wraps_and_uninstrument_restores_all_four_methods():
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
    with FakeParallel() as fake:
        with instrumented() as traced:
            pass

        client = sync_client(fake)
        client.search(search_queries=["q"])
        client.extract(urls=["https://x.example/a"])

        async def call() -> None:
            async_parallel = async_client(fake)
            try:
                await async_parallel.search(search_queries=["q"])
                await async_parallel.extract(urls=["https://x.example/a"])
            finally:
                await async_parallel.close()

        asyncio.run(call())

    assert len(fake.calls) == 4
    assert traced.spans() == []


def test_instrument_uninstrument_cycles_do_not_stack_wrappers():
    originals = _class_methods()
    with FakeParallel() as fake:
        for _ in range(3):
            with instrumented() as traced:
                sync_client(fake).search(search_queries=["q"])
            assert [span.name for span in traced.spans()] == ["parallel.search"]
            assert _class_methods() == originals
