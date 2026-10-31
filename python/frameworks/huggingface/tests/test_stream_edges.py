"""Cancellation, close errors and concurrent consumers at the wrapper boundary."""

import asyncio
import json

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.trace import StatusCode

from .test_streaming import wrapped_stream


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("chunks", [[], ["hello", " world"]])
def test_plain_text_and_empty_streams(telemetry, asynchronous, chunks):
    async def source():
        for chunk in chunks:
            yield chunk

    async def run():
        stream = await wrapped_stream(telemetry, source(), True)
        return [chunk async for chunk in stream]

    if asynchronous:
        received = asyncio.run(run())
    else:
        received = list(wrapped_stream(telemetry, iter(chunks), False))
    assert received == chunks
    spans = telemetry[1].get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert json.loads(attrs["output.value"])["generated_text"] == "".join(chunks)
    assert spans[0].status.status_code == StatusCode.OK
    assert "gen_ai.usage.output_tokens" not in attrs  # Never estimate token counts.


@pytest.mark.parametrize("asynchronous", [False, True])
def test_close_failure_is_recorded_and_propagated(telemetry, asynchronous):
    error = RuntimeError("close failed")

    class Source:
        def __iter__(self):
            return self

        def __next__(self):
            return "hello"

        def __aiter__(self):
            return self

        async def __anext__(self):
            return next(self)

        def close(self):
            raise error

        async def aclose(self):
            raise error

    async def run():
        stream = await wrapped_stream(telemetry, Source(), True)
        with pytest.raises(RuntimeError) as raised:
            await stream.aclose()
        assert raised.value is error
        await stream.aclose()

    if asynchronous:
        asyncio.run(run())
    else:
        stream = wrapped_stream(telemetry, Source(), False)
        with pytest.raises(RuntimeError) as raised:
            stream.close()
        assert raised.value is error
        stream.close()
    spans = telemetry[1].get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


def test_concurrent_async_streams_keep_separate_contexts(telemetry):
    async def consume(text):
        producer_context = []

        async def source():
            producer_context.append(trace.get_current_span().get_span_context().span_id)
            yield text
            await asyncio.sleep(0)
            producer_context.append(trace.get_current_span().get_span_context().span_id)
            yield " end"

        stream = await wrapped_stream(telemetry, source(), True)
        chunks = []
        async for chunk in stream:
            assert not trace.get_current_span().get_span_context().is_valid
            chunks.append(chunk)
            await asyncio.sleep(0)
        assert chunks == [text, " end"]
        assert len(set(producer_context)) == 1
        return producer_context[0]

    async def run():
        return await asyncio.gather(consume("first"), consume("second"))

    span_ids = asyncio.run(run())
    assert len(set(span_ids)) == 2
    spans = telemetry[1].get_finished_spans()
    assert {span.context.span_id for span in spans} == set(span_ids)
    assert {
        json.loads(span.attributes["output.value"])["generated_text"] for span in spans
    } == {"first end", "second end"}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_private_partial_output_and_exception_do_not_reach_otlp(
    telemetry, asynchronous
):
    error = RuntimeError("private provider error body")

    def source():
        yield "private partial output"
        raise error

    async def async_source():
        for chunk in source():
            yield chunk

    config = TraceConfig(hide_outputs=True)

    async def run():
        stream = await wrapped_stream(telemetry, async_source(), True, config=config)
        with pytest.raises(RuntimeError) as raised:
            async for _ in stream:
                pass
        assert raised.value is error

    if asynchronous:
        asyncio.run(run())
    else:
        stream = wrapped_stream(telemetry, source(), False, config=config)
        with pytest.raises(RuntimeError) as raised:
            list(stream)
        assert raised.value is error
    spans = telemetry[1].get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR
    wire = encode_spans(spans).SerializeToString()
    assert b"private partial output" not in wire
    assert b"private provider error body" not in wire
    assert b"RuntimeError" in wire
