"""Errors and cancellation: ERROR status, exception re-raised unchanged."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("tavily", reason="tavily-python must be installed to test its instrumentor")

from opentelemetry.trace import StatusCode  # noqa: E402

from _support import attrs, instrumented  # noqa: E402
from _tavily_fake import (  # noqa: E402
    FAIL_401,
    FAIL_500,
    KEYLESS_LIMIT,
    SLOW,
    TAVILY_KEY,
    FakeTavily,
)


@pytest.fixture()
def fake():
    with FakeTavily() as server:
        yield server


def _sync(fake: FakeTavily, api_key: Any = TAVILY_KEY) -> Any:
    from tavily import TavilyClient

    return TavilyClient(api_key=api_key, api_base_url=fake.origin)


async def _async(fake: FakeTavily, method: str, *args: Any, api_key: Any = TAVILY_KEY) -> Any:
    from tavily import AsyncTavilyClient

    client = AsyncTavilyClient(api_key=api_key, api_base_url=fake.origin)
    try:
        return await getattr(client, method)(*args)
    finally:
        await client.close()


def _exception_events(span: Any) -> list:
    return [event for event in span.events if event.name == "exception"]


def _assert_error_span(span: Any, error: BaseException) -> None:
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "{0}: {1}".format(type(error).__name__, error)
    (event,) = _exception_events(span)
    assert event.attributes["exception.type"].endswith(type(error).__name__)
    values = attrs(span)
    assert "tavily.result_count" not in values
    assert "tavily.cancelled" not in values


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_invalid_key_sets_error_and_reraises(fake, mode):
    from tavily import InvalidAPIKeyError

    with instrumented() as traced:
        with pytest.raises(InvalidAPIKeyError) as info:
            if mode == "sync":
                _sync(fake).search(FAIL_401)
            else:
                asyncio.run(_async(fake, "search", FAIL_401))

    assert str(info.value) == "Unauthorized: invalid API key."
    span = traced.one()
    assert span.name == "tavily.search"
    assert attrs(span)["input.value"] == FAIL_401
    _assert_error_span(span, info.value)


def test_sync_http_error_sets_error_and_reraises(fake):
    import requests

    with instrumented() as traced:
        with pytest.raises(requests.HTTPError) as info:
            _sync(fake).search(FAIL_500)

    _assert_error_span(traced.one(), info.value)


def test_async_http_error_sets_error_and_reraises(fake):
    import httpx

    with instrumented() as traced:
        with pytest.raises(httpx.HTTPStatusError) as info:
            asyncio.run(_async(fake, "search", FAIL_500))

    _assert_error_span(traced.one(), info.value)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_keyless_limit_sets_error_reraises_and_does_not_retry(fake, monkeypatch, mode):
    """PRD J4: TavilyKeylessLimitError is ERROR and re-raised; no retry loop."""
    from tavily import TavilyKeylessLimitError

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with instrumented() as traced:
        with pytest.raises(TavilyKeylessLimitError) as info:
            if mode == "sync":
                _sync(fake, api_key=None).search(KEYLESS_LIMIT)
            else:
                asyncio.run(_async(fake, "search", KEYLESS_LIMIT, api_key=None))

    assert info.value.code == "keyless_rate_limited"
    assert fake.paths() == ["/search"]
    assert fake.calls[0][1].get("x-tavily-access-mode") == "keyless"
    _assert_error_span(traced.one(), info.value)


@pytest.mark.parametrize(
    "method, argument",
    [("search", SLOW), ("extract", ["https://example.com/" + SLOW])],
)
def test_cancelled_async_call_is_error_cancelled(fake, method, argument):
    async def scenario() -> None:
        task = asyncio.ensure_future(_async(fake, method, argument))
        while not fake.calls:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with instrumented() as traced:
        asyncio.run(scenario())

    span = traced.one()
    assert span.name == "tavily.{0}".format(method)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    values = attrs(span)
    assert values["tavily.cancelled"] is True
    assert "tavily.result_count" not in values
    # Cancellation is not a failure of the call: no exception event.
    assert _exception_events(span) == []


def test_successful_calls_have_no_cancelled_attribute(fake):
    with instrumented() as traced:
        _sync(fake).search("fine")
        asyncio.run(_async(fake, "extract", ["https://example.com/a"]))

    for span in traced.spans():
        assert "tavily.cancelled" not in attrs(span)
        assert span.status.status_code is StatusCode.OK
