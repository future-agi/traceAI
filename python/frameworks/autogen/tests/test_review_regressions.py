"""Real-SDK regressions for privacy and transparent async-generator delegation."""

import asyncio
import json
from importlib.metadata import version

import pytest
from autogen_agentchat.agents import AssistantAgent, SocietyOfMindAgent, UserProxyAgent
from autogen_agentchat.base import Response, TaskResult
from autogen_agentchat.conditions import TextMentionTermination
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
@pytest.mark.parametrize("policy", ["values", "messages", "text"])
@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
async def test_real_agentchat_independent_io_privacy(spans, monkeypatch, team, streaming,
                                                   environment, policy, hide_inputs, hide_outputs):
    exporter, provider, instrumentor = spans
    instrumentor.uninstrument()
    input_flag, output_flag = {
        "values": ("hide_inputs", "hide_outputs"),
        "messages": ("hide_input_messages", "hide_output_messages"),
        "text": ("hide_input_text", "hide_output_text"),
    }[policy]
    if environment:
        monkeypatch.setenv("FI_" + input_flag.upper(), str(hide_inputs).lower())
        monkeypatch.setenv("FI_" + output_flag.upper(), str(hide_outputs).lower())
        config = TraceConfig()
    else:
        config = TraceConfig(**{input_flag: hide_inputs, output_flag: hide_outputs})
    instrumentor.instrument(tracer_provider=provider, config=config)
    message = TextMessage(content="private-input", source="user")
    assistant = AssistantAgent("assistant", model_client=LocalModel())
    if team:
        chat = RoundRobinGroupChat([assistant], max_turns=1,
                                   termination_condition=TextMentionTermination("offline reply"))
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
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("environment", [False, True])
@pytest.mark.parametrize("flag", [None, "hide_inputs", "hide_outputs", "hide_input_messages",
                                 "hide_output_messages", "hide_input_text", "hide_output_text"])
async def test_real_agentchat_error_details_privacy(spans, monkeypatch, team, streaming, environment, flag):
    exporter, provider, instrumentor = spans
    failure = ValueError("private-model-diagnostic")
    message = TextMessage(content="unchanged-task", source="user")
    # 0.4.5 logs background participant errors and returns a partial TaskResult;
    # 0.7.5 propagates them as RuntimeError. Assert BOTH exact native contracts:
    # instrumentation must not introduce a new exception on the historical SDK.
    historical_partial_result = team and version("autogen-agentchat") == "0.4.5"

    async def call():
        assistant = AssistantAgent("assistant", model_client=LocalModel(error=failure))
        if team:
            chat = RoundRobinGroupChat([assistant], max_turns=1)
            if streaming:
                items = [item async for item in chat.run_stream(task=message)]
                return items[-1]
            return await chat.run(task=message)
        if streaming:
            items = [item async for item in assistant.on_messages_stream([message], CancellationToken())]
            return items[-1]
        return await assistant.on_messages([message], CancellationToken())

    async def exercise():
        if historical_partial_result:
            result = await call()
            assert isinstance(result, TaskResult)
            assert result.messages == [message]
            assert result.messages[0] is message
            assert result.stop_reason is None
            outcome = (TaskResult, result.stop_reason)
        else:
            with pytest.raises(Exception) as caught:
                await call()
            # Newer teams wrap model errors; direct agents preserve identity.
            assert "private-model-diagnostic" in str(caught.value)
            outcome = (type(caught.value), caught.value is failure)
        assert message.content == "unchanged-task"
        return outcome

    instrumentor.uninstrument()
    native = await exercise()
    if flag and environment:
        monkeypatch.setenv("FI_" + flag.upper(), "true")
        config = TraceConfig()
    else:
        config = TraceConfig(**({flag: True} if flag else {}))
    instrumentor.instrument(tracer_provider=provider, config=config)
    actual = await exercise()
    assert actual == native
    if not team:
        assert actual == (ValueError, True)
    finished = [s for s in exporter.get_finished_spans() if s.name.startswith("autogen.")]
    assert finished
    errors = [s for s in finished if s.status.status_code == StatusCode.ERROR]
    assert errors
    if historical_partial_result:
        team_spans = [s for s in finished if s.name.startswith("autogen.team.")]
        assert len(team_spans) == (1 if streaming else 2)
        assert all(s.status.status_code == StatusCode.OK for s in team_spans)
        assert len(errors) == 1
        assert errors[0].name == "autogen.agent.on_messages_stream.assistant"
    else:
        assert len(errors) == len(finished)
    for span in errors:
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes["autogen.is_error"] is True
        assert span.attributes["autogen.error.type"] in ("ValueError", "RuntimeError")
        events = [e for e in span.events if e.name == "exception"]
        assert len(events) == 1
        assert events[0].attributes["exception.type"].endswith(span.attributes["autogen.error.type"])
        payload = span.to_json()
        assert ("private-model-diagnostic" in payload) is (flag is None)
        if flag:
            assert span.status.description is None
            assert "autogen.error.message" not in span.attributes
            assert "exception.message" not in events[0].attributes
            assert "exception.stacktrace" not in events[0].attributes
        else:
            assert "private-model-diagnostic" in span.attributes["autogen.error.message"]
            assert "private-model-diagnostic" in span.status.description
            assert "private-model-diagnostic" in events[0].attributes["exception.message"]
        assert span.end_time


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
            finished = [s for s in exporter.get_finished_spans() if s.name.startswith("autogen.")]
            errors = [s for s in finished if s.status.status_code == StatusCode.ERROR]
            if reset_failure:
                assert len(errors) == 1
                cleanup = errors[0]
                logical = next(s for s in finished if s.name == "autogen.agent.on_messages_stream.society")
                assert cleanup.name == logical.name + ".cleanup"
                assert cleanup.parent.span_id == logical.context.span_id
                assert [link.context for link in cleanup.links] == [logical.context]
                assert cleanup.attributes["autogen.error.type"] == "RuntimeError"
                assert cleanup.status.description == "post-response-reset"
                events = [e for e in cleanup.events if e.name == "exception"]
                assert len(events) == 1
                assert events[0].attributes["exception.message"] == str(failure)
                assert "RuntimeError: post-response-reset" in events[0].attributes["exception.stacktrace"]
                assert cleanup.end_time
            else:
                assert not errors
                assert not any(s.name.endswith(".cleanup") for s in finished)
        finally:
            release.set()
            await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["anext", "athrow", "aclose"])
@pytest.mark.parametrize("hide_details", [False, True])
async def test_post_result_cleanup_error_is_linked_and_private(spans, operation, hide_details):
    from types import SimpleNamespace
    from fi_instrumentation import FITracer
    from traceai_autogen._v04_wrapper import wrap_agent_on_messages_stream
    exporter, provider, _ = spans
    config = TraceConfig(hide_outputs=hide_details)
    tracer = provider.get_tracer(__name__)
    failure = RuntimeError("private-cleanup-diagnostic")
    result = Response(chat_message=TextMessage(content="result", source="assistant"))
    cleaned = []

    async def original(self):
        try:
            yield result
        finally:
            cleaned.append(True)
            raise failure

    wrapped = wrap_agent_on_messages_stream(original, FITracer(tracer, config=config), config)
    with tracer.start_as_current_span("consumer") as caller:
        stream = wrapped(SimpleNamespace(name="cleanup-test"))
        assert await anext(stream) is result
        assert not cleaned
        assert trace.get_current_span() is caller
        logical = next(s for s in exporter.get_finished_spans() if s.name.startswith("autogen."))
        assert logical.status.status_code == StatusCode.OK
        with pytest.raises(RuntimeError) as caught:
            if operation == "anext":
                await anext(stream)
            elif operation == "athrow":
                await stream.athrow(ValueError("injection"))
            else:
                await stream.aclose()
        assert caught.value is failure
        assert trace.get_current_span() is caller
        await stream.aclose()
    assert cleaned == [True]
    cleanup, = [s for s in exporter.get_finished_spans() if s.name.endswith(".cleanup")]
    assert cleanup.parent.span_id == logical.context.span_id
    assert [link.context for link in cleanup.links] == [logical.context]
    assert cleanup.attributes["autogen.stream.cleanup"] is True
    assert cleanup.status.status_code == StatusCode.ERROR
    assert cleanup.attributes["autogen.error.type"] == "RuntimeError"
    events = [e for e in cleanup.events if e.name == "exception"]
    assert len(events) == 1
    assert events[0].attributes["exception.type"].endswith("RuntimeError")
    assert ("private-cleanup-diagnostic" in cleanup.to_json()) is not hide_details
    assert cleanup.end_time


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
