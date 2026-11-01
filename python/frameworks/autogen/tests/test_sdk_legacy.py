"""Real legacy SDK contracts; pyautogen 0.2 excludes Python 3.13+."""

import json
import inspect
import pytest
from autogen import ConversableAgent
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode
from .test_sdk_contract import spans


def legacy(name, reply="legacy reply", **kwargs):
    return ConversableAgent(name, llm_config=False, code_execution_config=False,
                            human_input_mode="NEVER", default_auto_reply=reply, **kwargs)


def test_real_legacy_reply_chat_and_function(spans):
    assistant, user = legacy("assistant"), legacy("user", max_consecutive_auto_reply=0)
    response = assistant.generate_reply(messages=[{"role": "user", "content": "hello"}])
    assert response == "legacy reply"
    chat = user.initiate_chat(assistant, message="hello", max_turns=1, silent=True)
    returns_result = "max_turns" in inspect.signature(ConversableAgent.initiate_chat).parameters
    if returns_result:
        assert [m["content"] for m in chat.chat_history] == ["hello", "legacy reply"]
    else:
        assert chat is None  # pyautogen 0.2.0 returns None, not ChatResult
    assert [m["content"] for m in user.chat_messages[assistant]] == ["hello", "legacy reply"]
    assistant.register_function({"add": lambda x, y: x + y})
    result = assistant.execute_function({"name": "add", "arguments": '{"x": 2, "y": 3}'})
    assert result == (True, {"name": "add", "role": "function", "content": "5"})
    finished = spans[0].get_finished_spans()
    expected_replies = 2 if returns_result else 3  # old SDK asks user to terminate
    assert [s.name for s in finished] == ["autogen.generate_reply"] * expected_replies + ["autogen.initiate_chat", "autogen.execute_function"]
    assert json.loads(finished[expected_replies].attributes["input.value"]) == "hello"
    for span in finished[1:expected_replies]:
        assert span.parent.span_id == finished[expected_replies].context.span_id
    for span in finished:
        assert span.attributes["gen_ai.span.kind"] in ("AGENT", "TOOL")
        assert span.status.status_code == StatusCode.OK
        assert "input.value" in span.attributes and "output.value" in span.attributes
    assert json.loads(finished[-1].attributes["output.value"]) == [True, result[1]]


@pytest.mark.asyncio
async def test_real_legacy_async_reply_chat_and_function(spans):
    assistant, user = legacy("assistant"), legacy("user", max_consecutive_auto_reply=0)
    assert await assistant.a_generate_reply(messages=[{"role": "user", "content": "hello"}]) == "legacy reply"
    chat = await user.a_initiate_chat(assistant, message="hello", max_turns=1, silent=True)
    returns_result = "max_turns" in inspect.signature(ConversableAgent.initiate_chat).parameters
    if returns_result:
        assert [m["content"] for m in chat.chat_history] == ["hello", "legacy reply"]
    else:
        assert chat is None  # pyautogen 0.2.0 returns None, not ChatResult
    assert [m["content"] for m in user.chat_messages[assistant]] == ["hello", "legacy reply"]
    async def add(x, y):
        return x + y
    assistant.register_function({"add": add})
    result = await assistant.a_execute_function({"name": "add", "arguments": '{"x": 2, "y": 3}'})
    assert result == (True, {"name": "add", "role": "function", "content": "5"})
    finished = spans[0].get_finished_spans()
    expected_replies = 2 if returns_result else 3
    assert [s.name for s in finished] == ["autogen.a_generate_reply"] * expected_replies + ["autogen.a_initiate_chat", "autogen.a_execute_function"]
    assert json.loads(finished[expected_replies].attributes["input.value"]) == "hello"
    for span in finished[1:expected_replies]:
        assert span.parent.span_id == finished[expected_replies].context.span_id
    assert all(s.status.status_code == StatusCode.OK for s in finished)


def test_real_legacy_error_identity_and_span(spans):
    exporter, provider, instrumentor = spans
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider)
    assistant = legacy("assistant")
    failure = ValueError("private-error")
    def fail(recipient, messages, sender, config):
        raise failure
    assistant.register_reply([None], fail, position=0)
    with pytest.raises(ValueError) as caught:
        assistant.generate_reply(messages=[{"role": "user", "content": "private-input"}])
    assert caught.value is failure
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert len(span.events) == 1
    assert span.events[0].attributes["exception.message"] == "private-error"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["generate_reply", "a_generate_reply", "initiate_chat", "a_initiate_chat"])
@pytest.mark.parametrize("flag", [None, "hide_inputs", "hide_outputs", "hide_input_messages",
                                  "hide_output_messages", "hide_input_text", "hide_output_text"])
@pytest.mark.parametrize("environment", [False, True])
async def test_real_legacy_raised_error_privacy(spans, operation, flag, environment, monkeypatch):
    exporter, provider, instrumentor = spans
    instrumentor.uninstrument()
    failure = ValueError("private-legacy-raised-diagnostic")

    def fail(*args, **kwargs):
        raise failure

    async def async_fail(*args, **kwargs):
        raise failure

    async def call():
        assistant = legacy("assistant")
        assistant.register_reply([ConversableAgent, None],
                                 async_fail if operation.startswith("a_") else fail, position=0)
        if operation.endswith("initiate_chat"):
            target = legacy("user", max_consecutive_auto_reply=0)
            args = (assistant,)
            kwargs = dict(message="ordinary-task", max_turns=1, silent=True)
        else:
            target, args = assistant, ()
            kwargs = dict(messages=[{"role": "user", "content": "ordinary-task"}])
        with pytest.raises(ValueError) as caught:
            if operation.startswith("a_"):
                await getattr(target, operation)(*args, **kwargs)
            else:
                getattr(target, operation)(*args, **kwargs)
        assert caught.value is failure

    # Run the same real SDK boundary natively, then instrument it. No SDK method
    # is replaced and the public reply extension raises the identical exception.
    await call()
    assert not exporter.get_finished_spans()
    if flag and environment:
        monkeypatch.setenv("FI_" + flag.upper(), "true")
        config = TraceConfig()
    else:
        config = TraceConfig(**({flag: True} if flag else {}))
    instrumentor.instrument(tracer_provider=provider, config=config)
    from opentelemetry import trace
    with provider.get_tracer(__name__).start_as_current_span("caller") as caller:
        await call()
        assert trace.get_current_span() is caller
        finished = exporter.get_finished_spans()
        assert len(finished) == (2 if operation.endswith("initiate_chat") else 1)
        assert all(span.name.startswith("autogen.") for span in finished)
        assert finished[-1].parent.span_id == caller.get_span_context().span_id
        if len(finished) == 2:
            assert finished[0].parent.span_id == finished[1].context.span_id
        for span in finished:
            assert span.status.status_code == StatusCode.ERROR
            event, = span.events
            assert event.name == "exception"
            assert event.attributes["exception.type"] == "ValueError"
            if flag:
                assert span.status.description is None
                assert "exception.message" not in event.attributes
                assert "exception.stacktrace" not in event.attributes
                assert "private-legacy-raised-diagnostic" not in span.to_json()
            else:
                assert "private-legacy-raised-diagnostic" in span.status.description
                assert event.attributes["exception.message"] == str(failure)
                assert "private-legacy-raised-diagnostic" in event.attributes["exception.stacktrace"]


def test_real_legacy_content_privacy(spans):
    exporter, provider, instrumentor = spans
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    assistant = legacy("assistant", reply="private-output")
    assert assistant.generate_reply(messages=[{"role": "user", "content": "private-input"}]) == "private-output"
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK
    assert "private-input" not in span.to_json()
    assert "private-output" not in span.to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure_kind", ["missing", "invalid", "throwing"])
@pytest.mark.parametrize("hidden", [False, True])
async def test_real_legacy_failed_tool_is_error_and_preserves_tuple(spans, asynchronous, failure_kind, hidden):
    exporter, provider, instrumentor = spans
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_outputs=hidden))
    assistant = legacy("assistant")
    def fail(value):
        raise ValueError("private-tool-diagnostic")
    async def async_fail(value):
        return fail(value)
    assistant.register_function({"fail": async_fail if asynchronous else fail})
    call = {"name": "absent" if failure_kind == "missing" else "fail",
            "arguments": "not-json-private" if failure_kind == "invalid" else '{"value": 1}'}
    # Obtain the SDK's exact result with the same real hook uninstrumented.
    instrumentor.uninstrument()
    original_call = dict(call)
    expected = await assistant.a_execute_function(call) if asynchronous else assistant.execute_function(call)
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_outputs=hidden))
    result = await assistant.a_execute_function(call) if asynchronous else assistant.execute_function(call)
    assert result == expected
    assert result[0] is False
    assert call == original_call
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["autogen.is_error"] is True
    assert len(span.events) == 1
    assert span.events[0].name == "exception"
    if hidden:
        assert result[1]["content"] not in span.to_json()
        assert span.status.description is None
    else:
        assert span.status.description == result[1]["content"]
        assert span.events[0].attributes["exception.message"] == result[1]["content"]
        assert json.loads(span.attributes["output.value"]) == [False, result[1]]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
async def test_real_legacy_sender_history_observed_without_injecting_messages(spans, asynchronous, hidden):
    exporter, provider, instrumentor = spans
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=hidden))
    assistant, peer = legacy("assistant"), legacy("peer")
    assistant.receive("private-sender-input", sender=peer, request_reply=False, silent=True)
    before = [dict(message) for message in assistant.chat_messages[peer]]
    if asynchronous:
        result = await assistant.a_generate_reply(sender=peer)
    else:
        result = assistant.generate_reply(messages=None, sender=peer)
    assert result == "legacy reply"
    assert assistant.chat_messages[peer] == before
    span, = exporter.get_finished_spans()
    if hidden:
        assert "private-sender-input" not in span.to_json()
    else:
        assert json.loads(span.attributes["input.value"]) == before
    assert json.loads(span.attributes["output.value"]) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
async def test_real_legacy_chat_output_from_result_or_old_history(spans, asynchronous, hidden):
    exporter, provider, instrumentor = spans
    instrumentor.uninstrument()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_outputs=hidden))
    assistant = legacy("assistant", reply="private-final-reply")
    user = legacy("user", max_consecutive_auto_reply=0)
    if asynchronous:
        result = await user.a_initiate_chat(assistant, message="hello", max_turns=1, silent=True)
    else:
        result = user.initiate_chat(assistant, message="hello", max_turns=1, silent=True)
    if "max_turns" not in inspect.signature(ConversableAgent.initiate_chat).parameters:
        assert result is None
    else:
        assert result.chat_history == user.chat_messages[assistant]
    assert user.chat_messages[assistant][-1]["content"] == "private-final-reply"
    chat_span, = [s for s in exporter.get_finished_spans() if s.name.endswith("initiate_chat")]
    if hidden:
        assert "private-final-reply" not in chat_span.to_json()
    else:
        assert json.loads(chat_span.attributes["output.value"]) == "private-final-reply"
