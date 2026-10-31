import asyncio

import pytest
from agentscope import model
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from .conftest import V2, call_agent, make_agent, msg, response
from .test_instrumentor import spans_by_kind


async def test_stream_protocol_identity_send_throw_and_close(tracing):
    _, _, exporter = tracing
    first, second = response("first"), response("second")
    observed = []
    thrown = ValueError("application error")

    async def generator():
        try:
            value = yield first
            observed.append(value)
            try:
                yield second
            except ValueError as error:
                assert error is thrown
                yield first
        finally:
            observed.append("closed")

    class CustomModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            return generator()

    stream = await CustomModel.__new__(CustomModel)([])
    assert not exporter.get_finished_spans()
    assert await stream.__anext__() is first
    assert not trace.get_current_span().get_span_context().is_valid
    assert await stream.asend("sent-value") is second
    assert await stream.athrow(thrown) is first
    await asyncio.create_task(stream.aclose())
    assert observed == ["sent-value", "closed"]
    spans = spans_by_kind(exporter, "LLM")
    assert len(spans) == 1
    assert spans[0].attributes["traceai.stream.incomplete"] is True
    assert spans[0].status.status_code is StatusCode.OK
    assert not trace.get_current_span().get_span_context().is_valid


async def test_unconsumed_async_generator_not_started(tracing):
    _, _, exporter = tracing
    entered = []

    class CustomModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            entered.append(1)
            yield response()

    stream = CustomModel.__new__(CustomModel)([])
    assert not entered
    await stream.aclose()
    assert not entered
    assert not exporter.get_finished_spans()


async def test_model_stream_error_retains_original_exception(tracing):
    _, _, exporter = tracing
    error = RuntimeError("private-stream-error")

    class CustomModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            yield response("partial")
            raise error

    stream = CustomModel.__new__(CustomModel)([])
    await anext(stream)
    with pytest.raises(RuntimeError) as caught:
        await anext(stream)
    assert caught.value is error
    (span,) = spans_by_kind(exporter, "LLM")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["output.value"] == "partial"
    assert "private-stream-error" not in str(span.events)


async def test_cancellation_retains_original_exception(tracing):
    _, _, exporter = tracing
    entered = asyncio.Event()
    block = asyncio.Event()

    class CustomModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            entered.set()
            await block.wait()

    instance = CustomModel.__new__(CustomModel)
    task = asyncio.create_task(instance([]))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (span,) = spans_by_kind(exporter, "LLM")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"].endswith("CancelledError")
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.skipif(not V2, reason="2.x Agent stream API")
async def test_real_agent_stream_and_cross_task_close(tracing, endpoint, caplog):
    _, _, exporter = tracing
    agent = make_agent(endpoint, stream=True)
    stream = agent.reply_stream(msg(), yield_final_msg=True)
    await anext(stream)
    assert not trace.get_current_span().get_span_context().is_valid
    await asyncio.create_task(stream.aclose())
    (span,) = spans_by_kind(exporter, "AGENT")
    assert span.attributes["traceai.stream.incomplete"] is True
    assert "Failed to detach" not in caplog.text


async def test_uninstrument_before_first_advance(tracing):
    instrumentor, _, exporter = tracing

    class CustomModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            yield response()

    stream = CustomModel.__new__(CustomModel)([])
    instrumentor.uninstrument()
    assert await anext(stream) is not None
    await stream.aclose()
    assert not exporter.get_finished_spans()


@pytest.mark.skipif(not V2, reason="2.x native middleware protocol")
async def test_native_middleware_is_preserved_and_removed_cleanly(tracing, endpoint):
    from agentscope.middleware import MiddlewareBase

    instrumentor, _, exporter = tracing
    seen = []

    class Observer(MiddlewareBase):
        async def on_reply(self, agent, input_kwargs, next_handler):
            seen.append("entered")
            async for item in next_handler(**input_kwargs):
                yield item

    observer = Observer()
    agent = make_agent(endpoint)
    agent._reply_middlewares.append(observer)
    await call_agent(agent)
    assert seen == ["entered"]
    assert len(spans_by_kind(exporter, "AGENT")) == 1
    instrumentor.uninstrument()
    assert agent._reply_middlewares == [observer]
    await call_agent(agent)
    assert seen == ["entered", "entered"]
    assert len(spans_by_kind(exporter, "AGENT")) == 1


async def test_existing_agent_output_text(tracing, endpoint):
    _, _, exporter = tracing
    await call_agent(make_agent(endpoint))
    (span,) = spans_by_kind(exporter, "AGENT")
    assert span.attributes["output.value"] == "hello local"
