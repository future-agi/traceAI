"""Deterministic interleaving, usage, routing and stream-abandonment contracts."""

import asyncio
import gc
import json
import weakref
from contextlib import contextmanager
from unittest.mock import Mock

import litellm
import pytest
from fi_instrumentation import TraceConfig
from litellm.types.utils import ModelResponse
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_litellm import LiteLLMInstrumentor


@pytest.fixture
def capture(monkeypatch):
    @contextmanager
    def install(chunks=None, result=None, config=None, endpoint="completion"):
        async def acall(model=None, **kwargs):
            async def stream():
                for chunk in chunks:
                    if isinstance(chunk, BaseException):
                        raise chunk
                    yield chunk

            return stream() if chunks is not None else result

        def sync_call(model=None, **kwargs):
            if chunks is None:
                return result

            def stream():
                for chunk in chunks:
                    if isinstance(chunk, BaseException):
                        raise chunk
                    yield chunk

            return stream()

        monkeypatch.setattr(litellm, endpoint, sync_call, raising=False)
        monkeypatch.setattr(litellm, "a" + endpoint, acall, raising=False)
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        inst = LiteLLMInstrumentor()
        inst.instrument(tracer_provider=provider, config=config or TraceConfig())
        try:
            yield exporter, provider
        finally:
            inst.uninstrument()
            provider.shutdown()

    return install


async def invoke(asynchronous, *, endpoint="completion", **kwargs):
    params = dict(model="openai/gpt-4o-mini", **kwargs)
    if asynchronous:
        return await getattr(litellm, "a" + endpoint)(**params)
    return getattr(litellm, endpoint)(**params)


async def consume(stream, asynchronous, partial=False):
    if asynchronous:
        if partial:
            await stream.__anext__()
        else:
            [chunk async for chunk in stream]
        await stream.aclose()
    else:
        if partial:
            next(stream)
        else:
            list(stream)
        stream.close()


def attrs(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return dict(spans[0].attributes)


def tool_chunks():
    chunks = []
    for part in (0, 1):
        choices = []
        for choice in (1, 0):
            tools = []
            for tool in (1, 0):
                function = {
                    "arguments": '{"value":' if part == 0 else f"{choice * 10 + tool}}}"
                }
                data = {"index": tool, "function": function}
                if part == 0:
                    data.update(id=f"call-{choice}-{tool}", type="function")
                    function["name"] = f"tool-{choice}-{tool}"
                tools.append(data)
            choices.append(
                {"index": choice, "delta": {"role": "assistant", "tool_calls": tools}}
            )
        chunks.append({"model": "sdk-stream-model", "choices": choices})
    return chunks


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize(
    "flag", [None, "hide_outputs", "hide_output_messages", "hide_output_text"]
)
@pytest.mark.asyncio
async def test_interleaved_tools(capture, asynchronous, partial, flag):
    config = TraceConfig(**({flag: True} if flag else {}))
    with capture(chunks=tool_chunks(), config=config) as (exporter, _):
        stream = await invoke(asynchronous, stream=True)
        await consume(stream, asynchronous, partial)
    actual = attrs(exporter)
    assert actual["gen_ai.response.model"] == "sdk-stream-model"
    for choice in (0, 1):
        for tool in (0, 1):
            prefix = (
                f"gen_ai.output.messages.{choice}.message.tool_calls.{tool}.tool_call."
            )
            arguments = '{"value":' + ("" if partial else f"{choice * 10 + tool}}}")
            if flag:
                assert "value" not in json.dumps(list(actual.values()))
            else:
                assert actual[prefix + "id"] == f"call-{choice}-{tool}"
                assert actual[prefix + "function.name"] == f"tool-{choice}-{tool}"
                assert actual[prefix + "function.arguments"] == arguments


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("finish", ["discard", "close", "exhaust"])
@pytest.mark.asyncio
async def test_abandoned_stream_ends_once(capture, asynchronous, partial, finish):
    with capture(chunks=tool_chunks()) as (exporter, provider):
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            stream = await invoke(asynchronous, stream=True)
            span = stream.span
            end = Mock(wraps=span.end)
            span.end = end
            if partial:
                if asynchronous:
                    await stream.__anext__()
                else:
                    next(stream)
            if finish == "close":
                if asynchronous:
                    await stream.aclose()
                    await stream.aclose()
                else:
                    stream.close()
                    stream.close()
            elif finish == "exhaust":
                await consume(stream, asynchronous)
            ref = weakref.ref(stream)
            del stream
            gc.collect()
            assert ref() is None
            assert end.call_count == 1
            actual = attrs(exporter)
            assert not span.is_recording()
            assert trace.get_current_span() is parent
            if partial or finish == "exhaust":
                assert (
                    actual[
                        "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"
                    ]
                    == "tool-0-0"
                )
            if finish == "discard":
                assert (
                    exporter.get_finished_spans()[0].status.status_code
                    is StatusCode.UNSET
                )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("value", [None, 0, 7])
@pytest.mark.asyncio
async def test_detailed_usage_missing_zero_and_reported_cost(
    capture, asynchronous, streaming, value
):
    usage = {
        "prompt_tokens": value,
        "completion_tokens": value,
        "total_tokens": value,
        "prompt_tokens_details": {"cached_tokens": value, "audio_tokens": value},
        "completion_tokens_details": {"reasoning_tokens": value, "audio_tokens": value},
        "cache_creation_input_tokens": value,
        "cost": value,
    }
    result = {"model": "actual-model", "choices": [], "usage": usage}
    with capture(chunks=[result] if streaming else None, result=result) as (
        exporter,
        _,
    ):
        response = await invoke(asynchronous, stream=streaming)
        if streaming:
            await consume(response, asynchronous)
    actual = attrs(exporter)
    for key in (
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.total_tokens",
        "gen_ai.usage.input_tokens.cache_read",
        "gen_ai.usage.input_tokens.cache_write",
        "gen_ai.usage.input_tokens.audio",
        "gen_ai.usage.output_tokens.audio",
        "gen_ai.usage.output_tokens.reasoning",
        "gen_ai.cost.total",
    ):
        if value is None:
            assert key not in actual
        else:
            assert actual[key] == value


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("metadata_provider", [None, "azure"])
@pytest.mark.asyncio
async def test_explicit_routing_and_private_metadata(
    capture, asynchronous, metadata_provider
):
    result = ModelResponse(model="actual-deployment")
    result._hidden_params = {
        "api_key": "synthetic-hidden-secret",
        "response_cost": 0.003,
    }
    if metadata_provider:
        result._hidden_params["custom_llm_provider"] = metadata_provider
    with capture(result=result) as (exporter, _):
        await invoke(asynchronous, custom_llm_provider="vertex_ai")
    actual = attrs(exporter)
    assert actual["gen_ai.provider.name"] == (metadata_provider or "vertex_ai")
    assert actual["gen_ai.response.model"] == "actual-deployment"
    assert actual["gen_ai.cost.total"] == 0.003
    assert "synthetic-hidden-secret" not in json.dumps(actual)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("ending", ["close", "error", "cancel", "failed", "discard"])
@pytest.mark.asyncio
async def test_responses_partial_stream_lifecycle(capture, asynchronous, ending):
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "call_id": "call-synthetic",
                "name": "lookup",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "delta": '{"value":',
        },
    ]
    error = (
        asyncio.CancelledError()
        if ending == "cancel"
        else ValueError("synthetic-stream-error")
    )
    if ending in ("error", "cancel"):
        events.append(error)
    elif ending == "failed":
        events.append({"type": "response.failed", "response": {"status": "failed"}})
    with capture(chunks=events, endpoint="responses") as (exporter, provider):
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            stream = await invoke(
                asynchronous, endpoint="responses", input="hello", stream=True
            )
            if ending in ("error", "cancel"):
                with pytest.raises(type(error)) as caught:
                    await consume(stream, asynchronous)
                assert caught.value is error
            elif ending == "failed":
                await consume(stream, asynchronous)
            else:
                for _ in range(2):
                    if asynchronous:
                        await stream.__anext__()
                    else:
                        next(stream)
                if ending == "close":
                    if asynchronous:
                        await stream.aclose()
                    else:
                        stream.close()
            del stream
            gc.collect()
            assert trace.get_current_span() is parent
            actual = attrs(exporter)
            assert (
                actual[
                    "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments"
                ]
                == '{"value":'
            )
            status = exporter.get_finished_spans()[0].status.status_code
            assert status is (
                StatusCode.ERROR
                if ending in ("error", "cancel", "failed")
                else StatusCode.UNSET
                if ending == "discard"
                else StatusCode.OK
            )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_bad_telemetry_does_not_change_success(capture, asynchronous):
    class UnserializableResponse:
        def model_dump(self):
            raise ValueError("synthetic-extraction-error")

    result = UnserializableResponse()
    with capture(result=result) as (exporter, _):
        assert await invoke(asynchronous) is result
    assert exporter.get_finished_spans()[0].status.status_code is StatusCode.OK


def test_instrumentation_does_not_create_absent_responses_methods(monkeypatch):
    for name in ("responses", "aresponses"):
        monkeypatch.delattr(litellm, name, raising=False)
    provider = TracerProvider()
    inst = LiteLLMInstrumentor()
    try:
        inst.instrument(tracer_provider=provider)
        assert not hasattr(litellm, "responses")
        assert not hasattr(litellm, "aresponses")
        inst.uninstrument()
        assert not hasattr(litellm, "responses")
        assert not hasattr(litellm, "aresponses")
    finally:
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.asyncio
async def test_responses_failed_payload_privacy(
    capture, asynchronous, streaming, private, monkeypatch
):
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", str(private).lower())
    result = {
        "object": "response",
        "status": "failed",
        "output": [],
        "error": {"message": "synthetic-private-error", "code": "server_error"},
    }
    chunks = [{"type": "response.failed", "response": result}]
    with capture(
        chunks=chunks if streaming else None, result=result, endpoint="responses"
    ) as (exporter, _):
        response = await invoke(
            asynchronous, endpoint="responses", input="hello", stream=streaming
        )
        if streaming:
            await consume(response, asynchronous)
    actual = attrs(exporter)
    assert ("synthetic-private-error" in json.dumps(actual)) is not private
    assert result["error"]["message"] == "synthetic-private-error"
    assert exporter.get_finished_spans()[0].status.status_code is StatusCode.ERROR
