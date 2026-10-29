"""Real-SDK regressions for privacy and transparent async-generator delegation."""

import asyncio
import json

import pytest
from autogen_agentchat.agents import AssistantAgent, SocietyOfMindAgent, UserProxyAgent
from autogen_agentchat.base import Response, TaskResult
from autogen_agentchat.messages import TextMessage
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core import CancellationToken
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from .test_sdk_contract import LocalModel, spans


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("environment", [False, True])
@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
async def test_real_agentchat_independent_io_privacy(spans, monkeypatch, team, streaming,
                                                   environment, hide_inputs, hide_outputs):
    exporter, provider, instrumentor = spans
    instrumentor.uninstrument()
    if environment:
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hide_inputs).lower())
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hide_outputs).lower())
        config = TraceConfig()
    else:
        config = TraceConfig(hide_inputs=hide_inputs, hide_outputs=hide_outputs)
    instrumentor.instrument(tracer_provider=provider, config=config)
    message = TextMessage(content="private-input", source="user")
    assistant = AssistantAgent("assistant", model_client=LocalModel())
    if team:
        chat = RoundRobinGroupChat([assistant], max_turns=1)
        if streaming:
            items = [item async for item in chat.run_stream(task=message)]
            result = items[-1]
            assert isinstance(result, TaskResult)
            assert items[0] is message
        else:
            result = await chat.run(task=message)
        assert result.messages[0] is message
        assert result.messages[-1].content == "offline reply"
    else:
        messages = [message]
        if streaming:
            items = [item async for item in assistant.on_messages_stream(messages, CancellationToken())]
            result = items[-1]
        else:
            result = await assistant.on_messages(messages, CancellationToken())
        assert isinstance(result, Response)
        assert result.chat_message.content == "offline reply"
        assert messages == [message]
    assert message.content == "private-input"
    finished = exporter.get_finished_spans()
    assert finished
    payload = json.dumps([json.loads(span.to_json()) for span in finished])
    assert ("private-input" in payload) is not hide_inputs
    assert ("offline reply" in payload) is not hide_outputs
    assert all(span.status.status_code == StatusCode.OK for span in finished if span.name.startswith("autogen."))


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_failure", [False, True])
async def test_real_society_delivers_response_before_post_yield_reset(spans, reset_failure):
    exporter, provider, _ = spans
    entered, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("post-response-reset")

    class ResetTeam(RoundRobinGroupChat):
        async def reset(self, cancellation_token=None):
            entered.set()
            await release.wait()
            if reset_failure:
                raise failure
            await super().reset()

    inner = AssistantAgent("inner", model_client=LocalModel())
    chat = ResetTeam([inner], max_turns=1)
    assistant = SocietyOfMindAgent("society", team=chat, model_client=LocalModel())
    with provider.get_tracer(__name__).start_as_current_span("consumer") as caller:
        stream = assistant.on_messages_stream([TextMessage(content="hello", source="user")], CancellationToken())
        try:
            while True:
                item = await asyncio.wait_for(anext(stream), timeout=2)
                assert trace.get_current_span() is caller
                if isinstance(item, Response):
                    assert item.chat_message.content == "offline reply"
                    break
            assert not entered.is_set()  # no lookahead into reset
            assert any(s.name == "autogen.agent.on_messages_stream.society" for s in exporter.get_finished_spans())
            resume = asyncio.create_task(anext(stream))
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not resume.done()
            release.set()
            if reset_failure:
                with pytest.raises(RuntimeError) as caught:
                    await resume
                assert caught.value is failure
            else:
                with pytest.raises(StopAsyncIteration):
                    await resume
            assert trace.get_current_span() is caller
        finally:
            release.set()
            await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["athrow", "asend", "aclose"])
async def test_real_userproxy_generator_protocol_matches_native(spans, operation):
    exporter, provider, instrumentor = spans

    async def exercise(instrumented):
        instrumentor.uninstrument()
        if instrumented:
            instrumentor.instrument(tracer_provider=provider)
        inputs = []
        async def input_func(prompt, cancellation_token):
            inputs.append(prompt)
            return "user reply"
        agent = UserProxyAgent("user", input_func=input_func)
        stream = agent.on_messages_stream([], CancellationToken())
        with provider.get_tracer(__name__).start_as_current_span("consumer") as caller:
            first = await anext(stream)
            assert trace.get_current_span() is caller
            try:
                if operation == "athrow":
                    await stream.athrow(ValueError("synthetic-stop"))
                elif operation == "asend":
                    result = await stream.asend("consumer-sent-value")
                    assert isinstance(result, Response)
                    assert result.chat_message.content == "user reply"
                    with pytest.raises(StopAsyncIteration):
                        await anext(stream)
                else:
                    await stream.aclose()
            except Exception as error:
                outcome = (type(error).__name__, str(error))
            else:
                outcome = None
            finally:
                await stream.aclose()
            assert trace.get_current_span() is caller
        return type(first).__name__, outcome, inputs

    native = await exercise(False)
    actual = await exercise(True)
    assert actual == native
    if operation == "athrow":
        assert actual[1] == ("RuntimeError", "Failed to get user input: synthetic-stop")
    traced = [s for s in exporter.get_finished_spans() if s.name.startswith("autogen.")]
    assert len(traced) == 1
    assert traced[0].end_time
    assert traced[0].status.status_code == (StatusCode.ERROR if operation == "athrow" else StatusCode.OK if operation == "asend" else StatusCode.UNSET)


@pytest.mark.asyncio
async def test_stream_protocol_restores_latest_producer_context_and_caller(spans):
    # In addition to real UserProxyAgent above, pin the general delegation
    # contract for generators that consume sent values and catch injected errors.
    from types import SimpleNamespace
    from fi_instrumentation import FITracer
    from traceai_autogen._v04_wrapper import wrap_agent_on_messages_stream
    exporter, provider, _ = spans
    tracer = provider.get_tracer(__name__)
    first, sent, second, third = object(), object(), object(), object()
    failure = ValueError("injected")
    observed = []

    async def original(self, *args, **kwargs):
        with tracer.start_as_current_span("producer-child") as child:
            try:
                value = yield first
                assert value is sent
                assert trace.get_current_span() is child
                with tracer.start_as_current_span("after-send"):
                    pass
                try:
                    yield second
                except ValueError as caught:
                    assert caught is failure
                    assert trace.get_current_span() is child
                    with tracer.start_as_current_span("after-throw"):
                        pass
                    yield third
            finally:
                assert trace.get_current_span() is child
                with tracer.start_as_current_span("cleanup"):
                    pass
                observed.append("closed")

    wrapped = wrap_agent_on_messages_stream(original, FITracer(tracer))
    with tracer.start_as_current_span("consumer") as caller:
        stream = wrapped(SimpleNamespace(name="test"))
        assert await anext(stream) is first
        assert trace.get_current_span() is caller
        with tracer.start_as_current_span("consumer-sibling"):
            pass
        assert await stream.asend(sent) is second
        assert trace.get_current_span() is caller
        assert await stream.athrow(failure) is third
        assert trace.get_current_span() is caller
        await stream.aclose()
        assert trace.get_current_span() is caller
    assert observed == ["closed"]
    by_name = {s.name: s for s in exporter.get_finished_spans()}
    for name in ("after-send", "after-throw", "cleanup"):
        assert by_name[name].parent.span_id == by_name["producer-child"].context.span_id
    assert by_name["consumer-sibling"].parent.span_id == by_name["consumer"].context.span_id
    assert by_name["producer-child"].parent.span_id == by_name["autogen.agent.on_messages_stream.test"].context.span_id
    assert by_name["autogen.agent.on_messages_stream.test"].attributes["autogen.stream.closed_early"] is True


@pytest.mark.asyncio
async def test_stream_athrow_generatorexit_is_forwarded_not_converted_to_aclose(spans):
    from types import SimpleNamespace
    from fi_instrumentation import FITracer
    from traceai_autogen._v04_wrapper import wrap_agent_on_messages_stream
    _, provider, _ = spans
    first, caught_item = object(), object()
    error = GeneratorExit("injected-not-close")
    observed = []

    async def original(self):
        try:
            yield first
        except GeneratorExit as caught:
            observed.append(caught)
            yield caught_item

    for method in (original, wrap_agent_on_messages_stream(original, FITracer(provider.get_tracer(__name__)))):
        stream = method(SimpleNamespace(name="test"))
        assert await anext(stream) is first
        assert await stream.athrow(error) is caught_item
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await stream.aclose()
    assert observed == [error, error]
