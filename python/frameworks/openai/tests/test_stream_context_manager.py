"""The SDK context manager must never unwrap the traced iterator."""
import asyncio

import pytest
from opentelemetry.trace import StatusCode

from .test_openai_compatible_spans import (
    REQUEST_MODEL, async_client, chat_stream, client, make_tracer, only_span,
)


@pytest.mark.parametrize("mode", ["full", "early", "empty", "error"])
def test_sync_stream_context_finishes_once(make_tracer, mode):
    exporter = make_tracer()
    with client(lambda request: chat_stream()) as sdk:
        stream = sdk.chat.completions.create(
            model=REQUEST_MODEL, messages=[{"role": "user", "content": "hi"}], stream=True,
        )
        try:
            with stream as entered:
                assert entered is stream
                if mode != "empty":
                    for _ in entered:
                        if mode == "error":
                            raise ValueError("consumer stopped")
                        if mode == "early":
                            break
        except ValueError as error:
            assert str(error) == "consumer stopped"
        assert stream.response.is_closed
    span, attrs = only_span(exporter)
    assert attrs["gen_ai.request.model"] == REQUEST_MODEL
    assert attrs["gen_ai.provider.name"] == "baseten"
    assert span.status.status_code == (StatusCode.ERROR if mode == "error" else StatusCode.OK)
    if mode == "full":
        assert attrs["output.value"] == "Tokyo"
    if mode == "early":
        assert attrs["output.value"] == "Tok"
    if mode == "error":
        assert any(event.name == "exception" for event in span.events)


@pytest.mark.parametrize("mode", ["full", "early", "empty", "error"])
def test_async_stream_context_finishes_once(make_tracer, mode):
    exporter = make_tracer()

    async def run():
        async with async_client(lambda request: chat_stream()) as sdk:
            stream = await sdk.chat.completions.create(
                model=REQUEST_MODEL, messages=[{"role": "user", "content": "hi"}], stream=True,
            )
            try:
                async with stream as entered:
                    assert entered is stream
                    if mode != "empty":
                        async for _ in entered:
                            if mode == "error":
                                raise ValueError("consumer stopped")
                            if mode == "early":
                                break
            except ValueError as error:
                assert str(error) == "consumer stopped"
            assert stream.response.is_closed

    asyncio.run(run())
    span, attrs = only_span(exporter)
    assert attrs["gen_ai.request.model"] == REQUEST_MODEL
    assert attrs["gen_ai.provider.name"] == "baseten"
    assert span.status.status_code == (StatusCode.ERROR if mode == "error" else StatusCode.OK)
    if mode == "full":
        assert attrs["output.value"] == "Tokyo"
    if mode == "early":
        assert attrs["output.value"] == "Tok"
    if mode == "error":
        assert any(event.name == "exception" for event in span.events)
