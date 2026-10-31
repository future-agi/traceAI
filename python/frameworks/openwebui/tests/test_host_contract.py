"""Released host dispatch and streaming contract, not a full application test."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from starlette.responses import JSONResponse, Response, StreamingResponse
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


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "result",
    [
        JSONResponse({"error": {"message": "synthetic denial"}}, status_code=429),
        JSONResponse({"detail": "synthetic denial"}, status_code=403),
        Response(b"synthetic unavailable", status_code=503),
        Response(b"not a completion", status_code=200),
        JSONResponse(["not a completion"], status_code=200),
        JSONResponse({}, status_code=200),
        JSONResponse({"choices": [{"delta": {"content": "not a completion"}}]}),
    ],
)
async def test_released_host_http_error_is_not_empty_success(host, stream, result):
    s, ns = host
    s.delegate.return_value = result
    response = await ns["generate_chat_completion"](
        s.request, {**s.body, "stream": stream}, s.user
    )
    if stream:
        chunks = [x async for x in response.body_iterator]
        assert len(chunks) == 1 and '"error"' in chunks[0]
        assert "finish_reason" not in chunks[0]
    else:
        assert isinstance(response, dict) and response["error"]
        assert "choices" not in response
    (span,) = s.exporter.get_finished_spans()
    assert span.status.description == "model request failed"


async def test_released_host_nonstream_json_response_preserves_completion(host):
    s, ns = host
    completion = s.delegate.return_value
    s.delegate.return_value = JSONResponse(completion)
    response = await ns["generate_chat_completion"](s.request, s.body, s.user)
    assert response == completion
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["llm.token_count.total"] == 10


@pytest.mark.parametrize("stream", [False, True])
async def test_released_host_native_ollama_is_rejected_before_dispatch(host, stream):
    s, ns = host
    s.request.app.state.MODELS["synthetic"]["owned_by"] = "ollama"
    native = ns["generate_ollama_chat_completion"] = AsyncMock()
    response = await ns["generate_chat_completion"](
        s.request, {**s.body, "stream": stream}, s.user
    )
    if stream:
        chunks = [x async for x in response.body_iterator]
        assert len(chunks) == 1 and "Native Ollama" in chunks[0]
        assert '"error"' in chunks[0] and "finish_reason" not in chunks[0]
    else:
        assert "Native Ollama" in str(response["error"])
    native.assert_not_awaited()
    s.delegate.assert_not_awaited()
    assert not s.exporter.get_finished_spans()


async def test_released_host_shielded_cancellation_cleanup(host):
    s, ns = host
    s.delegate.return_value = StreamingResponse(
        iterator(["data: {}\n\n", "data: [DONE]\n\n"])
    )
    response = await ns["generate_chat_completion"](
        s.request, {**s.body, "stream": True}, s.user
    )
    waiting = asyncio.Event()

    async def consume():
        try:
            await anext(response.body_iterator)
            waiting.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Released middleware closes the outer dispatcher under shield BEFORE
            # the consuming task becomes cancelled. Its inner stream is finalized.
            await asyncio.shield(response.body_iterator.aclose())
            raise

    task = asyncio.create_task(consume())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["openwebui.outcome"] == "cancelled"
    assert span.status.description == "model request cancelled"
