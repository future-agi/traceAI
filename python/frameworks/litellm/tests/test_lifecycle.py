"""Offline regressions using real SDK response objects and exported OTel spans."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import litellm
import pytest
from litellm.types.utils import ModelResponse
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import traceai_litellm
from fi_instrumentation.fi_types import SpanAttributes
from traceai_litellm import LiteLLMInstrumentor

MESSAGES = [
    {"role": "system", "content": "Be helpful"},
    {"role": "user", "content": "Hello"},
]


def response():
    return ModelResponse(
        model="gpt-4o-mini",
        choices=[{"message": {"role": "assistant", "content": "hello"}}],
        usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    )


@pytest.fixture
def instrument(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Keep each SDK test independent of other instrumentors in the core suite.
    inst = LiteLLMInstrumentor()
    if inst.is_instrumented_by_opentelemetry:
        inst.uninstrument()
    inst.original_litellm_funcs.clear()
    monkeypatch.setattr(traceai_litellm, "Protect", None)

    @contextmanager
    def install(**functions):
        for name, fn in functions.items():
            monkeypatch.setattr(litellm, name, fn)
        inst.instrument(tracer_provider=provider)
        try:
            yield exporter, provider
        finally:
            inst.uninstrument()

    yield install
    if inst.is_instrumented_by_opentelemetry:
        inst.uninstrument()
    provider.shutdown()


def only_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def test_positional_request_and_full_context(instrument):
    def completion(model, messages, **kwargs):
        return response()

    with instrument(completion=completion) as (exporter, _):
        litellm.completion("gpt-4o-mini", MESSAGES)
    attrs = only_span(exporter).attributes
    assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4o-mini"
    assert all(m["content"] in attrs[SpanAttributes.INPUT_VALUE] for m in MESSAGES)


def test_non_json_options_do_not_break_request(instrument):
    result = response()
    with instrument(completion=lambda **kwargs: result) as (exporter, _):
        assert (
            litellm.completion(
                model="gpt-4o-mini", messages=MESSAGES, response_format=object()
            )
            is result
        )
    assert only_span(exporter).status.status_code is StatusCode.OK


def test_usage_can_be_absent(instrument):
    result = response()
    result.usage = None
    with instrument(completion=lambda **kwargs: result) as (exporter, _):
        assert litellm.completion(model="gpt-4o-mini", messages=MESSAGES) is result
    assert only_span(exporter).status.status_code is StatusCode.OK


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_stream_construction_error_preserves_exception_and_parent(
    instrument, async_call
):
    error = ValueError("synthetic error")

    def completion(**kwargs):
        raise error

    async def acompletion(**kwargs):
        raise error

    with instrument(completion=completion, acompletion=acompletion) as (
        exporter,
        provider,
    ):
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            with pytest.raises(ValueError) as caught:
                if async_call:
                    await litellm.acompletion(
                        model="gpt-4o-mini", messages=MESSAGES, stream=True
                    )
                else:
                    litellm.completion(
                        model="gpt-4o-mini", messages=MESSAGES, stream=True
                    )
            assert caught.value is error
            assert trace.get_current_span() is parent
            assert only_span(exporter).status.status_code is StatusCode.ERROR


def stream_chunks():
    yield {"choices": [{"delta": {"content": "hello"}}]}
    yield {
        "choices": [],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def test_sync_stream_usage_and_context(instrument):
    with instrument(completion=lambda **kwargs: stream_chunks()) as (
        exporter,
        provider,
    ):
        with provider.get_tracer("test").start_as_current_span("parent") as parent:
            stream = litellm.completion(
                model="gpt-4o-mini", messages=MESSAGES, stream=True
            )
            assert trace.get_current_span() is parent
            assert list(stream) == list(stream_chunks())
            assert trace.get_current_span() is parent
            span = only_span(exporter)
            assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "hello"
            assert span.attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 3
            assert span.parent.span_id == parent.get_span_context().span_id
            stream.close()
            assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
async def test_async_stream_usage_and_cross_task_close(instrument):
    async def chunks():
        for chunk in stream_chunks():
            yield chunk

    async def acompletion(**kwargs):
        return chunks()

    with instrument(acompletion=acompletion) as (exporter, _):
        stream = await litellm.acompletion(
            model="gpt-4o-mini", messages=MESSAGES, stream=True
        )
        assert not trace.get_current_span().get_span_context().is_valid

        async def consume():
            assert [c async for c in stream] == list(stream_chunks())
            await stream.aclose()

        await asyncio.create_task(consume())
        span = only_span(exporter)
        assert span.attributes[SpanAttributes.OUTPUT_VALUE] == "hello"
        assert span.attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 3


def test_sync_stream_read_error_ends_span(instrument):
    error = ValueError("stream read failed")

    def chunks():
        yield next(stream_chunks())
        raise error

    with instrument(completion=lambda **kwargs: chunks()) as (exporter, _):
        stream = litellm.completion(model="gpt-4o-mini", messages=MESSAGES, stream=True)
        with pytest.raises(ValueError) as caught:
            list(stream)
        assert caught.value is error
        assert only_span(exporter).status.status_code is StatusCode.ERROR


@pytest.mark.asyncio
async def test_async_stream_cancel_ends_span(instrument):
    async def chunks():
        yield next(stream_chunks())
        raise asyncio.CancelledError()

    async def acompletion(**kwargs):
        return chunks()

    with instrument(acompletion=acompletion) as (exporter, _):
        stream = await litellm.acompletion(
            model="gpt-4o-mini", messages=MESSAGES, stream=True
        )
        with pytest.raises(asyncio.CancelledError):
            async for _ in stream:
                pass
        assert only_span(exporter).status.status_code is StatusCode.ERROR


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_early_close_closes_upstream_once(instrument, async_call):
    closed = []

    def chunks():
        try:
            yield next(stream_chunks())
            yield next(stream_chunks())
        finally:
            closed.append(True)

    async def achunks():
        try:
            yield next(stream_chunks())
            yield next(stream_chunks())
        finally:
            closed.append(True)

    async def acompletion(**kwargs):
        return achunks()

    with instrument(completion=lambda **kwargs: chunks(), acompletion=acompletion) as (
        exporter,
        _,
    ):
        if async_call:
            stream = await litellm.acompletion(
                model="gpt-4o-mini", messages=MESSAGES, stream=True
            )
            await stream.__anext__()
            await stream.aclose()
            await stream.aclose()
        else:
            stream = litellm.completion(
                model="gpt-4o-mini", messages=MESSAGES, stream=True
            )
            next(stream)
            stream.close()
            stream.close()
        assert closed == [True]
        assert only_span(exporter).attributes[SpanAttributes.OUTPUT_VALUE] == "hello"


def test_retries_have_one_span(instrument):
    def retry(**kwargs):
        return litellm.completion(**kwargs)

    with instrument(
        completion=lambda **kwargs: response(), completion_with_retries=retry
    ) as (exporter, _):
        litellm.completion_with_retries(model="gpt-4o-mini", messages=MESSAGES)
    assert only_span(exporter).name == "completion_with_retries"


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_real_sdk_mock_completion(instrument, async_call):
    # This uses LiteLLM's own mock transport, not a mocked public API function.
    with instrument() as (exporter, _):
        kwargs = dict(
            model="gpt-4o-mini",
            messages=MESSAGES,
            mock_response="hello",
            api_key="synthetic-not-a-key",
        )
        result = (
            await litellm.acompletion(**kwargs)
            if async_call
            else litellm.completion(**kwargs)
        )
        assert result.choices[0].message.content == "hello"
    assert only_span(exporter).attributes[SpanAttributes.OUTPUT_VALUE] == "hello"


def test_suppression_and_uninstrument_restore_original(instrument):
    original = lambda **kwargs: response()
    with instrument(completion=original) as (exporter, _):
        token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            litellm.completion(model="gpt-4o-mini", messages=MESSAGES)
        finally:
            context.detach(token)
        assert not exporter.get_finished_spans()
    assert litellm.completion is original


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.asyncio
async def test_real_sdk_stream(instrument, async_call):
    with instrument() as (exporter, _):
        kwargs = dict(
            model="gpt-4o-mini",
            messages=MESSAGES,
            mock_response="hello",
            api_key="synthetic-not-a-key",
            stream=True,
        )
        if async_call:
            stream = await litellm.acompletion(**kwargs)
            chunks = [chunk async for chunk in stream]
            await stream.aclose()
        else:
            stream = litellm.completion(**kwargs)
            chunks = list(stream)
            stream.close()
        assert chunks
    assert only_span(exporter).attributes[SpanAttributes.OUTPUT_VALUE] == "hello"


def test_real_retry_positional_arguments(instrument):
    with instrument() as (exporter, _):
        result = litellm.completion_with_retries(
            "gpt-4o-mini",
            MESSAGES,
            mock_response="hello",
            api_key="synthetic-not-a-key",
        )
        assert result.choices[0].message.content == "hello"
    span = only_span(exporter)
    assert span.attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4o-mini"
    assert all(
        m["content"] in span.attributes[SpanAttributes.INPUT_VALUE] for m in MESSAGES
    )


def test_uninstrument_restores_protect(monkeypatch):
    original = lambda *args, **kwargs: None
    protect = SimpleNamespace(protect=original)
    monkeypatch.setattr(traceai_litellm, "Protect", protect)

    def install_wrapper(**kwargs):
        protect.protect = lambda *args, **kwargs: original(*args, **kwargs)

    monkeypatch.setattr(traceai_litellm, "wrap_function_wrapper", install_wrapper)
    provider = TracerProvider()
    inst = LiteLLMInstrumentor()
    try:
        inst.instrument(tracer_provider=provider)
        assert protect.protect is not original
        inst.uninstrument()
        assert protect.protect is original
    finally:
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
        provider.shutdown()


def test_provider_credentials_are_not_exported(instrument):
    import json

    seen = []

    def completion(**kwargs):
        seen.append(kwargs)
        return response()

    with instrument(completion=completion) as (exporter, _):
        litellm.completion(
            model="gpt-4o-mini",
            messages=MESSAGES,
            api_key="synthetic-provider-secret",
            extra_headers={"Authorization": "Bearer synthetic-header-secret"},
        )
    attrs = json.dumps(dict(only_span(exporter).attributes))
    assert "synthetic-provider-secret" not in attrs
    assert "synthetic-header-secret" not in attrs
    assert seen[0]["api_key"] == "synthetic-provider-secret"
    assert seen[0]["extra_headers"] == {
        "Authorization": "Bearer synthetic-header-secret"
    }


def test_real_protect_descriptor_restored(monkeypatch):
    import sys
    import types

    class FakeProtect:
        def protect(self, value):
            return value

    original = FakeProtect.protect
    fake_module = types.ModuleType("fi.evals")
    fake_module.Protect = FakeProtect
    fake_parent = types.ModuleType("fi")
    fake_parent.evals = fake_module
    monkeypatch.setitem(sys.modules, "fi", fake_parent)
    monkeypatch.setitem(sys.modules, "fi.evals", fake_module)
    monkeypatch.setattr(traceai_litellm, "Protect", FakeProtect)
    provider = TracerProvider()
    inst = LiteLLMInstrumentor()
    try:
        inst.instrument(tracer_provider=provider)
        assert FakeProtect.protect is not original
        inst.uninstrument()
        assert FakeProtect.protect is original
        assert FakeProtect().protect("ok") == "ok"
    finally:
        if inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
        provider.shutdown()
