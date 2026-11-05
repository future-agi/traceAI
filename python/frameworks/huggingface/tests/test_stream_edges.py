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

@pytest.mark.parametrize("asynchronous", [False, True])
def test_full_text_stream_hides_original_prompt(telemetry, asynchronous):
    from fi_instrumentation import FITracer
    from traceai_huggingface._wrappers import (
        _AsyncTextGenerationWrapper, _TextGenerationWrapper,
    )
    from types import SimpleNamespace

    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("privacy"), config=TraceConfig(hide_inputs=True))
    chunks = [
        {"token": {"text": "answer", "special": False}},
        {"generated_text": "synthetic-secret-prompt answer"},
    ]
    if asynchronous:
        async def source():
            for chunk in chunks:
                yield chunk
        async def run():
            async def call(**kw):
                return source()
            stream = await _AsyncTextGenerationWrapper(tracer=tracer)(
                call, SimpleNamespace(model="offline"), (),
                {"prompt": "synthetic-secret-prompt", "stream": True, "return_full_text": True},
            )
            return [chunk async for chunk in stream]
        asyncio.run(run())
    else:
        list(_TextGenerationWrapper(tracer=tracer)(
            lambda **kw: iter(chunks), SimpleNamespace(model="offline"), (),
            {"prompt": "synthetic-secret-prompt", "stream": True, "return_full_text": True},
        ))
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["input.value"] == "__REDACTED__"
    assert "synthetic-secret-prompt" not in encode_spans(exporter.get_finished_spans()).SerializeToString().decode(errors="ignore")
    assert json.loads(attrs["output.value"])["generated_text"] == "answer"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_output_messages", "hide_output_text"])
def test_stream_chat_privacy_flags_do_not_leak_raw_choice(telemetry, asynchronous, flag):
    config = TraceConfig(**{flag: True})
    chunk = {"choices": [{"index": 0, "delta": {
        "content": "synthetic-private-content", "tool_calls": [{"index": 0,
        "function": {"name": "query", "arguments": '{"secret":"synthetic-private-argument"}'}}],
    }}]}
    async def source():
        yield chunk
    async def run():
        stream = await wrapped_stream(telemetry, source(), True, chat=True, config=config)
        return [item async for item in stream]
    if asynchronous:
        asyncio.run(run())
    else:
        list(wrapped_stream(telemetry, iter([chunk]), False, chat=True, config=config))
    attrs = telemetry[1].get_finished_spans()[0].attributes
    assert "output.value" not in attrs or "synthetic-private-content" not in attrs["output.value"]
    assert "output.value" not in attrs or "synthetic-private-argument" not in attrs["output.value"]
    wire = encode_spans(telemetry[1].get_finished_spans()).SerializeToString()
    assert b"synthetic-private-content" not in wire
    if flag == "hide_output_messages":
        assert b"synthetic-private-argument" not in wire


def test_abandoned_async_stream_finalizes_once(telemetry):
    import gc
    async def source():
        yield "partial response"
        yield "unread"
    async def run():
        stream = await wrapped_stream(telemetry, source(), True)
        assert await stream.__anext__() == "partial response"
        del stream
        gc.collect()
        assert len(telemetry[1].get_finished_spans()) == 1
        assert telemetry[1].get_finished_spans()[0].attributes["huggingface.stream.status"] == "abandoned"
    asyncio.run(run())
    telemetry[0].shutdown()
    assert len(telemetry[1].get_finished_spans()) == 1


def test_declared_core_floor_supports_required_span_keys():
    from pathlib import Path
    from fi_instrumentation.fi_types import SpanAttributes
    spec = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert 'fi-instrumentation-otel = ">=1.2.2"' in spec
    assert SpanAttributes.GEN_AI_SPAN_KIND
    assert SpanAttributes.GEN_AI_OUTPUT_MESSAGES


def test_documented_alternate_token_matches_sdk():
    from pathlib import Path
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "`HUGGING_FACE_HUB_TOKEN`" in readme
    assert "`HUGGINGFACE_HUB_TOKEN`" not in readme


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("flag", ["hide_output_messages", "hide_output_text"])
def test_text_stream_privacy_does_not_leak_raw_output(telemetry, asynchronous, flag):
    config = TraceConfig(**{flag: True})
    async def source():
        yield "synthetic-private-text"
    async def run():
        stream = await wrapped_stream(telemetry, source(), True, config=config)
        return [part async for part in stream]
    if asynchronous:
        asyncio.run(run())
    else:
        list(wrapped_stream(telemetry, iter(["synthetic-private-text"]), False, config=config))
    spans = telemetry[1].get_finished_spans()
    assert b"synthetic-private-text" not in encode_spans(spans).SerializeToString()


def test_documented_client_lifetime_for_early_stream_close():
    from pathlib import Path
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "client.close()" in readme
    assert "await client.close()" in readme


@pytest.mark.parametrize("include_tokens", [False, True])
def test_full_text_stream_drops_private_prefill(telemetry, include_tokens):
    from fi_instrumentation import FITracer
    from types import SimpleNamespace
    from traceai_huggingface._wrappers import _TextGenerationWrapper

    provider, exporter = telemetry
    tracer = FITracer(provider.get_tracer("privacy"), config=TraceConfig(hide_inputs=True))
    chunks = ([{"token": {"text": " answer", "special": False}}] if include_tokens else [])
    chunks.append({"generated_text": "synthetic-secret-prompt answer",
                   "details": {"prefill": [{"text": "synthetic-secret-prompt"}], "generated_tokens": 1}})
    list(_TextGenerationWrapper(tracer=tracer)(
        lambda **kw: iter(chunks), SimpleNamespace(model="offline"), (),
        {"prompt": "synthetic-secret-prompt", "stream": True, "return_full_text": True},
    ))
    spans = exporter.get_finished_spans()
    assert b"synthetic-secret-prompt" not in encode_spans(spans).SerializeToString()
    assert json.loads(spans[0].attributes["output.value"])["generated_text"] == (
        " answer" if include_tokens else ""
    )
