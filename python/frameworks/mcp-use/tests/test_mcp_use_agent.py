"""MCPAgent end to end: the real mcp-use 1.7.1 agent loop on a fake LLM.

``ChatFake`` replays a script; the MCP server is ``_mcp_server.py`` on a
loopback streamable-HTTP port (see ``_mcp_use_support``). Spans go to an
InMemorySpanExporter through a provider passed to the callback.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import Any, Dict, List

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
    ChatFake,
    ToolStarted,
    add_script,
    answer,
    attrs,
    mcp_client,
    new_provider,
    only,
    parent_id,
    run_agent,
    spans_named,
    tool_call,
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
from langchain_core.callbacks import BaseCallbackHandler
from mcp_use import MCPAgent
from opentelemetry.trace import StatusCode

from traceai_mcp_use import FutureAGICallback

pytest.importorskip("uvicorn", reason="the loopback MCP server runs on uvicorn")


def _traced(**kwargs: Any):
    exporter, provider = new_provider()
    return exporter, FutureAGICallback(tracer_provider=provider, **kwargs)


def _assert_j1_tree(spans) -> None:
    """One agent root; LLM, tool, LLM as its children, in one trace."""
    assert sorted(span.name for span in spans) == sorted([AGENT, LLM_SPAN, LLM_SPAN, "execute_tool add"])
    agent = only(spans, AGENT)
    assert parent_id(agent) is None
    children = sorted(
        (span for span in spans if span is not agent), key=lambda span: span.start_time
    )
    assert [span.name for span in children] == [LLM_SPAN, "execute_tool add", LLM_SPAN]
    for child in children:
        # The callback tree puts the tool run under the "tools" graph node
        # and the model run under the "model" node; both nodes are siblings
        # under the root, so the tool span is a sibling of the LLM spans.
        assert parent_id(child) == agent.context.span_id
        assert child.context.trace_id == agent.context.trace_id
        assert child.start_time >= agent.start_time and child.end_time <= agent.end_time
    for span in spans:
        assert span.status.status_code is StatusCode.OK, span.name
    assert attrs(agent)["gen_ai.span.kind"] == "AGENT"
    assert attrs(agent)["mcp_use.agent.llm_call_count"] == 2
    assert attrs(agent)["mcp_use.agent.tool_call_count"] == 1
    assert attrs(agent)["mcp_use.agent.tool_error_count"] == 0
    llm = attrs(children[0])
    assert llm["gen_ai.span.kind"] == "LLM"
    assert llm["gen_ai.request.model"] == MODEL
    assert llm["gen_ai.response.model"] == MODEL
    assert llm["gen_ai.usage.input_tokens"] == 11
    assert llm["mcp_use.llm.tool_call_count"] == 1
    tool = attrs(children[1])
    assert tool["gen_ai.span.kind"] == "TOOL"
    assert tool["gen_ai.tool.name"] == "add"
    assert tool["gen_ai.tool.call.id"] == "call-1"


@pytest.mark.parametrize("method", ["run", "stream", "stream_events"])
def test_every_entry_point_gives_one_agent_span_with_llm_and_tool_children(method):
    exporter, handler = _traced()
    result = run_agent(add_script(), [handler], method=method)
    if method == "stream_events":
        assert any(event.get("event") == "on_tool_end" for event in result)
    else:
        assert result == ANSWER
    _assert_j1_tree(exporter.get_finished_spans())
    assert handler._runs == {}


class _TokenCounter(BaseCallbackHandler):
    run_inline = True

    def __init__(self) -> None:
        super().__init__()
        self.tokens: List[str] = []

    def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        self.tokens.append(token)


@pytest.mark.parametrize("method", ["run", "stream_events"])
def test_a_streamed_completion_is_one_llm_span(method):
    exporter, handler = _traced()
    counter = _TokenCounter()
    result = run_agent(add_script(), [handler, counter], method=method, streaming=True)
    spans = exporter.get_finished_spans()
    _assert_j1_tree(spans)
    tool_turn, final_turn = sorted(spans_named(spans, LLM_SPAN), key=lambda span: span.start_time)
    # "ANSWER-MARKER the sum is five" streams as five word chunks; every
    # token LangChain reports is counted and none becomes a span.
    assert {"ANSWER-MARKER ", "the ", "sum ", "is ", "five"} <= set(counter.tokens)
    chunks = tool_turn.attributes["mcp_use.llm.chunk_count"] + final_turn.attributes["mcp_use.llm.chunk_count"]
    assert chunks == len(counter.tokens)
    assert final_turn.attributes["mcp_use.llm.chunk_count"] >= 5
    assert {event.name for event in final_turn.events} == {"mcp_use.llm.chunk"}
    assert len(final_turn.events) == final_turn.attributes["mcp_use.llm.chunk_count"]
    assert "ANSWER-MARKER" not in wire(spans)
    if method == "run":
        assert result == ANSWER


def test_content_and_keys_are_not_recorded_by_default(monkeypatch):
    monkeypatch.setenv(ENV_SECRET_NAME, ENV_SECRET)
    exporter, handler = _traced()
    script = [tool_call("echo", {"text": ARG + " " + ENV_SECRET}), answer()]
    run_agent(script, [handler], query=PROMPT + " " + ENV_SECRET)
    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    text = wire(spans)
    for marker in CONTENT_MARKERS + (LLM_KEY, ENV_SECRET):
        assert marker not in text, marker


def test_capture_content_records_content_without_secrets(monkeypatch):
    monkeypatch.setenv(ENV_SECRET_NAME, ENV_SECRET)
    exporter, handler = _traced(capture_content=True, redact=[EXPLICIT_SECRET])
    leak = "{0} {1} {2}".format(ENV_SECRET, EXPLICIT_SECRET, LLM_KEY)
    script = [tool_call("echo", {"text": ARG + " " + leak}), answer()]
    run_agent(script, [handler], query=PROMPT + " " + leak)
    spans = exporter.get_finished_spans()
    text = wire(spans)
    for secret in (ENV_SECRET, EXPLICIT_SECRET, LLM_KEY):
        assert secret not in text, secret
    agent = attrs(only(spans, AGENT))
    assert agent["input.value"] == PROMPT + " [redacted] [redacted] [redacted]"
    assert agent["output.value"] == ANSWER
    tool = attrs(only(spans, "execute_tool echo"))
    assert json.loads(tool["gen_ai.tool.call.arguments"]) == {"text": ARG + " [redacted] [redacted] [redacted]"}
    assert tool["gen_ai.tool.call.result"] == "ECHO-RESULT:" + ARG + " [redacted] [redacted] [redacted]"


def test_mcp_tool_error_fails_the_tool_span_and_the_agent_recovers():
    # mcp-use 1.7.1 turns a failed MCP call into a formatted error the LLM
    # reads (agents/adapters/langchain_adapter.py:181-206), so the run
    # succeeds; the tool span is ERROR and the agent counts the error.
    exporter, handler = _traced()
    script = [tool_call("fail", {"reason": ARG}), answer()]
    assert run_agent(script, [handler]) == ANSWER
    spans = exporter.get_finished_spans()
    tool = only(spans, "execute_tool fail")
    assert tool.status.status_code is StatusCode.ERROR
    assert tool.status.description == "RuntimeError"
    assert tool.attributes["mcp_use.tool.error_type"] == "RuntimeError"
    assert [event.attributes["exception.type"] for event in tool.events] == ["RuntimeError"]
    agent = only(spans, AGENT)
    assert agent.status.status_code is StatusCode.OK
    assert agent.attributes["mcp_use.agent.tool_error_count"] == 1
    assert "tool failed because" not in wire(spans) and ARG not in wire(spans)


def test_tool_error_text_is_recorded_with_capture_and_scrubbed_with_hide_inputs():
    exporter, handler = _traced(capture_content=True)
    run_agent([tool_call("fail", {"reason": ARG}), answer()], [handler])
    tool = only(exporter.get_finished_spans(), "execute_tool fail")
    assert tool.status.description == "RuntimeError: Error executing tool fail: tool failed because " + ARG

    exporter, handler = _traced(capture_content=True, config=TraceConfig(hide_inputs=True))
    run_agent([tool_call("fail", {"reason": ARG}), answer()], [handler])
    spans = exporter.get_finished_spans()
    tool = only(spans, "execute_tool fail")
    assert tool.status.description == "RuntimeError: Error executing tool fail: tool failed because " + REDACTED_VALUE
    (event,) = tool.events
    assert ARG not in event.attributes["exception.message"]
    assert ARG not in event.attributes["exception.stacktrace"]
    assert ARG not in wire(spans) and "PROMPT-MARKER" not in wire(spans)


@pytest.mark.parametrize("retry_on_error", [True, False])
def test_invalid_tool_arguments_fail_the_tool_span(retry_on_error):
    exporter, handler = _traced()
    script = [tool_call("add", {"a": "not-a-number", "b": 3}), answer()]
    result = run_agent(script, [handler], agent_kwargs={"retry_on_error": retry_on_error})
    assert result == ANSWER
    spans = exporter.get_finished_spans()
    tool = only(spans, "execute_tool add")
    assert tool.status.status_code is StatusCode.ERROR
    assert tool.events[0].attributes["exception.type"] == "pydantic_core._pydantic_core.ValidationError"
    assert only(spans, AGENT).attributes["mcp_use.agent.tool_error_count"] == 1


def test_an_llm_error_fails_the_agent_span_and_reaches_the_caller_unchanged():
    exporter, handler = _traced()
    error = ConnectionError("provider unreachable")
    with pytest.raises(ConnectionError) as raised:
        run_agent([error], [handler])
    assert raised.value is error
    spans = exporter.get_finished_spans()
    assert sorted(span.name for span in spans) == sorted([AGENT, LLM_SPAN])
    for span in spans:
        assert span.status.status_code is StatusCode.ERROR
        assert span.events[0].attributes["exception.type"] == "ConnectionError"
    assert parent_id(only(spans, LLM_SPAN)) == only(spans, AGENT).context.span_id


def test_cancelling_the_run_ends_the_open_spans_as_cancelled():
    exporter, handler = _traced()
    started = ToolStarted()

    async def go() -> None:
        client = mcp_client()
        try:
            agent = MCPAgent(
                llm=ChatFake(script=[tool_call("slow", {"seconds": 30}), answer()]),
                client=client,
                callbacks=[handler, started],
            )
            task = asyncio.ensure_future(agent.run(PROMPT))
            for _ in range(1000):
                if started.started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.started.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await client.close_all_sessions()

    asyncio.run(go())
    spans = exporter.get_finished_spans()
    assert sorted(span.name for span in spans) == sorted([AGENT, LLM_SPAN, "execute_tool slow"])
    for name in (AGENT, "execute_tool slow"):
        span = only(spans, name)
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "cancelled"
        assert span.attributes["mcp_use.cancelled"] is True
        assert span.events == ()
    assert only(spans, LLM_SPAN).status.status_code is StatusCode.OK
    assert handler._runs == {}


def test_leaving_stream_early_ends_the_agent_span_as_cancelled():
    exporter, handler = _traced()

    async def go() -> None:
        client = mcp_client()
        try:
            agent = MCPAgent(llm=ChatFake(script=add_script()), client=client, callbacks=[handler])
            stream = agent.stream(PROMPT)
            async for _ in stream:
                break  # the first item is the (action, observation) of the tool step
            await stream.aclose()
            for _ in range(500):
                if spans_named(exporter.get_finished_spans(), AGENT):
                    break
                await asyncio.sleep(0.01)
        finally:
            await client.close_all_sessions()

    asyncio.run(go())
    spans = exporter.get_finished_spans()
    agent = only(spans, AGENT)
    assert agent.status.status_code is StatusCode.ERROR
    assert agent.status.description == "cancelled"
    assert agent.attributes["mcp_use.cancelled"] is True
    assert only(spans, "execute_tool add").status.status_code is StatusCode.OK
    assert handler._runs == {}


def test_empty_callbacks_list_yields_no_spans():
    # callbacks=[] is the Python switch: ObservabilityManager.get_callbacks
    # returns it as is and adds no default handler
    # (agents/observability/callbacks_manager.py:72-74).
    exporter, handler = _traced()
    seen: List[Any] = []

    async def go() -> None:
        client = mcp_client()
        try:
            agent = MCPAgent(llm=ChatFake(script=add_script()), client=client, callbacks=[])
            seen.append(agent.callbacks)
            assert await agent.run(PROMPT) == ANSWER
        finally:
            await client.close_all_sessions()

    asyncio.run(go())
    assert seen == [[]]
    assert exporter.get_finished_spans() == ()


def test_an_agent_without_the_callback_is_not_traced():
    # The handler exists in the process but only traces where it is passed.
    exporter, handler = _traced()
    run_agent(add_script(), [handler])
    exporter.clear()
    run_agent(add_script(), None)
    run_agent(add_script(), [ToolStarted()])
    assert exporter.get_finished_spans() == ()


def test_the_callers_current_span_parents_the_agent_span():
    exporter, handler = _traced()
    tracer = new_provider()[1].get_tracer("app")
    outer: List[Any] = []

    @contextmanager
    def around():
        with tracer.start_as_current_span("request") as span:
            outer.append(span)
            yield

    run_agent(add_script(), [handler], around=around)
    agent = only(exporter.get_finished_spans(), AGENT)
    assert parent_id(agent) == outer[0].get_span_context().span_id
    assert agent.context.trace_id == outer[0].get_span_context().trace_id


def test_session_user_metadata_and_tags_reach_every_span():
    exporter, handler = _traced()

    @contextmanager
    def around():
        with using_session("session-1"), using_user("user-1"), using_metadata(
            {"tenant": "t1"}
        ), using_tags(["beta"]):
            yield

    run_agent(add_script(), [handler], around=around)
    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    for span in spans:
        assert span.attributes["session.id"] == "session-1", span.name
        assert span.attributes["user.id"] == "user-1"
        assert json.loads(span.attributes["metadata"]) == {"tenant": "t1"}
        assert span.attributes["tag.tags"] == ("beta",)


def test_suppress_tracing_records_nothing_and_the_run_still_answers():
    exporter, handler = _traced()
    assert run_agent(add_script(), [handler], around=suppress_tracing) == ANSWER
    assert exporter.get_finished_spans() == ()


def test_concurrent_runs_on_one_handler_get_separate_traces():
    exporter, handler = _traced()

    async def one() -> Any:
        client = mcp_client()
        try:
            agent = MCPAgent(llm=ChatFake(script=add_script(), delay=0.05), client=client, callbacks=[handler])
            return await agent.run(PROMPT)
        finally:
            await client.close_all_sessions()

    async def both() -> List[Any]:
        return await asyncio.gather(one(), one(), one())

    assert asyncio.run(both()) == [ANSWER] * 3
    spans = exporter.get_finished_spans()
    agents = spans_named(spans, AGENT)
    assert len(agents) == 3
    for agent in agents:
        tree = [span for span in spans if span.context.trace_id == agent.context.trace_id]
        _assert_j1_tree(tree)
    assert handler._runs == {}


def test_a_broken_tracer_does_not_change_the_agent_result():
    class Broken:
        def get_tracer(self, *args: Any, **kwargs: Any) -> Any:
            return self

        def start_span(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("tracer is broken")

    handler = FutureAGICallback(tracer_provider=Broken(), capture_content=True)
    assert run_agent(add_script(), [handler]) == ANSWER
    for method in ("stream", "stream_events"):
        run_agent(add_script(), [handler], method=method)
    assert handler._runs == {}


def test_sync_and_async_langchain_dispatch_give_the_same_spans():
    # MCPAgent is async only. The handler also runs under LangChain's sync
    # callback manager: drive a LangChain agent with a plain tool through
    # invoke() and ainvoke() and compare.
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    @tool
    def add(a: int, b: int) -> str:
        """Add two integers."""
        return "SUM-RESULT-{0}".format(a + b)

    shapes = []
    for mode in ("sync", "async"):
        exporter, handler = _traced(capture_content=True)
        graph = create_agent(ChatFake(script=add_script()), [add])
        inputs = {"messages": [("user", PROMPT)]}
        config: Dict[str, Any] = {"callbacks": [handler]}
        if mode == "sync":
            graph.invoke(inputs, config=config)
        else:
            asyncio.run(graph.ainvoke(inputs, config=config))
        spans = exporter.get_finished_spans()
        _assert_j1_tree(spans)
        assert attrs(only(spans, "execute_tool add"))["gen_ai.tool.call.result"] == "SUM-RESULT-5"
        shapes.append(sorted((span.name, tuple(sorted(attrs(span)))) for span in spans))
    assert shapes[0] == shapes[1]


def test_the_structured_output_formatting_call_has_no_span():
    # With output_schema, mcp-use 1.7.1 formats the answer after the graph
    # run with structured_llm.ainvoke(prompt), passing no callbacks
    # (agents/mcpagent.py:595, 886). That call gets no span and does not
    # start a second agent span.
    from langchain_core.runnables import RunnableLambda
    from pydantic import BaseModel

    class Sum(BaseModel):
        total: int

    class ChatStructured(ChatFake):
        def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
            return RunnableLambda(lambda prompt: Sum(total=5))

    exporter, handler = _traced()

    async def go() -> Any:
        client = mcp_client()
        try:
            agent = MCPAgent(llm=ChatStructured(script=add_script()), client=client, callbacks=[handler])
            return await agent.run(PROMPT, output_schema=Sum)
        finally:
            await client.close_all_sessions()

    assert asyncio.run(go()) == Sum(total=5)
    _assert_j1_tree(exporter.get_finished_spans())
