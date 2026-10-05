"""Unit tests: a fake LangChain callback sequence, no MCP server and no LLM.

``Run`` replays the callback tree mcp-use 1.7.1 produces for one agent run
(recorded from MCPAgent.run on the fake LLM and server): a LangGraph root
chain, a ``model`` node chain holding each chat-model run, and a ``tools``
node chain holding each tool run. Spans go to an InMemorySpanExporter.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import pytest
from _mcp_use_support import (
    AGENT,
    ANSWER,
    ARG,
    CONTENT_MARKERS,
    ENV_SECRET,
    ENV_SECRET_NAME,
    EXPLICIT_SECRET,
    LLM_KEY,
    LLM_SPAN,
    MODEL,
    PROMPT,
    attrs,
    new_provider,
    only,
    parent_id,
    spans_named,
    wire,
)
from fi_instrumentation import (
    REDACTED_VALUE,
    TraceConfig,
    suppress_tracing,
    using_metadata,
    using_session,
    using_tags,
    using_user,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry import trace as trace_api
from opentelemetry.trace import StatusCode

import traceai_mcp_use._callback as callback_module
from traceai_mcp_use import AGENT_SPAN_NAME, FutureAGICallback
from traceai_mcp_use._text import (
    MAX_ERROR_BYTES,
    MAX_NAME_BYTES,
    MAX_STACKTRACE_BYTES,
    MAX_VALUE_BYTES,
)

TOOL_SPAN = "execute_tool add"
METADATA = {
    "ls_provider": "fake",
    "ls_model_name": MODEL,
    "ls_model_type": "chat",
    "ls_temperature": 0.2,
    "ls_max_tokens": 64,
}


def _result(message: AIMessage) -> LLMResult:
    message = message.model_copy(
        update={
            "usage_metadata": {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "input_token_details": {"cache_read": 3},
            },
            "response_metadata": {
                "model_name": MODEL,
                "finish_reason": "tool_calls" if message.tool_calls else "stop",
            },
        }
    )
    return LLMResult(generations=[[ChatGeneration(message=message)]])


class Run:
    """One agent run, driven callback by callback like LangGraph does."""

    def __init__(self, handler: FutureAGICallback, query: str = PROMPT) -> None:
        self.handler = handler
        self.root = uuid4()
        self.history: List[Any] = [HumanMessage(content=query)]
        handler.on_chain_start(
            {}, {"messages": list(self.history)}, run_id=self.root, name="LangGraph"
        )

    def node(self, name: str) -> UUID:
        run_id = uuid4()
        self.handler.on_chain_start(
            None, {"messages": list(self.history)}, run_id=run_id, parent_run_id=self.root, name=name
        )
        return run_id

    def end_node(self, run_id: UUID) -> None:
        self.handler.on_chain_end({"messages": []}, run_id=run_id, parent_run_id=self.root)

    def llm(self, reply: AIMessage, system: str = "You are an MCP agent.") -> UUID:
        node = self.node("model")
        run_id = uuid4()
        self.handler.on_chat_model_start(
            {"name": "ChatFake", "repr": "ChatFake(api_key='{0}')".format(LLM_KEY)},
            [[SystemMessage(content=system)] + list(self.history)],
            run_id=run_id,
            parent_run_id=node,
            invocation_params={"model_name": MODEL, "api_key": LLM_KEY},
            metadata=dict(METADATA),
        )
        self.handler.on_llm_end(_result(reply), run_id=run_id, parent_run_id=node)
        self.end_node(node)
        self.history.append(reply)
        return run_id

    def start_tool(self, name: str, args: Dict[str, Any], call_id: str = "call-1"):
        node = self.node("tools")
        run_id = uuid4()
        self.handler.on_tool_start(
            {"name": name, "description": "A tool."},
            str(args),
            run_id=run_id,
            parent_run_id=node,
            inputs=dict(args),
            tool_call_id=call_id,
            name=name,
        )
        return node, run_id

    def tool(self, name: str, args: Dict[str, Any], result: str, call_id: str = "call-1") -> None:
        node, run_id = self.start_tool(name, args, call_id)
        output = ToolMessage(content=result, name=name, tool_call_id=call_id)
        self.handler.on_tool_end(output, run_id=run_id, parent_run_id=node)
        self.end_node(node)
        self.history.append(output)

    def finish(self, text: str = ANSWER) -> None:
        self.handler.on_chain_end(
            {"messages": self.history + [AIMessage(content=text)]}, run_id=self.root
        )


def _journey(handler: FutureAGICallback, query: str = PROMPT, args=None, result="SUM-RESULT-5"):
    run = Run(handler, query)
    args = args if args is not None else {"a": 2, "b": 3, "note": ARG}
    run.llm(
        AIMessage(
            content="", tool_calls=[{"name": "add", "args": args, "id": "call-1", "type": "tool_call"}]
        )
    )
    run.tool("add", args, result)
    run.llm(AIMessage(content=ANSWER))
    run.finish()
    return run


def _handler(**kwargs: Any):
    exporter, provider = new_provider()
    return exporter, FutureAGICallback(tracer_provider=provider, **kwargs)


def test_three_span_kinds_from_a_fake_callback_sequence():
    exporter, handler = _handler()
    _journey(handler)
    spans = exporter.get_finished_spans()

    # The model/tools/middleware node chains are not spans.
    assert sorted(span.name for span in spans) == sorted([AGENT, LLM_SPAN, LLM_SPAN, TOOL_SPAN])
    assert AGENT_SPAN_NAME == AGENT
    agent = only(spans, AGENT)
    tool = only(spans, TOOL_SPAN)
    first_llm, second_llm = spans_named(spans, LLM_SPAN)
    assert parent_id(agent) is None
    # Parent = nearest traced ancestor in the callback tree: the agent.
    for child in (first_llm, tool, second_llm):
        assert parent_id(child) == agent.context.span_id
        assert child.context.trace_id == agent.context.trace_id

    assert attrs(agent) == {
        "gen_ai.span.kind": "AGENT",
        "gen_ai.operation.name": "invoke_agent",
        "mcp_use.agent.llm_call_count": 2,
        "mcp_use.agent.tool_call_count": 1,
        "mcp_use.agent.tool_error_count": 0,
    }
    assert attrs(first_llm) == {
        "gen_ai.span.kind": "LLM",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": MODEL,
        "gen_ai.provider.name": "fake",
        "gen_ai.request.temperature": 0.2,
        "gen_ai.request.max_tokens": 64,
        "gen_ai.response.model": MODEL,
        "gen_ai.response.finish_reasons": ("tool_calls",),
        "gen_ai.usage.input_tokens": 11,
        "gen_ai.usage.output_tokens": 7,
        "gen_ai.usage.total_tokens": 18,
        "gen_ai.usage.cache_read_tokens": 3,
        "mcp_use.llm.input_message_count": 2,
        "mcp_use.llm.tool_call_count": 1,
    }
    assert attrs(second_llm)["mcp_use.llm.input_message_count"] == 4
    assert attrs(second_llm)["gen_ai.response.finish_reasons"] == ("stop",)
    assert attrs(tool) == {
        "gen_ai.span.kind": "TOOL",
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "add",
        "gen_ai.tool.call.id": "call-1",
    }
    for span in spans:
        assert span.status.status_code is StatusCode.OK
        assert span.events == ()


def test_content_is_off_by_default():
    exporter, handler = _handler()
    _journey(handler)
    text = wire(exporter.get_finished_spans())
    for marker in CONTENT_MARKERS + (LLM_KEY, "You are an MCP agent"):
        assert marker not in text, marker
    for span in exporter.get_finished_spans():
        for key in attrs(span):
            assert not key.startswith(("input.", "output.", "gen_ai.input.", "gen_ai.output."))
            assert key not in ("gen_ai.tool.call.arguments", "gen_ai.tool.call.result")


def test_capture_content_records_prompt_messages_arguments_and_results():
    exporter, handler = _handler(capture_content=True)
    _journey(handler)
    spans = exporter.get_finished_spans()
    agent = attrs(only(spans, AGENT))
    first_llm, second_llm = (attrs(span) for span in spans_named(spans, LLM_SPAN))
    tool = attrs(only(spans, TOOL_SPAN))
    arguments = json.dumps({"a": 2, "b": 3, "note": ARG})

    assert agent["input.value"] == PROMPT
    assert agent["output.value"] == ANSWER
    assert first_llm["gen_ai.input.messages.0.message.role"] == "system"
    assert first_llm["gen_ai.input.messages.0.message.content"] == "You are an MCP agent."
    assert first_llm["gen_ai.input.messages.1.message.role"] == "user"
    assert first_llm["gen_ai.input.messages.1.message.content"] == PROMPT
    assert first_llm["input.value"] == PROMPT
    call = "gen_ai.output.messages.0.message.tool_calls.0.tool_call."
    assert first_llm["gen_ai.output.messages.0.message.role"] == "assistant"
    assert first_llm[call + "function.name"] == "add"
    assert first_llm[call + "id"] == "call-1"
    assert first_llm[call + "function.arguments"] == arguments
    assert "output.value" not in first_llm  # a tool-call-only reply has no text
    assert second_llm["gen_ai.input.messages.3.message.role"] == "tool"
    assert second_llm["gen_ai.input.messages.3.message.content"] == "SUM-RESULT-5"
    assert second_llm["output.value"] == ANSWER
    assert tool["gen_ai.tool.call.arguments"] == arguments
    assert tool["input.value"] == arguments
    assert tool["gen_ai.tool.call.result"] == "SUM-RESULT-5"
    assert tool["output.value"] == "SUM-RESULT-5"
    # The serialized model and invocation params are never read for content.
    assert LLM_KEY not in wire(spans)


def test_hide_inputs_drops_every_input_including_tool_arguments():
    exporter, handler = _handler(capture_content=True, config=TraceConfig(hide_inputs=True))
    _journey(handler)
    spans = exporter.get_finished_spans()
    text = wire(spans)
    assert PROMPT not in text and ARG not in text
    for span in spans:
        values = attrs(span)
        assert values.get("input.value", REDACTED_VALUE) == REDACTED_VALUE
        assert not any(key.startswith("gen_ai.input.messages") for key in values)
        assert not any(key.endswith("function.arguments") for key in values)
        assert "gen_ai.tool.call.arguments" not in values
    # Outputs stay.
    assert attrs(only(spans, AGENT))["output.value"] == ANSWER
    assert attrs(only(spans, TOOL_SPAN))["gen_ai.tool.call.result"] == "SUM-RESULT-5"


def test_hide_outputs_drops_every_output():
    exporter, handler = _handler(capture_content=True, config=TraceConfig(hide_outputs=True))
    _journey(handler)
    spans = exporter.get_finished_spans()
    text = wire(spans)
    assert ANSWER not in text and "SUM-RESULT" not in text
    for span in spans:
        values = attrs(span)
        assert values.get("output.value", REDACTED_VALUE) == REDACTED_VALUE
        assert not any(key.startswith("gen_ai.output.messages") for key in values)
        assert "gen_ai.tool.call.result" not in values
    assert attrs(only(spans, AGENT))["input.value"] == PROMPT


def test_hide_flags_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    exporter, handler = _handler(capture_content=True)
    _journey(handler)
    text = wire(exporter.get_finished_spans())
    for marker in CONTENT_MARKERS:
        assert marker not in text, marker


def _failing_tool(handler: FutureAGICallback, error: BaseException, query: str = PROMPT):
    run = Run(handler, query)
    args = {"reason": ARG + " jane.doe@example.com"}
    run.llm(
        AIMessage(content="", tool_calls=[{"name": "fail", "args": args, "id": "c", "type": "tool_call"}])
    )
    node, tool = run.start_tool("fail", args, "c")
    try:
        raise error
    except BaseException as raised:  # give it a traceback
        handler.on_tool_error(raised, run_id=tool, parent_run_id=node)
    run.end_node(node)
    run.llm(AIMessage(content=ANSWER))
    run.finish()


def test_tool_error_records_type_only_by_default():
    exporter, handler = _handler()
    _failing_tool(handler, ValueError("tool failed because " + ARG))
    spans = exporter.get_finished_spans()
    tool = only(spans, "execute_tool fail")
    assert tool.status.status_code is StatusCode.ERROR
    assert tool.status.description == "ValueError"
    (event,) = tool.events
    assert event.name == "exception"
    assert dict(event.attributes) == {"exception.type": "ValueError"}
    agent = only(spans, AGENT)
    # The agent recovered (mcp-use returns tool errors to the LLM).
    assert agent.status.status_code is StatusCode.OK
    assert attrs(agent)["mcp_use.agent.tool_error_count"] == 1
    assert "tool failed because" not in wire(spans)


def test_tool_error_text_with_capture_and_hide_inputs_scrubs_the_arguments():
    exporter, handler = _handler(capture_content=True, config=TraceConfig(hide_inputs=True))
    error = ValueError("tool failed because " + ARG + " jane.doe@example.com")
    _failing_tool(handler, error)
    tool = only(exporter.get_finished_spans(), "execute_tool fail")
    (event,) = tool.events
    for text in (tool.status.description, event.attributes["exception.message"], event.attributes["exception.stacktrace"]):
        assert "tool failed because" in text
        assert ARG not in text, text
        assert "jane.doe" not in text, text
        assert REDACTED_VALUE in text


def test_pii_redaction_covers_attributes_status_and_exception_event():
    exporter, handler = _handler(capture_content=True, config=TraceConfig(pii_redaction=True))
    _failing_tool(handler, ValueError("mail jane.doe@example.com failed"), query="ask jane.doe@example.com")
    spans = exporter.get_finished_spans()
    assert "jane.doe@example.com" not in wire(spans)
    assert attrs(only(spans, AGENT))["input.value"] == "ask <EMAIL_ADDRESS>"
    tool = only(spans, "execute_tool fail")
    assert tool.status.description == "ValueError: mail <EMAIL_ADDRESS> failed"
    assert tool.events[0].attributes["exception.message"] == "mail <EMAIL_ADDRESS> failed"


def test_secrets_are_removed_from_attributes_status_and_events(monkeypatch):
    monkeypatch.setenv(ENV_SECRET_NAME, ENV_SECRET)
    exporter, handler = _handler(capture_content=True, redact=[EXPLICIT_SECRET])
    leaky = "keys {0} {1} {2} Authorization: Bearer abcdefghijklmnop".format(
        ENV_SECRET, EXPLICIT_SECRET, LLM_KEY
    )
    run = Run(handler, query=leaky)
    run.llm(AIMessage(content=leaky))
    node, tool = run.start_tool("echo", {"text": leaky})
    try:
        raise RuntimeError(leaky)
    except RuntimeError as error:
        handler.on_tool_error(error, run_id=tool, parent_run_id=node)
    run.end_node(node)
    run.finish(text=leaky)
    spans = exporter.get_finished_spans()
    text = wire(spans)
    for secret in (ENV_SECRET, EXPLICIT_SECRET, LLM_KEY, "abcdefghijklmnop"):
        assert secret not in text, secret
    assert attrs(only(spans, AGENT))["input.value"].startswith("keys [redacted] [redacted] [redacted]")


def test_secrets_are_removed_from_names_with_content_off(monkeypatch):
    monkeypatch.setenv(ENV_SECRET_NAME, ENV_SECRET)
    exporter, handler = _handler()
    run = Run(handler)
    node, tool = run.start_tool("tool-" + ENV_SECRET, {}, call_id="id-" + ENV_SECRET)
    handler.on_tool_end("done", run_id=tool, parent_run_id=node)
    run.end_node(node)
    run.finish()
    assert ENV_SECRET not in wire(exporter.get_finished_spans())


def test_size_caps():
    exporter, handler = _handler(capture_content=True)
    long_text = "é" * 10_000  # 2 bytes each in UTF-8
    run = Run(handler, query=long_text)
    many = [HumanMessage(content="m{0}".format(index)) for index in range(40)]
    run.history = many
    calls = [
        {"name": "t{0}".format(index), "args": {}, "id": "i{0}".format(index), "type": "tool_call"}
        for index in range(20)
    ]
    run.llm(AIMessage(content=long_text, tool_calls=calls))
    long_name = "n" * 1000
    node, tool = run.start_tool(long_name, {"text": long_text})
    # A traceback longer than the cap: the cause's 20 KB message comes
    # first, the raised exception's line last. The cap keeps the end.
    try:
        try:
            raise RuntimeError("c" * 20_000)
        except RuntimeError as cause:
            raise ValueError("x" * 5000 + " LAST-LINE-END") from cause
    except ValueError as error:
        handler.on_tool_error(error, run_id=tool, parent_run_id=node)
    run.end_node(node)
    run.finish(text=long_text)
    spans = exporter.get_finished_spans()

    for span in spans:
        for key, value in attrs(span).items():
            if isinstance(value, str):
                assert len(value.encode("utf-8")) <= MAX_VALUE_BYTES, key
    agent = attrs(only(spans, AGENT))
    assert agent["input.value"] == "é" * (MAX_VALUE_BYTES // 2)
    llm = attrs(only(spans, LLM_SPAN))
    assert llm["mcp_use.llm.input_message_count"] == 41
    recorded = {key.split(".")[3] for key in llm if key.startswith("gen_ai.input.messages.")}
    assert len(recorded) == callback_module.MAX_MESSAGES == 32
    # The most recent 32 messages: m9..m39 and the system prompt is dropped.
    assert llm["gen_ai.input.messages.31.message.content"] == "m39"
    assert llm["mcp_use.llm.tool_call_count"] == 20
    names = [key for key in llm if key.endswith("function.name")]
    assert len(names) == callback_module.MAX_TOOL_CALLS == 16

    # The tool name is capped before it is put into the span name.
    tool_span = only(spans, "execute_tool " + "n" * MAX_NAME_BYTES)
    assert tool_span.attributes["gen_ai.tool.name"] == "n" * MAX_NAME_BYTES
    (event,) = tool_span.events
    message = event.attributes["exception.message"]
    assert message == "x" * MAX_ERROR_BYTES
    assert tool_span.status.description == "ValueError: " + "x" * MAX_ERROR_BYTES
    stacktrace = event.attributes["exception.stacktrace"]
    assert MAX_STACKTRACE_BYTES - 4 <= len(stacktrace.encode("utf-8")) <= MAX_STACKTRACE_BYTES
    assert stacktrace.rstrip().endswith("LAST-LINE-END")


def test_streamed_tokens_are_events_on_one_llm_span_with_capped_count():
    exporter, handler = _handler(capture_content=True)
    run = Run(handler)
    node = run.node("model")
    llm = uuid4()
    handler.on_chat_model_start({}, [[HumanMessage(content=PROMPT)]], run_id=llm, parent_run_id=node, metadata=METADATA)
    for index in range(200):
        handler.on_llm_new_token("TOKEN-TEXT-{0} ".format(index), run_id=llm, parent_run_id=node)
    handler.on_llm_end(_result(AIMessage(content="streamed")), run_id=llm, parent_run_id=node)
    run.end_node(node)
    run.finish()
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans].count(LLM_SPAN) == 1
    span = only(spans, LLM_SPAN)
    assert span.attributes["mcp_use.llm.chunk_count"] == 200
    assert len(span.events) == callback_module.MAX_CHUNK_EVENTS == 128
    assert [event.attributes["mcp_use.llm.chunk.index"] for event in span.events] == list(range(128))
    assert all(event.name == "mcp_use.llm.chunk" for event in span.events)
    assert all(set(event.attributes) == {"mcp_use.llm.chunk.index"} for event in span.events)
    assert "TOKEN-TEXT" not in wire(spans)


def test_llm_error_fails_the_llm_and_agent_spans():
    exporter, handler = _handler()
    run = Run(handler)
    node = run.node("model")
    llm = uuid4()
    handler.on_chat_model_start({}, [[HumanMessage(content=PROMPT)]], run_id=llm, parent_run_id=node, metadata=METADATA)
    error = ConnectionError("provider down " + PROMPT)
    handler.on_llm_error(error, run_id=llm, parent_run_id=node)
    handler.on_chain_error(error, run_id=node, parent_run_id=run.root)
    handler.on_chain_error(error, run_id=run.root)
    spans = exporter.get_finished_spans()
    assert sorted(span.name for span in spans) == sorted([AGENT, LLM_SPAN])
    for span in spans:
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "ConnectionError"
        assert [event.name for event in span.events] == ["exception"]
        assert span.events[0].attributes["exception.type"] == "ConnectionError"
    assert attrs(only(spans, AGENT))["mcp_use.agent.llm_call_count"] == 1
    assert PROMPT not in wire(spans)


def test_mcp_use_formatted_tool_error_is_an_error_span():
    # mcp-use 1.7.1 returns a failed MCP call as format_error()'s dict, as
    # JSON in the ToolMessage (agents/adapters/langchain_adapter.py:186-205).
    for capture in (False, True):
        exporter, handler = _handler(capture_content=capture)
        run = Run(handler)
        node, tool = run.start_tool("fail", {"reason": ARG})
        content = json.dumps(
            {
                "error": "RuntimeError",
                "details": "Error executing tool fail: tool failed because " + ARG,
                "stack": "Traceback (most recent call last):\n  ...\nRuntimeError: boom",
                "code": "UNKNOWN",
                "tool": "fail",
            }
        )
        handler.on_tool_end(ToolMessage(content=content, tool_call_id="call-1"), run_id=tool, parent_run_id=node)
        run.end_node(node)
        run.finish()
        spans = exporter.get_finished_spans()
        span = only(spans, "execute_tool fail")
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes["mcp_use.tool.error_type"] == "RuntimeError"
        (event,) = span.events
        assert event.attributes["exception.type"] == "RuntimeError"
        assert "gen_ai.tool.call.result" not in span.attributes
        assert attrs(only(spans, AGENT))["mcp_use.agent.tool_error_count"] == 1
        if capture:
            assert span.status.description == "RuntimeError: Error executing tool fail: tool failed because " + ARG
            assert event.attributes["exception.stacktrace"].endswith("RuntimeError: boom")
        else:
            assert span.status.description == "RuntimeError"
            assert ARG not in wire(spans)


def test_tool_message_with_error_status_is_an_error_span():
    exporter, handler = _handler()
    run = Run(handler)
    node, tool = run.start_tool("add", {"a": 1})
    output = ToolMessage(content="Error: bad input", tool_call_id="call-1", status="error")
    handler.on_tool_end(output, run_id=tool, parent_run_id=node)
    run.end_node(node)
    run.finish()
    span = only(exporter.get_finished_spans(), TOOL_SPAN)
    assert span.status.status_code is StatusCode.ERROR
    assert span.events[0].attributes["exception.type"] == "langchain_core.tools.ToolException"


@pytest.mark.parametrize("error", [asyncio.CancelledError(), GeneratorExit()])
def test_cancellation_ends_open_spans_as_cancelled_without_an_exception_event(error):
    exporter, handler = _handler()
    run = Run(handler)
    node, tool = run.start_tool("slow", {"seconds": 30})
    # What LangGraph reports when the task is cancelled during the tool
    # call: chain errors for the tools node and the root, no tool callback.
    handler.on_chain_error(error, run_id=node, parent_run_id=run.root)
    handler.on_chain_error(error, run_id=run.root)
    spans = exporter.get_finished_spans()
    assert sorted(span.name for span in spans) == sorted([AGENT, "execute_tool slow"])
    for span in spans:
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "cancelled"
        assert span.attributes["mcp_use.cancelled"] is True
        assert span.events == ()


def test_a_run_whose_parent_ended_first_is_ended_as_incomplete():
    exporter, handler = _handler()
    run = Run(handler)
    node = run.node("model")
    handler.on_chat_model_start({}, [[HumanMessage(content="x")]], run_id=uuid4(), parent_run_id=node)
    run.finish()
    spans = exporter.get_finished_spans()
    llm = only(spans, "chat")
    assert llm.attributes["mcp_use.incomplete"] is True
    assert llm.status.status_code is StatusCode.ERROR
    assert llm.status.description == "ended without a result"
    assert only(spans, AGENT).status.status_code is StatusCode.OK
    assert handler._runs == {} and len(handler._roots) == 0


def test_text_completion_models_get_an_llm_span():
    exporter, handler = _handler(capture_content=True)
    run = Run(handler)
    node = run.node("model")
    llm = uuid4()
    handler.on_llm_start({}, ["complete " + PROMPT], run_id=llm, parent_run_id=node, invocation_params={"model": "completion-1"})
    handler.on_llm_end(LLMResult(generations=[[]], llm_output={"model_name": "completion-1"}), run_id=llm, parent_run_id=node)
    run.end_node(node)
    run.finish()
    span = only(exporter.get_finished_spans(), "text_completion completion-1")
    assert span.attributes["gen_ai.operation.name"] == "text_completion"
    assert span.attributes["input.value"] == "complete " + PROMPT


def test_interleaved_agent_runs_on_one_handler_stay_separate():
    exporter, handler = _handler()
    first, second = Run(handler), Run(handler)
    first.llm(AIMessage(content="a"))
    second.llm(AIMessage(content="b"))
    second.tool("add", {"a": 1}, "r")
    first.finish()
    second.finish()
    spans = exporter.get_finished_spans()
    agents = spans_named(spans, AGENT)
    assert len(agents) == 2
    for agent in agents:
        children = [span for span in spans if parent_id(span) == agent.context.span_id]
        assert all(child.context.trace_id == agent.context.trace_id for child in children)
    counts = sorted((a.attributes["mcp_use.agent.llm_call_count"], a.attributes["mcp_use.agent.tool_call_count"]) for a in agents)
    assert counts == [(1, 0), (1, 1)]
    assert handler._runs == {}


def test_open_runs_are_bounded(monkeypatch):
    monkeypatch.setattr(callback_module, "MAX_OPEN_RUNS", 3)
    exporter, handler = _handler()
    abandoned = Run(handler)
    abandoned.start_tool("slow", {})
    current = Run(handler)
    current.node("model")  # 5 open runs: the abandoned agent run is evicted
    spans = exporter.get_finished_spans()
    assert sorted(span.name for span in spans) == sorted([AGENT, "execute_tool slow"])
    assert all(span.attributes["mcp_use.incomplete"] is True for span in spans)
    current.finish()
    assert handler._runs == {}


def test_the_current_span_parents_the_agent_span():
    exporter, handler = _handler()
    tracer = new_provider()[1].get_tracer("test")
    with tracer.start_as_current_span("request") as outer:
        _journey(handler)
    agent = only(exporter.get_finished_spans(), AGENT)
    assert parent_id(agent) == outer.get_span_context().span_id
    assert agent.context.trace_id == outer.get_span_context().trace_id


def test_context_attributes_are_stamped_on_every_span():
    exporter, handler = _handler()
    with using_session("session-1"), using_user("user-1"), using_metadata({"tenant": "t1"}), using_tags(["beta"]):
        _journey(handler)
    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    for span in spans:
        assert span.attributes["session.id"] == "session-1"
        assert span.attributes["user.id"] == "user-1"
        assert json.loads(span.attributes["metadata"]) == {"tenant": "t1"}
        assert span.attributes["tag.tags"] == ("beta",)


def test_suppress_tracing_records_nothing():
    exporter, handler = _handler()
    with suppress_tracing():
        _journey(handler)
    assert exporter.get_finished_spans() == ()
    assert handler._runs == {}


class _Broken:
    """A tracer provider whose tracers and spans raise on every call."""

    def get_tracer(self, *args: Any, **kwargs: Any) -> Any:
        return self

    def start_span(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("tracer is broken")


class _BrokenSpans(trace_api.TracerProvider):
    """Spans start, then raise from set_status, add_event and end.

    With ``on_attribute`` they also raise from set_attribute, so starting
    one fails too (FITracer sets the start attributes).
    """

    def __init__(self, on_attribute: bool) -> None:
        self.on_attribute = on_attribute

    def get_tracer(self, *args: Any, **kwargs: Any) -> Any:
        return self

    def start_span(self, *args: Any, **kwargs: Any) -> Any:
        return _BrokenSpan(self.on_attribute)


class _BrokenSpan(trace_api.NonRecordingSpan):
    def __init__(self, on_attribute: bool) -> None:
        super().__init__(trace_api.INVALID_SPAN_CONTEXT)
        self.on_attribute = on_attribute

    def is_recording(self) -> bool:
        return True

    def set_attribute(self, *args: Any, **kwargs: Any) -> None:
        if self.on_attribute:
            raise RuntimeError("span is broken")

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("span is broken")

    def add_event(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("span is broken")

    def end(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("span is broken")


@pytest.mark.parametrize(
    "provider", [_Broken(), _BrokenSpans(on_attribute=False), _BrokenSpans(on_attribute=True)]
)
def test_tracer_failures_never_reach_the_caller(provider):
    handler = FutureAGICallback(tracer_provider=provider, capture_content=True)
    _journey(handler)
    _failing_tool(handler, ValueError("boom"))
    run = Run(handler)
    node = run.node("model")
    llm = uuid4()
    handler.on_chat_model_start({}, [[HumanMessage(content="x")]], run_id=llm, parent_run_id=node)
    handler.on_llm_new_token("t", run_id=llm, parent_run_id=node)
    handler.on_chain_error(asyncio.CancelledError(), run_id=run.root)
    assert handler._runs == {}


@pytest.mark.parametrize("capture", [False, True])
def test_malformed_callback_payloads_never_raise(capture):
    exporter, handler = _handler(capture_content=capture)
    root, child = uuid4(), uuid4()
    weird: List[Any] = [None, object(), 42, "text", [object()], {"messages": object()}]
    for value in weird:
        handler.on_chain_start(value, value, run_id=root)
        handler.on_chat_model_start(value, [[value]] if value is not None else value, run_id=child, parent_run_id=root, metadata=value, invocation_params=value)
        handler.on_llm_new_token(value, run_id=child)
        handler.on_llm_end(value, run_id=child)
        handler.on_llm_start(value, value, run_id=child, parent_run_id=root)
        handler.on_llm_error(RuntimeError(), run_id=child)
        handler.on_tool_start(value, value, run_id=child, parent_run_id=root, inputs=value)
        handler.on_tool_end(value, run_id=child)
        handler.on_tool_error(RuntimeError(), run_id=child)
        handler.on_retriever_start(value, value, run_id=child, parent_run_id=root)
        handler.on_retriever_end(value, run_id=child)
        handler.on_chain_end(value, run_id=root)
        handler.on_chain_error(RuntimeError(), run_id=uuid4())
    assert handler._runs == {}


def test_constructor_validates_its_arguments():
    with pytest.raises(TypeError, match="TraceConfig"):
        FutureAGICallback(config={"hide_inputs": True})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="capture_content"):
        FutureAGICallback(capture_content="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="not a string"):
        FutureAGICallback(redact="secret-value")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="strings"):
        FutureAGICallback(redact=[b"secret-value"])  # type: ignore[list-item]
