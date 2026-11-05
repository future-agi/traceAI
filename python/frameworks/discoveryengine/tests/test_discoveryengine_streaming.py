"""Stream lifecycle, identity and context checks; no cloud endpoints or ADC."""

import asyncio
import gc
from unittest.mock import AsyncMock, Mock

import pytest
from _discoveryengine_support import (
    FakeDiscoveryEngine,
    answer_request,
    async_answer_client,
    attrs,
    instrumented,
)
from fi_instrumentation import suppress_tracing
from google.api_core.exceptions import PermissionDenied
from google.auth.credentials import AnonymousCredentials
from google.cloud import discoveryengine_v1 as de
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from traceai_discoveryengine import DiscoveryEngineInstrumentor


def chunk(text="", state=de.Answer.State.STREAMING):
    return de.AnswerQueryResponse(answer={"answer_text": text, "state": state})


def mock_client(stream, is_async=False):
    cls = (
        de.ConversationalSearchServiceAsyncClient
        if is_async
        else de.ConversationalSearchServiceClient
    )
    client = cls(credentials=AnonymousCredentials())
    # The async GAPIC method returns the wrapped transport's awaitable.
    rpc = AsyncMock(return_value=stream) if is_async else Mock(return_value=stream)
    client.transport._wrapped_methods[client.transport.stream_answer_query] = rpc
    return client, rpc


def test_chunks_are_lazy_identity_preserved_and_context_does_not_leak():
    pieces = [chunk("part"), chunk("ial"), chunk("partial", de.Answer.State.SUCCEEDED)]
    reads = []
    with instrumented(capture_content=True) as traced:
        tracer = traced.provider.get_tracer("app")

        def generate():
            for piece in pieces:
                reads.append(trace.get_current_span().get_span_context().span_id)
                yield piece

        client, rpc = mock_client(generate())
        try:
            with tracer.start_as_current_span("parent") as parent:
                stream = client.stream_answer_query(request=answer_request())
                assert not reads
                assert rpc.call_count == 1
                assert traced.spans() == []
                results = []
                for piece in stream:
                    assert trace.get_current_span() is parent
                    results.append(piece)
            span, parent_span = traced.spans()
            assert all(a is b for a, b in zip(results, pieces))
            assert span.parent.span_id == parent_span.context.span_id
            assert reads == [span.context.span_id] * 3
            assert attrs(span)["discoveryengine.answer.length"] == 7
            assert attrs(span)["output.value"].count("partial") == 1
        finally:
            client.transport.close()


@pytest.mark.parametrize("ending", ["close", "drop", "error", "incomplete", "failed"])
def test_sync_stream_abandonment_and_failures_end_once(ending):
    error = PermissionDenied("hidden query echoed")

    def generate():
        yield chunk("partial")
        if ending == "error":
            raise error
        if ending == "failed":
            yield chunk(state=de.Answer.State.FAILED)

    with instrumented() as traced:
        client, _ = mock_client(generate())
        try:
            stream = client.stream_answer_query(request=answer_request())
            next(stream)
            if ending == "close":
                stream.close()
                stream.close()
            elif ending == "drop":
                del stream
                gc.collect()
            elif ending == "error":
                with pytest.raises(PermissionDenied) as raised:
                    list(stream)
                assert raised.value is error
                stream.close()
            else:
                list(stream)
            span = traced.one()
            assert span.status.status_code is StatusCode.ERROR
            if ending in ("close", "drop"):
                assert attrs(span)["discoveryengine.cancelled"] is True
                assert not span.events
            elif ending == "error":
                assert len(span.events) == 1
            elif ending == "failed":
                assert attrs(span)["discoveryengine.answer.state"] == "FAILED"
            else:
                assert span.status.description == "incomplete answer stream"
        finally:
            client.transport.close()


def test_cancel_forwards_result_and_finishes_even_before_first_read():
    vendor = Mock()
    vendor.cancel.return_value = True
    with instrumented() as traced:
        client, _ = mock_client(vendor)
        try:
            stream = client.stream_answer_query(request=answer_request())
            assert stream.cancel() is True
            assert stream.cancel() is True
        finally:
            client.transport.close()
    assert vendor.cancel.call_count == 2
    assert attrs(traced.one())["discoveryengine.cancelled"] is True


@pytest.mark.parametrize("ending", ["ok", "error", "cancel", "close", "incomplete", "failed"])
def test_async_stream_lifecycle(ending):
    error = PermissionDenied("query marker")

    async def run():
        async def generate():
            yield chunk("partial")
            if ending == "error":
                raise error
            if ending == "cancel":
                raise asyncio.CancelledError()
            if ending == "ok":
                yield chunk("partial", de.Answer.State.SUCCEEDED)
            if ending == "failed":
                yield chunk(state=de.Answer.State.FAILED)

        client, rpc = mock_client(generate(), True)
        with instrumented() as traced:
            try:
                stream = await client.stream_answer_query(request=answer_request())
                assert rpc.call_count == 1
                assert traced.spans() == []
                first = await anext(stream)
                assert first.answer.answer_text == "partial"
                assert not trace.get_current_span().get_span_context().is_valid
                if ending == "close":
                    await stream.aclose()
                    await stream.aclose()
                elif ending in ("error", "cancel"):
                    with pytest.raises(
                        PermissionDenied if ending == "error" else asyncio.CancelledError
                    ) as raised:
                        await anext(stream)
                    if ending == "error":
                        assert raised.value is error
                else:
                    async for _ in stream:
                        pass
                span = traced.one()
                assert span.status.status_code is (
                    StatusCode.OK if ending == "ok" else StatusCode.ERROR
                )
                if ending in ("close", "cancel"):
                    assert attrs(span)["discoveryengine.cancelled"] is True
                    assert not span.events
            finally:
                await client.transport.close()

    asyncio.run(run())


def test_async_real_grpc_stream():
    async def run(fake):
        client = async_answer_client(fake)
        try:
            stream = await client.stream_answer_query(request=answer_request())
            return [response async for response in stream]
        finally:
            await client.transport.close()

    with FakeDiscoveryEngine() as fake, instrumented() as traced:
        chunks = asyncio.run(run(fake))
    assert len(chunks) == 2
    assert traced.one().status.status_code is StatusCode.OK
    assert attrs(traced.one())["discoveryengine.stream.chunk_count"] == 2


@pytest.mark.parametrize("mode", ["suppressed", "uninstrumented_bound"])
def test_stream_suppression_and_uninstrumentation(mode):
    with instrumented() as traced:
        vendor = iter([chunk("done", de.Answer.State.SUCCEEDED)])
        client, _ = mock_client(vendor)
        try:
            bound = client.stream_answer_query
            if mode == "suppressed":
                with suppress_tracing():
                    assert bound(request=answer_request()) is vendor
            else:
                DiscoveryEngineInstrumentor().uninstrument()
                assert bound(request=answer_request()) is vendor
            assert len(list(vendor)) == 1
        finally:
            client.transport.close()
    assert traced.spans() == []
