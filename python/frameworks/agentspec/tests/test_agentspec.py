"""Agent Spec instrumentation over the real pyagentspec Trace, Span and Event classes."""
import asyncio
import json

import pytest
from opentelemetry import context as context_api
from opentelemetry import trace as otel_trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import StatusCode
from pyagentspec.tracing.events import (
    AgentExecutionEnd,
    AgentExecutionStart,
    FlowExecutionEnd,
    FlowExecutionStart,
    LlmGenerationChunkReceived,
    LlmGenerationRequest,
    LlmGenerationResponse,
    NodeExecutionEnd,
    NodeExecutionStart,
    ToolExecutionRequest,
    ToolExecutionResponse,
)
from pyagentspec.tracing.events.llmgeneration import ToolCall
from pyagentspec.tracing.messages.message import Message
from pyagentspec.tracing.spanprocessor import SpanProcessor
from pyagentspec.tracing.spans import (
    AgentExecutionSpan,
    FlowExecutionSpan,
    LlmGenerationSpan,
    NodeExecutionSpan,
    ToolExecutionSpan,
)
from pyagentspec.tracing.trace import Trace

from conftest import AGENT, FLOW, GENERATION, LLM, SECRET_ERROR, SECRET_KEY, START, TOOL, attrs, by_name
from fi_instrumentation import TraceConfig

PROMPT = [Message(content="Capital of Japan?", role="user")]


def run_agent(trace_kwargs=None, *, stream=False, fail=None):
    """One agent run: agent span > llm span (tool call) > tool span > llm span (answer)."""
    with Trace(name="run", **(trace_kwargs or {})):
        with AgentExecutionSpan(agent=AGENT) as agent_span:
            agent_span.add_event(AgentExecutionStart(agent=AGENT, inputs={"question": "Capital of Japan?"}))
            with LlmGenerationSpan(llm_config=LLM) as llm_span:
                llm_span.add_event(
                    LlmGenerationRequest(
                        llm_config=LLM, prompt=PROMPT, tools=[TOOL], request_id="r1",
                        llm_generation_config=GENERATION,
                    )
                )
                if stream:
                    for piece in ("To", "kyo"):
                        llm_span.add_event(
                            LlmGenerationChunkReceived(llm_config=LLM, content=piece, request_id="r1", output_tokens=1)
                        )
                llm_span.add_event(
                    LlmGenerationResponse(
                        llm_config=LLM, content="Tokyo", request_id="r1", completion_id="c1",
                        input_tokens=11, output_tokens=2,
                        tool_calls=[ToolCall(call_id="t1", tool_name="lookup", arguments='{"country": "Japan"}')],
                    )
                )
            with ToolExecutionSpan(tool=TOOL) as tool_span:
                tool_span.add_event(ToolExecutionRequest(tool=TOOL, inputs={"country": "Japan"}, request_id="t1"))
                if fail:
                    raise fail
                tool_span.add_event(ToolExecutionResponse(tool=TOOL, outputs={"capital": "Tokyo"}, request_id="t1"))
            agent_span.add_event(AgentExecutionEnd(agent=AGENT, outputs={"answer": "Tokyo"}))


# --- agent, llm and tool spans ------------------------------------------------------------------

def test_agent_run_produces_agent_llm_and_tool_spans_in_one_tree(spans):
    run_agent()
    finished = spans.get_finished_spans()
    assert len(finished) == 3, [s.name for s in finished]
    agent = by_name(spans, "agentspec.agent.geo-helper")
    llm = by_name(spans, "agentspec.llm.gpt-served")
    tool = by_name(spans, "agentspec.tool.lookup")
    assert attrs(agent)["gen_ai.span.kind"] == "AGENT"
    assert attrs(llm)["gen_ai.span.kind"] == "LLM"
    assert attrs(tool)["gen_ai.span.kind"] == "TOOL"
    assert agent.parent is None
    assert llm.parent.span_id == agent.context.span_id
    assert tool.parent.span_id == agent.context.span_id
    assert len({s.context.trace_id for s in finished}) == 1
    assert all(s.status.status_code != StatusCode.ERROR for s in finished)


def test_agent_attributes_and_content(spans):
    run_agent()
    a = attrs(by_name(spans, "agentspec.agent.geo-helper"))
    assert a["gen_ai.agent.name"] == "geo-helper"
    assert a["gen_ai.agent.description"] == "Answers geography"
    assert a["gen_ai.operation.name"] == "invoke_agent"
    assert json.loads(a["input.value"]) == {"question": "Capital of Japan?"}
    assert json.loads(a["output.value"]) == {"answer": "Tokyo"}
    assert a["output.mime_type"] == "application/json"


def test_llm_model_parameters_usage_and_content(spans):
    run_agent()
    a = attrs(by_name(spans, "agentspec.llm.gpt-served"))
    assert a["gen_ai.request.model"] == "gpt-served"
    assert a["gen_ai.provider.name"] == "openai"
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.request.temperature"] == 0.2
    assert a["gen_ai.request.max_tokens"] == 64
    assert a["gen_ai.request.top_p"] == 0.9
    assert a["gen_ai.usage.input_tokens"] == 11
    assert a["gen_ai.usage.output_tokens"] == 2
    assert a["gen_ai.usage.total_tokens"] == 13
    assert a["gen_ai.response.id"] == "c1"
    assert json.loads(a["input.value"]) == [{"role": "user", "content": "Capital of Japan?"}]
    assert a["output.value"] == "Tokyo"
    prefix = "gen_ai.output.messages.0.message.tool_calls.0.tool_call"
    assert a[f"{prefix}.id"] == "t1"
    assert a[f"{prefix}.function.name"] == "lookup"
    assert json.loads(a[f"{prefix}.function.arguments"]) == {"country": "Japan"}


def test_tool_attributes_and_content(spans):
    run_agent()
    a = attrs(by_name(spans, "agentspec.tool.lookup"))
    assert a["gen_ai.tool.name"] == "lookup"
    assert a["gen_ai.tool.description"] == "Look a capital up"
    assert a["gen_ai.operation.name"] == "execute_tool"
    assert a["gen_ai.tool.call.id"] == "t1"
    assert json.loads(a["input.value"]) == {"country": "Japan"}
    assert json.loads(a["output.value"]) == {"capital": "Tokyo"}


def test_flow_and_node_spans_are_chains(spans):
    with Trace(name="flow-run"):
        with FlowExecutionSpan(flow=FLOW) as flow_span:
            flow_span.add_event(FlowExecutionStart(flow=FLOW, inputs={"x": 1}))
            with NodeExecutionSpan(node=START) as node_span:
                node_span.add_event(NodeExecutionStart(node=START, inputs={"x": 1}))
                node_span.add_event(NodeExecutionEnd(node=START, outputs={"x": 1}, branch_selected="next"))
            flow_span.add_event(FlowExecutionEnd(flow=FLOW, outputs={"y": 2}, branch_selected="end"))
    flow = by_name(spans, "agentspec.flow.geo-flow")
    node = by_name(spans, "agentspec.node.start")
    assert attrs(flow)["gen_ai.span.kind"] == "CHAIN"
    assert attrs(node)["gen_ai.span.kind"] == "CHAIN"
    assert node.parent.span_id == flow.context.span_id
    assert json.loads(attrs(flow)["output.value"]) == {"y": 2}
    assert attrs(flow)["agentspec.flow.branch_selected"] == "end"
    assert attrs(node)["agentspec.node.branch_selected"] == "next"


def test_async_run_is_traced_the_same(spans):
    async def run():
        async with Trace(name="async-run"):
            async with AgentExecutionSpan(agent=AGENT) as agent_span:
                await agent_span.add_event_async(AgentExecutionStart(agent=AGENT, inputs={"q": "?"}))
                async with LlmGenerationSpan(llm_config=LLM) as llm_span:
                    await llm_span.add_event_async(
                        LlmGenerationResponse(llm_config=LLM, content="Tokyo", request_id="r", output_tokens=2)
                    )
                async with ToolExecutionSpan(tool=TOOL) as tool_span:
                    await tool_span.add_event_async(
                        ToolExecutionResponse(tool=TOOL, outputs={"capital": "Tokyo"}, request_id="t")
                    )

    asyncio.run(run())
    agent = by_name(spans, "agentspec.agent.geo-helper")
    llm = by_name(spans, "agentspec.llm.gpt-served")
    tool = by_name(spans, "agentspec.tool.lookup")
    assert llm.parent.span_id == agent.context.span_id and tool.parent.span_id == agent.context.span_id
    assert attrs(llm)["output.value"] == "Tokyo"
    assert json.loads(attrs(tool)["output.value"]) == {"capital": "Tokyo"}


def test_concurrent_async_traces_do_not_mix(spans):
    async def one(name):
        async with Trace(name=name):
            async with AgentExecutionSpan(agent=AGENT, name=name) as agent_span:
                await asyncio.sleep(0)
                await agent_span.add_event_async(AgentExecutionStart(agent=AGENT, inputs={"who": name}))
                async with LlmGenerationSpan(llm_config=LLM, name=f"{name}-llm"):
                    await asyncio.sleep(0)

    async def main():
        await asyncio.gather(one("a"), one("b"))

    asyncio.run(main())
    for name in ("a", "b"):
        agent = by_name(spans, f"agentspec.agent.{name}")
        llm = by_name(spans, f"agentspec.llm.{name}-llm")
        assert llm.parent.span_id == agent.context.span_id
        assert json.loads(attrs(agent)["input.value"]) == {"who": name}


# --- streaming ---------------------------------------------------------------------------------

def test_streamed_chunks_are_assembled_and_the_final_response_wins(spans):
    run_agent(stream=True)
    a = attrs(by_name(spans, "agentspec.llm.gpt-served"))
    assert a["output.value"] == "Tokyo"
    assert "agentspec.stream.incomplete" not in a


def test_stream_abandoned_after_some_chunks_ends_with_the_text_so_far(spans):
    with Trace(name="early-exit"):
        with LlmGenerationSpan(llm_config=LLM) as llm_span:
            llm_span.add_event(LlmGenerationRequest(llm_config=LLM, prompt=PROMPT, tools=[], request_id="r1"))
            for piece in ("To", "ky"):
                llm_span.add_event(LlmGenerationChunkReceived(llm_config=LLM, content=piece, request_id="r1"))
            # the consumer stops reading: the span closes without a response event
    llm = by_name(spans, "agentspec.llm.gpt-served")
    a = attrs(llm)
    assert a["output.value"] == "Toky"
    assert a["agentspec.stream.incomplete"] is True
    assert llm.end_time is not None
    assert llm.status.status_code != StatusCode.ERROR


# --- failures ----------------------------------------------------------------------------------

class Boom(RuntimeError):
    pass


def test_error_sets_status_and_error_type_without_the_message_and_reraises_unchanged(spans):
    error = Boom(SECRET_ERROR)
    with pytest.raises(Boom) as raised:
        run_agent(fail=error)
    assert raised.value is error
    tool = by_name(spans, "agentspec.tool.lookup")
    agent = by_name(spans, "agentspec.agent.geo-helper")
    for failed in (tool, agent):
        assert failed.status.status_code == StatusCode.ERROR
        assert attrs(failed)["error.type"] == "Boom"
    # the llm span that completed before the failure is not marked as failed
    assert by_name(spans, "agentspec.llm.gpt-served").status.status_code != StatusCode.ERROR
    exported = json.dumps([[dict(s.attributes), s.status.description, [(e.name, dict(e.attributes)) for e in s.events]]
                           for s in spans.get_finished_spans()], default=str)
    assert SECRET_ERROR not in exported
    assert "Traceback" not in exported


def test_llm_config_secrets_are_never_exported(spans):
    run_agent()
    exported = json.dumps([dict(s.attributes) for s in spans.get_finished_spans()], default=str)
    assert SECRET_KEY not in exported
    assert "llm.test" not in exported


# --- content capture rules -----------------------------------------------------------------------

def test_hide_inputs_and_outputs(instrument):
    spans = instrument(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    run_agent()
    for span in spans.get_finished_spans():
        a = attrs(span)
        assert "input.value" not in a and "output.value" not in a, span.name
    llm = attrs(by_name(spans, "agentspec.llm.gpt-served"))
    assert llm["gen_ai.usage.input_tokens"] == 11
    assert llm["gen_ai.request.model"] == "gpt-served"
    assert "gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.arguments" not in llm
    assert llm["gen_ai.output.messages.0.message.tool_calls.0.tool_call.function.name"] == "lookup"


def test_hide_outputs_only_keeps_inputs(instrument):
    spans = instrument(config=TraceConfig(hide_outputs=True))
    run_agent()
    llm = attrs(by_name(spans, "agentspec.llm.gpt-served"))
    assert "input.value" in llm and "output.value" not in llm


def test_masking_sensitive_information_withholds_every_sensitive_field(instrument):
    spans = instrument(mask_sensitive_information=True)
    run_agent()
    exported = json.dumps([dict(s.attributes) for s in spans.get_finished_spans()], default=str)
    for text in ("Capital of Japan?", "Tokyo", "Japan"):
        assert text not in exported
    assert attrs(by_name(spans, "agentspec.llm.gpt-served"))["gen_ai.usage.input_tokens"] == 11


# --- outermost only, coexistence, lifecycle ---------------------------------------------------------

def test_instrumenting_twice_does_not_duplicate_spans(provider, spans):
    from traceai_agentspec import AgentSpecInstrumentor

    AgentSpecInstrumentor().instrument(tracer_provider=provider)
    run_agent()
    assert len(spans.get_finished_spans()) == 3


def test_an_already_attached_processor_is_not_added_twice(spans):
    from traceai_agentspec import FIAgentSpecSpanProcessor

    trace = Trace(name="x")
    existing = [p for p in trace.span_processors if isinstance(p, FIAgentSpecSpanProcessor)]
    assert len(existing) == 1
    trace2 = Trace(name="y", span_processors=list(trace.span_processors))
    assert len([p for p in trace2.span_processors if isinstance(p, FIAgentSpecSpanProcessor)]) == 1


class Recorder(SpanProcessor):
    def __init__(self):
        super().__init__()
        self.started, self.ended, self.events = [], [], []

    def on_start(self, span): self.started.append(span.name)
    async def on_start_async(self, span): self.on_start(span)
    def on_end(self, span): self.ended.append(span.name)
    async def on_end_async(self, span): self.on_end(span)
    def on_event(self, event, span): self.events.append(event.name)
    async def on_event_async(self, event, span): self.on_event(event, span)
    def startup(self): pass
    async def startup_async(self): pass
    def shutdown(self): pass
    async def shutdown_async(self): pass


def test_user_processors_keep_working_and_the_users_list_is_not_modified(spans):
    recorder = Recorder()
    mine = [recorder]
    run_agent({"span_processors": mine})
    assert mine == [recorder]
    assert "AgentExecutionSpan" in recorder.started and "AgentExecutionStart" in recorder.events
    assert len(spans.get_finished_spans()) == 3


def test_uninstrument_restores_pyagentspec_and_stops_tracing(provider):
    from traceai_agentspec import AgentSpecInstrumentor

    original = Trace.__init__
    instrumentor = AgentSpecInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    assert Trace.__init__ is not original
    instrumentor.uninstrument()
    assert Trace.__init__ is original
    run_agent()
    assert provider.exporter.get_finished_spans() == ()


def test_suppressed_instrumentation_creates_no_spans(spans):
    token = context_api.attach(context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        run_agent()
    finally:
        context_api.detach(token)
    assert spans.get_finished_spans() == ()


def test_spans_join_the_callers_active_otel_span(provider, spans):
    with provider.get_tracer("app").start_as_current_span("request") as request:
        run_agent()
    agent = by_name(spans, "agentspec.agent.geo-helper")
    assert agent.parent.span_id == request.get_span_context().span_id
    assert agent.context.trace_id == request.get_span_context().trace_id


def test_spans_still_open_when_the_trace_closes_are_ended(spans):
    with Trace(name="leaky") as trace:
        span = AgentExecutionSpan(agent=AGENT)
        span.start()
        span.add_event(AgentExecutionStart(agent=AGENT, inputs={"q": 1}))
        # the runtime forgets to end it; the trace context exits
    finished = spans.get_finished_spans()
    assert [s.name for s in finished] == ["agentspec.agent.geo-helper"]
    assert attrs(finished[0])["agentspec.span.unfinished"] is True


# --- tracing failures never break the user's call -------------------------------------------------

def test_a_failing_tracer_does_not_break_the_run(provider, instrument, monkeypatch):
    instrument()
    from traceai_agentspec import FIAgentSpecSpanProcessor

    def explode(*args, **kwargs):
        raise RuntimeError("tracer is down")

    monkeypatch.setattr(FIAgentSpecSpanProcessor, "_start_span", explode, raising=False)
    result = []
    with Trace(name="r"):
        with AgentExecutionSpan(agent=AGENT) as span:
            span.add_event(AgentExecutionStart(agent=AGENT, inputs={}))
            result.append("ran")
        with LlmGenerationSpan(llm_config=LLM) as llm_span:
            llm_span.add_event(LlmGenerationResponse(llm_config=LLM, content="x", request_id="r"))
            result.append("llm")
    assert result == ["ran", "llm"]


def test_a_failing_tracer_does_not_replace_the_users_own_exception(provider, instrument, monkeypatch):
    instrument()
    from traceai_agentspec import FIAgentSpecSpanProcessor

    def explode(*args, **kwargs):
        raise RuntimeError("tracer is down")

    monkeypatch.setattr(FIAgentSpecSpanProcessor, "_start_span", explode, raising=False)
    error = Boom("user error")
    with pytest.raises(Boom) as raised:
        with Trace(name="r"):
            with AgentExecutionSpan(agent=AGENT):
                raise error
    assert raised.value is error


def test_an_unknown_event_or_span_type_is_ignored(spans):
    from pyagentspec.tracing.events.event import Event
    from pyagentspec.tracing.spans.span import Span

    class Custom(Span):
        pass

    with Trace(name="r"):
        with Custom() as custom:
            custom.add_event(Event(name="whatever"))
    assert spans.get_finished_spans() == ()
