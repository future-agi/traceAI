"""session.id on Agent Framework spans.

Uses a real SDK TracerProvider so ``on_start`` and ``on_end`` both run, and
starts spans the way the framework does (``observability._get_span``): bare,
with the attributes set right after.
"""

from contextlib import contextmanager

import pytest
from fi_instrumentation import using_session, using_user
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from traceai_agent_framework import AgentFrameworkSpanProcessor

_AGENT = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "a"}
_CHAT = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "m"}
_TOOL = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "t"}


@pytest.fixture
def env():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = AgentFrameworkSpanProcessor()
    provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, provider.get_tracer("agent_framework"), exporter, processor


@contextmanager
def _af_span(tracer, name, attrs, context=None):
    span = tracer.start_span(name, context=context)
    span.set_attributes(attrs)
    with trace_api.use_span(span, end_on_exit=True):
        yield span


def _sessions(exporter):
    return {s.name: s.attributes.get("session.id") for s in exporter.get_finished_spans()}


def test_per_call_conversation_ids_do_not_become_sessions(env):
    # Responses API tool loop: each chat call gets the previous response id as
    # its conversation id. Copying those gave every LLM call its own session.
    _, tracer, exporter, _ = env
    with _af_span(tracer, "invoke_agent a", _AGENT):
        for i in range(2):
            with _af_span(tracer, f"chat {i}", {**_CHAT, "gen_ai.conversation.id": f"resp_{i}"}):
                pass
    assert set(_sessions(exporter).values()) == {None}


def test_run_conversation_id_is_the_session_of_the_whole_run(env):
    _, tracer, exporter, _ = env
    with _af_span(tracer, "invoke_agent a", {**_AGENT, "gen_ai.conversation.id": "conv_1"}):
        with _af_span(tracer, "chat m", {**_CHAT, "gen_ai.conversation.id": "resp_9"}):
            pass
        with _af_span(tracer, "execute_tool t", _TOOL):
            pass
    assert _sessions(exporter) == {
        "chat m": "conv_1",
        "execute_tool t": "conv_1",
        "invoke_agent a": "conv_1",
    }


def test_workflow_conversation_id_reaches_nested_agents(env):
    _, tracer, exporter, _ = env
    with _af_span(tracer, "workflow.run", {"workflow.id": "wf", "gen_ai.conversation.id": "conv_w"}):
        with _af_span(tracer, "executor.process", {"executor.id": "e1"}):
            with _af_span(tracer, "invoke_agent a", _AGENT):
                with _af_span(tracer, "chat m", _CHAT):
                    pass
    assert set(_sessions(exporter).values()) == {"conv_w"}


def test_session_is_inherited_through_foreign_spans(env):
    provider, tracer, exporter, _ = env
    other = provider.get_tracer("httpx")
    with _af_span(tracer, "invoke_agent a", {**_AGENT, "gen_ai.conversation.id": "conv_1"}):
        with other.start_as_current_span("POST"):
            with _af_span(tracer, "chat m", _CHAT):
                pass
    sessions = _sessions(exporter)
    assert sessions["chat m"] == "conv_1"
    assert sessions["POST"] is None  # foreign spans are not modified


def test_using_session_and_user_reach_framework_spans(env):
    _, tracer, exporter, _ = env
    with using_session("sess-1"), using_user("user-1"):
        with _af_span(tracer, "invoke_agent a", {**_AGENT, "gen_ai.conversation.id": "conv_1"}):
            with _af_span(tracer, "chat m", _CHAT):
                pass
    spans = exporter.get_finished_spans()
    assert {s.attributes["session.id"] for s in spans} == {"sess-1"}
    assert {s.attributes["user.id"] for s in spans} == {"user-1"}


def test_using_session_does_not_touch_foreign_spans(env):
    provider, _, exporter, _ = env
    with using_session("sess-1"):
        with provider.get_tracer("other").start_as_current_span("x"):
            pass
    assert "session.id" not in exporter.get_finished_spans()[0].attributes


def test_existing_session_id_is_not_overwritten(env):
    _, tracer, exporter, _ = env
    attrs = {**_AGENT, "gen_ai.conversation.id": "conv_1", "session.id": "already"}
    with _af_span(tracer, "invoke_agent a", attrs):
        with _af_span(tracer, "chat m", _CHAT):
            pass
    assert set(_sessions(exporter).values()) == {"already"}


def test_no_session_without_using_session_or_conversation_id(env):
    _, tracer, exporter, _ = env
    with _af_span(tracer, "invoke_agent a", _AGENT):
        with _af_span(tracer, "chat m", _CHAT):
            pass
    assert set(_sessions(exporter).values()) == {None}


def test_session_state_is_released_when_spans_end(env):
    _, tracer, _, processor = env
    with _af_span(tracer, "invoke_agent a", {**_AGENT, "gen_ai.conversation.id": "conv_1"}):
        with _af_span(tracer, "chat m", _CHAT):
            assert len(processor._open) == 2
    assert processor._open == {}
    assert processor._inherited == {}


def test_remote_parent_does_not_leak_bubble_state(env):
    # A server that continues an incoming trace: the parent span is remote and
    # never ends here, so nothing may be kept for it.
    _, tracer, _, processor = env
    remote = trace_api.SpanContext(
        trace_id=0x1, span_id=0x2, is_remote=True,
        trace_flags=trace_api.TraceFlags(trace_api.TraceFlags.SAMPLED),
    )
    ctx = trace_api.set_span_in_context(trace_api.NonRecordingSpan(remote))
    with _af_span(tracer, "invoke_agent a", _AGENT, context=ctx):
        pass
    assert processor._desc_io == {}
