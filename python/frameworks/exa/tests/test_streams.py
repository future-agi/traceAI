"""Stream lifecycle from the real SDK against the loopback fake's SSE routes."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("exa_py", reason="exa-py must be installed to test its instrumentor")

from _support import EXA_KEY, FakeExa, instrumented  # noqa: E402
from exa_py import AsyncExa, Exa  # noqa: E402
from exa_py.api import (  # noqa: E402
    AsyncStreamAnswerResponse,
    AsyncStreamSearchResponse,
    StreamAnswerResponse,
    StreamSearchResponse,
)


@pytest.fixture()
def fake():
    with FakeExa() as server:
        yield server


def test_sync_streams_keep_the_vendor_type(fake):
    with instrumented():
        client = Exa(api_key=EXA_KEY, base_url=fake.origin)
        search = client.stream_search("q")
        answer = client.stream_answer("q")
        assert isinstance(search, StreamSearchResponse)
        assert isinstance(answer, StreamAnswerResponse)
        list(search)
        list(answer)


def test_async_streams_keep_the_vendor_type(fake):
    async def call() -> None:
        client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
        try:
            search = await client.stream_search("q")
            answer = await client.stream_answer("q")
            assert isinstance(search, AsyncStreamSearchResponse)
            assert isinstance(answer, AsyncStreamAnswerResponse)
            async for _ in search:
                pass
            async for _ in answer:
                pass
        finally:
            await client.client.aclose()

    with instrumented():
        asyncio.run(call())
