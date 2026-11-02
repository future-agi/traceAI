"""HTTP EOF is not evidence that every requested chat choice completed."""
import asyncio
import json

import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode

from .test_openai_compatible_spans import (
    REQUEST_MODEL, async_client, client, make_tracer, only_span,
)


def chunk(index=0, finish_reason=None, content=None):
    return {
        "id": "synthetic", "object": "chat.completion.chunk", "created": 1,
        "model": REQUEST_MODEL,
        "choices": [{"index": index, "delta": {"content": content} if content else {},
                     "finish_reason": finish_reason}],
    }


def stream_response(chunks, done):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("done", [False, True])
@pytest.mark.parametrize("case", [
    "empty", "partial", "usage_only", "stop", "length", "tool_calls", "content_filter",
    "function_call", "two_complete", "second_partial", "second_missing", "usage_tail",
])
def test_chat_completion_requires_terminal_choices(make_tracer, asynchronous, done, case):
    exporter = make_tracer(TraceConfig(hide_inputs=True, hide_outputs=True, hide_error_details=True))
    chunks = [chunk(content="synthetic-private-output")]
    n = 1
    complete = case in {"stop", "length", "tool_calls", "content_filter", "function_call", "two_complete", "usage_tail"}
    usage = {**chunk(), "choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
    if case == "empty":
        chunks = []
    elif case == "usage_only":
        chunks = [usage]
    elif case in {"two_complete", "second_partial", "second_missing"}:
        n = 2
        chunks.append(chunk(finish_reason="stop"))
        if case != "second_missing":
            chunks.append(chunk(index=1, content="second"))
        if case == "two_complete":
            chunks.append(chunk(index=1, finish_reason="stop"))
    elif case == "usage_tail":
        chunks.extend([chunk(finish_reason="stop"), usage])
    elif complete:
        chunks.append(chunk(finish_reason=case))
    params = dict(model=REQUEST_MODEL, messages=[{"role": "user", "content": "synthetic-input"}], stream=True, n=n)

    async def run_async():
        async with async_client(lambda _: stream_response(chunks, done)) as sdk:
            stream = await sdk.chat.completions.create(**params)
            actual = [c async for c in stream]
            assert len(actual) == len(chunks)
            await stream.close()

    if asynchronous:
        asyncio.run(run_async())
    else:
        with client(lambda _: stream_response(chunks, done)) as sdk:
            stream = sdk.chat.completions.create(**params)
            actual = list(stream)
            assert len(actual) == len(chunks)
            stream.close()
    span, attrs = only_span(exporter)
    assert span.status.status_code == (StatusCode.OK if complete else StatusCode.ERROR)
    assert "synthetic-private-output" not in str(attrs)
    if case == "usage_tail":
        assert attrs["gen_ai.usage.total_tokens"] == 5


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("consumer_error", [False, True])
def test_context_exit_uses_observed_terminal_state(make_tracer, asynchronous, terminal, consumer_error):
    exporter = make_tracer()
    chunks = [chunk(content="partial", finish_reason="stop" if terminal else None)]
    params = dict(model=REQUEST_MODEL, messages=[{"role": "user", "content": "hi"}], stream=True)
    error = ValueError("synthetic consumer error")

    async def run_async():
        async with async_client(lambda _: stream_response(chunks, True)) as sdk:
            stream = await sdk.chat.completions.create(**params)
            async with stream:
                await stream.__anext__()
                if consumer_error:
                    raise error

    def run_sync():
        with client(lambda _: stream_response(chunks, True)) as sdk:
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
    span, _ = only_span(exporter)
    assert span.status.status_code == (StatusCode.OK if terminal and not consumer_error else StatusCode.ERROR)
    assert len([e for e in span.events if e.name == "exception"]) == int(consumer_error)
