import asyncio
import copy
import json

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse
from traceai_openwebui._response import Observation


async def invoke(s, **kwargs):
    return await s.pipe.pipe(s.body, s.request, s.user.model_dump(), **kwargs)


def frame(data):
    return "data: " + json.dumps(data) + "\n\n"


async def iterator(chunks):
    for chunk in chunks:
        yield chunk


async def consume(response):
    return [chunk async for chunk in response.body_iterator]


async def test_real_nonstream_call_and_privacy(setup_pipe):
    s = setup_pipe
    original = copy.deepcopy(s.body)
    result = await invoke(s)
    assert result is s.delegate.return_value
    assert s.body == original
    s.delegate.assert_awaited_once()
    args, kwargs = s.delegate.call_args
    assert args[1]["model"] == "synthetic" and args[2] is s.user
    assert "bypass_filter" not in kwargs
    spans = s.exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["fi.span.kind"] == "LLM"
    assert span.attributes["llm.token_count.total"] == 10
    assert span.attributes["openwebui.outcome"] == "completed"
    assert span.start_time < span.end_time
    assert not span.events
    serialized = repr(span.attributes) + repr(span.events)
    for secret in (
        "private-input",
        "private-output",
        "secret@example",
        "synthetic-secret",
        "synthetic-user",
    ):
        assert secret not in serialized


async def test_stream_identity_usage_once_and_context(setup_pipe):
    s = setup_pipe
    chunks = [
        frame(
            {
                "choices": [
                    {"delta": {"content": "private-output"}, "finish_reason": "stop"}
                ]
            }
        ),
        frame({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}),
        frame({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}),
        "data: [DONE]\n\n",
    ]
    contexts = []

    async def source():
        for chunk in chunks:
            contexts.append(trace.get_current_span().get_span_context())
            yield chunk

    s.delegate.return_value = StreamingResponse(
        source(), media_type="text/event-stream"
    )
    with s.provider.get_tracer("test").start_as_current_span("parent") as parent:
        result = await invoke(s)
        assert not s.exporter.get_finished_spans()
        received = []
        async for chunk in result.body_iterator:
            assert trace.get_current_span() is parent
            received.append(chunk)
    spans = s.exporter.get_finished_spans()
    model = spans[0]
    assert received == chunks
    assert model.parent.span_id == parent.get_span_context().span_id
    assert all(ctx.span_id == model.context.span_id for ctx in contexts)
    assert model.attributes["llm.token_count.total"] == 10
    assert model.attributes["openwebui.outcome"] == "completed"


@pytest.mark.parametrize(
    "signal",
    [
        {"error": {"message": "private-error"}},
        {"type": "response.failed", "response": {"error": "private-error"}},
        {"type": "response.incomplete"},
    ],
)
async def test_stream_error_fixed_message(setup_pipe, signal):
    s = setup_pipe
    chunks = [frame(signal), "data: [DONE]\n\n"]
    s.delegate.return_value = StreamingResponse(iterator(chunks))
    assert await consume(await invoke(s)) == chunks
    (span,) = s.exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "model request failed"
    assert "private-error" not in repr(span.attributes) + repr(span.events)


@pytest.mark.parametrize(
    "result",
    [
        {"error": "private-error"},
        JSONResponse({"error": "private-error"}, status_code=429),
    ],
)
async def test_nonstream_error(setup_pipe, result):
    s = setup_pipe
    s.delegate.return_value = result
    response = await invoke(s)
    assert response == (
        json.loads(result.body) if isinstance(result, JSONResponse) else result
    )
    (span,) = s.exporter.get_finished_spans()
    assert span.status.description == "model request failed"


@pytest.mark.parametrize(
    "error", [RuntimeError("private-error"), asyncio.CancelledError()]
)
async def test_dispatch_error_or_cancellation(setup_pipe, error):
    s = setup_pipe
    s.delegate.side_effect = error
    with pytest.raises(type(error)):
        await invoke(s)
    (span,) = s.exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert "private-error" not in str(span.status.description)


async def test_stream_exception_closes_upstream_and_background(setup_pipe):
    s = setup_pipe
    closed = []

    async def source():
        try:
            yield frame({"choices": [{"delta": {"content": "one"}}]})
            raise RuntimeError("private-error")
        finally:
            closed.append("source")

    s.delegate.return_value = StreamingResponse(
        source(), background=BackgroundTask(closed.append, "background")
    )
    with pytest.raises(RuntimeError, match="private-error"):
        await consume(await invoke(s))
    assert closed == ["source", "background"]
    (span,) = s.exporter.get_finished_spans()
    assert span.status.description == "model request failed"


async def test_cancel_during_stream_wait(setup_pipe):
    s = setup_pipe
    waiting = asyncio.Event()
    closed = []

    async def source():
        try:
            waiting.set()
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            closed.append(True)

    s.delegate.return_value = StreamingResponse(source())
    result = await invoke(s)
    task = asyncio.create_task(consume(result))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["openwebui.outcome"] == "cancelled" and closed


async def test_close_between_chunks(setup_pipe):
    s = setup_pipe
    s.delegate.return_value = StreamingResponse(
        iterator(["data: {}\n\n", "data: [DONE]\n\n"])
    )
    result = await invoke(s)
    await anext(result.body_iterator)
    await result.body_iterator.aclose()
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["openwebui.outcome"] == "cancelled"


async def test_incomplete_stream_is_not_success(setup_pipe):
    s = setup_pipe
    s.delegate.return_value = StreamingResponse(iterator(["data: {}\n\n"]))
    await consume(await invoke(s))
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["openwebui.outcome"] == "incomplete"


@pytest.mark.parametrize(
    "chat_id,expected",
    [("saved", True), ("temporary:123", False), ("local:123", False)],
)
async def test_content_optin_and_temporary_privacy(setup_pipe, chat_id, expected):
    s = setup_pipe
    s.pipe.valves.capture_content = True
    s.pipe.valves.max_chars = 9
    await invoke(s, __metadata__={"chat_id": chat_id, "private": "secret"})
    (span,) = s.exporter.get_finished_spans()
    assert ("input.value" in span.attributes) is expected
    if expected:
        assert len(span.attributes["input.value"]) <= 9
        assert span.attributes["output.value"] == "private-o"
    assert "session.id" not in span.attributes


@pytest.mark.parametrize(
    "target",
    [
        {"pipe": True},
        {"owned_by": "arena"},
        {"info": {"base_model_id": "another-pipe"}},
        None,
    ],
)
async def test_invalid_target_cannot_recurse(setup_pipe, target):
    s = setup_pipe
    s.request.app.state.MODELS["synthetic"] = target
    with pytest.raises(ValueError, match="native provider"):
        await invoke(s)
    s.delegate.assert_not_awaited()
    assert not s.exporter.get_finished_spans()


async def test_two_calls_not_one_turn_aggregate(setup_pipe):
    s = setup_pipe
    await invoke(s)
    # A tool loop's next request includes results from the host, not another
    # model operation inside this wrapper. No transcript-reconstructed spans.
    s.body["messages"].append({"role": "tool", "content": "synthetic tool result"})
    await invoke(s)
    spans = s.exporter.get_finished_spans()
    assert len(spans) == 2 and all(x.attributes["fi.span.kind"] == "LLM" for x in spans)
    assert spans[0].end_time <= spans[1].start_time
    assert sum(x.attributes["llm.token_count.total"] for x in spans) == 20


async def test_concurrent_calls_are_isolated(setup_pipe):
    s = setup_pipe
    await asyncio.gather(*(invoke(s) for _ in range(20)))
    spans = s.exporter.get_finished_spans()
    assert len(spans) == len({x.context.trace_id for x in spans}) == 20
    assert all(x.attributes["llm.token_count.total"] == 10 for x in spans)


async def test_export_configuration_fail_open(setup_pipe, monkeypatch, caplog):
    s = setup_pipe

    def fail(key):
        raise ValueError("private-error")

    monkeypatch.setattr(s.pipe._providers, "acquire", fail)
    assert await invoke(s) is s.delegate.return_value
    assert "private-error" not in caplog.text


@pytest.mark.parametrize("version", ["0.11.3", "0.13.0", "1.0"])
async def test_unsupported_host_fails_clearly(setup_pipe, monkeypatch, version):
    monkeypatch.setattr("traceai_openwebui.version", lambda name: version)
    with pytest.raises(RuntimeError, match="requires Open WebUI"):
        await invoke(setup_pipe)


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True, "completion_tokens": -1},
        {"input_tokens": 2**64},
        "malformed",
    ],
)
def test_invalid_usage_not_counted(setup_pipe, usage):
    s = setup_pipe
    span = s.provider.get_tracer("test").start_span("test")
    observation = Observation(span, False, 10)
    observation.payload({"usage": usage})
    observation.finish()
    span.end()
    assert s.exporter.get_finished_spans()[0].attributes == {
        "gen_ai.usage.unavailable": True
    }


@pytest.mark.parametrize("size", [1, 2, 3, 11, 10000])
def test_fragmented_sse_and_responses_usage(setup_pipe, size):
    s = setup_pipe
    span = s.provider.get_tracer("test").start_span("test")
    observation = Observation(span, True, 20)
    data = (
        frame({"type": "response.output_text.delta", "delta": "café"})
        + frame(
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 12, "output_tokens": 4}},
            }
        )
        + "data: [DONE]\n\n"
    ).encode()
    for index in range(0, len(data), size):
        observation.chunk(data[index : index + size])
    observation.finish()
    span.end()
    (out,) = s.exporter.get_finished_spans()
    assert observation.terminal
    assert out.attributes["output.value"] == "café"
    assert out.attributes["llm.token_count.total"] == 16


async def test_cancellation_while_host_handles_chunk(setup_pipe):
    s = setup_pipe
    s.delegate.return_value = StreamingResponse(
        iterator(["data: {}\n\n", "data: [DONE]\n\n"])
    )
    result = await invoke(s)
    waiting = asyncio.Event()

    async def consumer():
        await anext(result.body_iterator)
        waiting.set()
        await asyncio.Event().wait()  # work in the host, outside our iterator

    task = asyncio.create_task(consumer())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["openwebui.outcome"] == "cancelled"
    await result.body_iterator.aclose()
    assert len(s.exporter.get_finished_spans()) == 1


async def test_abandoned_unconsumed_response_is_incomplete(setup_pipe):
    import gc

    s = setup_pipe
    s.delegate.return_value = StreamingResponse(iterator(["data: [DONE]\n\n"]))
    result = await invoke(s)
    del result
    gc.collect()
    (span,) = s.exporter.get_finished_spans()
    assert span.attributes["openwebui.outcome"] == "incomplete"


async def test_malformed_content_does_not_break_provider_response(setup_pipe):
    s = setup_pipe
    s.delegate.return_value = {"output": [{"type": "message", "content": None}]}
    assert await invoke(s) is s.delegate.return_value


def test_oversized_frame_is_bounded_and_next_frame_survives(setup_pipe):
    s = setup_pipe
    span = s.provider.get_tracer("test").start_span("test")
    observation = Observation(span, True, 20)
    observation.chunk('data: {"private": "' + "x" * 1_100_000 + '"}\n\n')
    observation.chunk(
        frame({"usage": {"input_tokens": 8, "output_tokens": 2}}) + "data: [DONE]\n\n"
    )
    observation.finish()
    span.end()
    (result,) = s.exporter.get_finished_spans()
    assert result.attributes["llm.token_count.total"] == 10
    assert not observation._buffer and not observation._data
    assert result.attributes["output.value"] == ""


async def test_preserves_server_system_prompt_bypass_not_access_bypass(setup_pipe):
    s = setup_pipe
    s.request.state.bypass_system_prompt = True
    s.request.state.bypass_filter = True
    await invoke(s)
    assert s.delegate.call_args.kwargs == {"bypass_system_prompt": True}
