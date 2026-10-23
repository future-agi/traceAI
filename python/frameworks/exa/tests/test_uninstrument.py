"""uninstrument() restores every wrapped vendor method by identity."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import EXA_KEY, FakeExa, instrumented  # noqa: E402
from exa_py import api as exa_api  # noqa: E402

METHODS = (
    "search",
    "get_contents",
    "answer",
    "search_and_contents",
    "stream_search",
    "stream_answer",
)
CLIENTS = ("Exa", "AsyncExa")


def _class_methods():
    return {
        (client, method): getattr(exa_api, client).__dict__[method]
        for client in CLIENTS
        for method in METHODS
    }


def test_every_method_is_defined_on_its_own_class():
    # AsyncExa defines its own twins; nothing is reached only by inheritance,
    # so wrapping both classes cannot double-wrap one function.
    assert len(_class_methods()) == 12


def test_instrument_wraps_and_uninstrument_restores_all_twelve_methods():
    originals = _class_methods()

    with instrumented():
        wrapped = _class_methods()
        assert all(wrapped[key] is not originals[key] for key in originals)
        assert all(hasattr(wrapped[key], "__wrapped__") for key in originals)

    restored = _class_methods()
    assert all(restored[key] is originals[key] for key in originals), [
        key for key in originals if restored[key] is not originals[key]
    ]


def test_no_spans_after_uninstrument():
    with FakeExa() as fake:
        with instrumented() as traced:
            pass

        client = exa_api.Exa(api_key=EXA_KEY, base_url=fake.origin)
        client.search("q")
        client.answer("q")
        list(client.stream_search("q"))

        async def call() -> None:
            async_client = exa_api.AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
            try:
                await async_client.search("q")
                async for _ in await async_client.stream_answer("q"):
                    pass
            finally:
                await async_client.client.aclose()

        asyncio.run(call())

    assert len(fake.calls) == 5
    assert traced.spans() == []
