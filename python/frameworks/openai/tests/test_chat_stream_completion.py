"""Judge exhausted chat streams only when the provider reports finish reasons."""

import asyncio
import json

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode

from .test_openai_compatible_spans import REQUEST_MODEL, make_tracer, only_span


@pytest.fixture(params=["openai", "deepseek"])
def provider(request):
    return request.param


def client(handler, provider):
    return openai.OpenAI(
        api_key="synthetic-key", base_url=f"https://api.{provider}.com/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )


def async_client(handler, provider):
    return openai.AsyncOpenAI(
        api_key="synthetic-key", base_url=f"https://api.{provider}.com/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False),
    )


def chunk(index=0, finish_reason=None, content=None):
    return {
        "id": "synthetic", "object": "chat.completion.chunk", "created": 1,
        "model": REQUEST_MODEL,
        "choices": [{"index": index, "delta": {"content": content} if content else {},
                     "finish_reason": finish_reason}],
    }


def stream_response(chunks, done):
    body = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("done", [False, True], ids=["eof", "done"])
@pytest.mark.parametrize("case", [
    "empty", "partial", "usage_only", "stop", "length", "tool_calls", "content_filter",
    "function_call", "other", "empty_reason", "two_complete", "second_partial", "second_missing",
    "usage_tail", "duplicate_first", "wrong_indices", "extra_partial", "two_without_reasons",
    "null_tail",
])
def test_exhausted_chat_completion_checks_reported_choices(make_tracer, provider, asynchronous, done, case):
    exporter = make_tracer(TraceConfig(hide_inputs=True, hide_outputs=True, hide_error_details=True))
    chunks = [chunk(content="synthetic-private-output")]
    n = 1
    expected_status = (
        StatusCode.ERROR
        if case in {"second_partial", "second_missing", "duplicate_first", "wrong_indices"}
        else StatusCode.OK
    )
    usage = {**chunk(), "choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
    if case == "empty":
        chunks = []
    elif case == "usage_only":
        chunks = [usage]
    elif case in {"two_complete", "second_partial", "second_missing", "duplicate_first", "wrong_indices"}:
        n = 2
        chunks.append(chunk(finish_reason="stop"))
        if case == "duplicate_first":
            chunks.append(chunk(finish_reason="stop"))
        elif case == "wrong_indices":
            chunks.append(chunk(index=2, finish_reason="stop"))
        elif case != "second_missing":
            chunks.append(chunk(index=1, content="synthetic-second"))
        if case == "two_complete":
            chunks.append(chunk(index=1, finish_reason="stop"))
    elif case == "two_without_reasons":
        n = 2
        chunks.append(chunk(index=1, content="synthetic-second"))
    elif case == "extra_partial":
        chunks.extend([chunk(finish_reason="stop"), chunk(index=1, content="synthetic-extra")])
    elif case == "usage_tail":
        chunks.extend([chunk(finish_reason="stop"), usage])
    elif case == "null_tail":
        chunks.extend([chunk(finish_reason="stop"), chunk()])
    elif case == "empty_reason":
        chunks.append(chunk(finish_reason=""))
    elif case != "partial":
        chunks.append(chunk(finish_reason=case))
    params = dict(
        model=REQUEST_MODEL, messages=[{"role": "user", "content": "synthetic-input"}], stream=True, n=n,
    )

    async def run_async():
        async with async_client(lambda _: stream_response(chunks, done), provider) as sdk:
            stream = await sdk.chat.completions.create(**params)
            assert len([item async for item in stream]) == len(chunks)
            await stream.close()

    if asynchronous:
        asyncio.run(run_async())
    else:
        with client(lambda _: stream_response(chunks, done), provider) as sdk:
            stream = sdk.chat.completions.create(**params)
            assert len(list(stream)) == len(chunks)
            stream.close()
    span, attrs = only_span(exporter)
    assert span.status.status_code == expected_status
    assert attrs["gen_ai.provider.name"] == provider
    assert "synthetic-private-output" not in str(attrs)
    if case == "usage_tail":
        assert attrs["gen_ai.usage.total_tokens"] == 5


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("declared_n,override_n,completed_choices,expected_status", [
    pytest.param(2, 1, 1, StatusCode.OK, id="override-down"),
    pytest.param(1, 2, 1, StatusCode.ERROR, id="override-up-incomplete"),
    pytest.param(1, 2, 2, StatusCode.OK, id="override-up-complete"),
    pytest.param(2, openai.Omit(), 1, StatusCode.OK, id="remove-n"),
    pytest.param(2, None, 1, StatusCode.OK, id="null-n"),
    pytest.param(openai.NOT_GIVEN, 2, 1, StatusCode.ERROR, id="extra-body-only-n"),
])
def test_completion_count_uses_effective_body(
    make_tracer, provider, asynchronous, declared_n, override_n, completed_choices, expected_status,
):
    exporter = make_tracer()
    chunks = [chunk(index=index, finish_reason="stop") for index in range(completed_choices)]
    wire_bodies = []

    def handler(request):
        body = json.loads(request.content)
        wire_bodies.append(body)
        if isinstance(override_n, openai.Omit):
            assert "n" not in body
        else:
            assert body["n"] == override_n
        return stream_response(chunks, done=True)

    extra_body = {"n": override_n, "synthetic_extra": "not-exported"}
    params = dict(
        model=REQUEST_MODEL, messages=[{"role": "user", "content": "synthetic-input"}],
        stream=True, n=declared_n, extra_body=extra_body,
    )

    async def run_async():
        async with async_client(handler, provider) as sdk:
            stream = await sdk.chat.completions.create(**params)
            assert len([item async for item in stream]) == completed_choices
            await stream.close()

    if asynchronous:
        asyncio.run(run_async())
    else:
        with client(handler, provider) as sdk:
            stream = sdk.chat.completions.create(**params)
            assert len(list(stream)) == completed_choices
            stream.close()

    assert len(wire_bodies) == 1
    assert extra_body == {"n": override_n, "synthetic_extra": "not-exported"}
    span, attrs = only_span(exporter)
    assert span.status.status_code == expected_status
    assert attrs["gen_ai.provider.name"] == provider
    parameters = json.loads(attrs["gen_ai.request.parameters"])
    if declared_n is openai.NOT_GIVEN:
        assert "n" not in parameters
    else:
        assert parameters["n"] == declared_n
    assert "synthetic_extra" not in str(attrs)
    assert "not-exported" not in str(attrs)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("terminal", [False, True], ids=["partial", "complete"])
@pytest.mark.parametrize("consumer_error", [False, True], ids=["clean-exit", "consumer-error"])
@pytest.mark.parametrize("n", [1, 2])
def test_early_context_exit_preserves_caller_outcome(
    make_tracer, provider, asynchronous, terminal, consumer_error, n,
):
    exporter = make_tracer()
    chunks = [chunk(content="synthetic-partial", finish_reason="stop" if terminal else None)]
    params = dict(
        model=REQUEST_MODEL, messages=[{"role": "user", "content": "synthetic-input"}], stream=True, n=n,
    )
    error = ValueError("synthetic consumer error")

    async def run_async():
        async with async_client(lambda _: stream_response(chunks, True), provider) as sdk:
            stream = await sdk.chat.completions.create(**params)
            async with stream:
                await stream.__anext__()
                if consumer_error:
                    raise error

    def run_sync():
        with client(lambda _: stream_response(chunks, True), provider) as sdk:
            stream = sdk.chat.completions.create(**params)
            with stream:
                next(stream)
                if consumer_error:
                    raise error

    def run():
        if asynchronous:
            asyncio.run(run_async())
        else:
            run_sync()

    if consumer_error:
        with pytest.raises(ValueError) as caught:
            run()
        assert caught.value is error
    else:
        run()
    span, attrs = only_span(exporter)
    assert span.status.status_code == (StatusCode.ERROR if consumer_error else StatusCode.OK)
    assert attrs["gen_ai.provider.name"] == provider
    assert len([event for event in span.events if event.name == "exception"]) == int(consumer_error)


@pytest.mark.parametrize("close_method", ["sync-close", "async-close", "async-aclose"])
def test_early_close_with_an_unfinished_choice_stays_ok(make_tracer, provider, close_method):
    exporter = make_tracer()
    chunks = [
        chunk(finish_reason="stop"), chunk(index=1, content="synthetic-partial"),
        chunk(index=1, finish_reason="stop"),
    ]
    params = dict(
        model=REQUEST_MODEL, messages=[{"role": "user", "content": "synthetic-input"}], stream=True, n=2,
    )

    async def run_async():
        async with async_client(lambda _: stream_response(chunks, True), provider) as sdk:
            stream = await sdk.chat.completions.create(**params)
            await stream.__anext__()
            await stream.__anext__()
            close = stream.aclose if close_method == "async-aclose" else stream.close
            await close()
            await close()

    if close_method == "sync-close":
        with client(lambda _: stream_response(chunks, True), provider) as sdk:
            stream = sdk.chat.completions.create(**params)
            next(stream)
            next(stream)
            stream.close()
            stream.close()
    else:
        asyncio.run(run_async())

    span, attrs = only_span(exporter)
    assert span.status.status_code == StatusCode.OK
    assert attrs["gen_ai.provider.name"] == provider
    assert not [event for event in span.events if event.name == "exception"]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_exhausted_context_retains_incomplete_status(make_tracer, provider, asynchronous):
    exporter = make_tracer()
    chunks = [chunk(finish_reason="stop"), chunk(index=1, content="synthetic-partial")]
    params = dict(
        model=REQUEST_MODEL, messages=[{"role": "user", "content": "synthetic-input"}], stream=True, n=2,
    )

    async def run_async():
        async with async_client(lambda _: stream_response(chunks, True), provider) as sdk:
            stream = await sdk.chat.completions.create(**params)
            async with stream:
                assert len([item async for item in stream]) == len(chunks)

    if asynchronous:
        asyncio.run(run_async())
    else:
        with client(lambda _: stream_response(chunks, True), provider) as sdk:
            with sdk.chat.completions.create(**params) as stream:
                assert len(list(stream)) == len(chunks)

    span, attrs = only_span(exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "Stream ended before all choices finished"
    assert attrs["gen_ai.provider.name"] == provider
