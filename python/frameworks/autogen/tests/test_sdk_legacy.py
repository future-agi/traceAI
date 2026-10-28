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
