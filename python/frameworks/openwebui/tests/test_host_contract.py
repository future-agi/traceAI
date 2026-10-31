"""Released host dispatch and streaming contract, not a full application test."""

import asyncio

import pytest
from starlette.responses import StreamingResponse
from test_pipe import frame, iterator


async def test_released_host_nonstream_and_access(host):
    s, ns = host
    result = await ns["generate_chat_completion"](s.request, s.body, s.user)
    assert result is s.delegate.return_value
    assert [call.args[1]["id"] for call in ns["check_model_access"].call_args_list] == [
        "traceai_openwebui",
        "synthetic",
    ]
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["llm.token_count.total"] == 10


async def test_released_host_stream_is_not_double_framed(host):
    s, ns = host
    chunks = [
        frame(
            {"choices": [{"delta": {"content": "synthetic"}, "finish_reason": "stop"}]}
        ),
        frame({"usage": {"input_tokens": 6, "output_tokens": 2}}),
        "data: [DONE]\n\n",
    ]
    s.delegate.return_value = StreamingResponse(
        iterator(chunks), media_type="text/event-stream"
    )
    result = await ns["generate_chat_completion"](
        s.request, {**s.body, "stream": True}, s.user
    )
    assert [chunk async for chunk in result.body_iterator] == chunks
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["llm.token_count.total"] == 8


async def test_released_host_stream_cancellation(host):
    s, ns = host
    waiting = asyncio.Event()

    async def source():
        waiting.set()
        await asyncio.Event().wait()
        yield "unreachable"

    s.delegate.return_value = StreamingResponse(source())
    result = await ns["generate_chat_completion"](
        s.request, {**s.body, "stream": True}, s.user
    )

    async def consume():
        return [chunk async for chunk in result.body_iterator]

    task = asyncio.create_task(consume())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["openwebui.outcome"] == "cancelled"


async def test_released_host_failure_is_traced_even_if_host_catches_it(host):
    s, ns = host
    s.delegate.side_effect = RuntimeError("synthetic provider failure")
    result = await ns["generate_chat_completion"](s.request, s.body, s.user)
    assert "error" in result
    (span,) = s.exporter.get_finished_spans()
    assert span.status.description == "model request failed"


async def test_released_host_access_failure_never_calls_provider(host):
    s, ns = host

    async def check(user, model):
        if model["id"] == "synthetic":
            raise PermissionError("synthetic denial")

    ns["check_model_access"].side_effect = check
    result = await ns["generate_chat_completion"](s.request, s.body, s.user)
    assert "error" in result
    s.delegate.assert_not_awaited()
    (span,) = s.exporter.get_finished_spans()
    assert span.status.description == "model request failed"


async def test_released_host_stream_http_error_is_not_empty_success(host):
    from starlette.responses import JSONResponse

    s, ns = host
    s.delegate.return_value = JSONResponse(
        {"error": {"message": "synthetic denial"}}, status_code=429
    )
    response = await ns["generate_chat_completion"](
        s.request, {**s.body, "stream": True}, s.user
    )
    chunks = [x async for x in response.body_iterator]
    assert len(chunks) == 1 and '"error"' in chunks[0]
    assert "finish_reason" not in chunks[0]
    (span,) = s.exporter.get_finished_spans()
    assert span.status.description == "model request failed"
