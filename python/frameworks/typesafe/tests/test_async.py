"""AsyncTypeSafeClient.system_one has the same span as the sync client."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("typesafe_sdk")

from opentelemetry.trace import StatusCode  # noqa: E402

from tests._support import (  # noqa: E402
    NOUL,
    STATE,
    FakeSystemOne,
    async_client,
    instrumented,
    no_retry,
    only_span,
    plain,
    questions,
)


def test_async_decision_span():
    async def main():
        fake = FakeSystemOne()
        async with async_client(fake) as typesafe:
            return await typesafe.system_one(STATE, questions())

    with instrumented() as exporter:
        response = asyncio.run(main())
    span = only_span(exporter)
    assert response.nouls["billing"].noul == NOUL
    assert span.attributes["gen_ai.span.kind"] == "EVALUATOR"
    assert span.attributes["typesafe.question.billing.noul"] == NOUL
    assert span.attributes["typesafe.question.tone.choice"] == "angry"
    assert span.status.status_code is StatusCode.OK


def test_async_error_span():
    from typesafe_sdk import TypeSafeRateLimitError

    async def main():
        async with async_client(FakeSystemOne(), retry=no_retry()) as typesafe:
            await typesafe.system_one(STATE, questions(), model="fail-429")

    with instrumented() as exporter:
        with pytest.raises(TypeSafeRateLimitError):
            asyncio.run(main())
    span = only_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["http.response.status_code"] == 429


def test_async_cancellation_is_marked_not_recorded_as_exception():
    import httpx2
    from typesafe_sdk import AsyncTypeSafeClient

    async def hang(request):
        await asyncio.sleep(30)

    async def main():
        async with AsyncTypeSafeClient(transport=httpx2.MockTransport(hang), **plain(AsyncTypeSafeClient, api_key="k")) as typesafe:
            task = asyncio.ensure_future(typesafe.system_one(STATE, questions()))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    with instrumented() as exporter:
        asyncio.run(main())
    span = only_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert span.attributes["typesafe.cancelled"] is True
    assert span.events == ()
