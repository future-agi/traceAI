"""Unit tests for the AG2 Classic attribute mapping (no autogen run needed)."""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_ag2_classic import (
    AG2_SCOPE,
    CONTENT_KEYS,
    KIND_BY_SPAN_TYPE,
    AG2ClassicSpanProcessor,
    kind_for_span_type,
    map_ag2_attributes,
)


# AC-02: kind map from the upstream SpanType enum ---------------------------------


EXPECTED_KINDS = {
    "conversation": "CHAIN",
    "multi_conversation": "CHAIN",
    "agent": "AGENT",
    "llm": "LLM",
    "tool": "TOOL",
    "handoff": "CHAIN",
    "speaker_selection": "CHAIN",
    "human_input": "CHAIN",
    "code_execution": "TOOL",
}


def test_kind_map_matches_architecture_table():
    assert KIND_BY_SPAN_TYPE == EXPECTED_KINDS


def test_kind_map_covers_every_installed_span_type():
    consts = pytest.importorskip("autogen.opentelemetry.consts")
    installed = {member.value for member in consts.SpanType}
    assert installed == set(KIND_BY_SPAN_TYPE)
    for value in installed:
        assert kind_for_span_type(value) == EXPECTED_KINDS[value]


def test_scope_name_matches_installed_consts():
    consts = pytest.importorskip("autogen.opentelemetry.consts")
    assert consts.INSTRUMENTING_MODULE_NAME == AG2_SCOPE


def test_unknown_or_missing_span_type_is_left_alone():
    attrs = {"gen_ai.operation.name": "chat", "gen_ai.input.messages": "[]"}
    assert map_ag2_attributes(attrs) == attrs
    assert map_ag2_attributes({"ag2.span.type": "nope"}) == {"ag2.span.type": "nope"}
    assert kind_for_span_type(None) is None


# Usage, cost, request parameters -------------------------------------------------


def test_llm_span_gets_total_tokens_cost_and_parameters():
    attrs = {
        "ag2.span.type": "llm",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "gpt-4o-mini",
        "gen_ai.usage.input_tokens": 11,
        "gen_ai.usage.output_tokens": 7,
        "gen_ai.usage.cost": 0.0001,
        "gen_ai.request.temperature": 0.2,
        "gen_ai.request.max_tokens": 64,
    }
    mapped = map_ag2_attributes(attrs)
    assert mapped["gen_ai.span.kind"] == "LLM"
    assert mapped["gen_ai.usage.total_tokens"] == 18
    assert mapped["gen_ai.cost.total"] == 0.0001
    assert mapped["gen_ai.usage.cost"] == 0.0001  # upstream key kept
    assert json.loads(mapped["gen_ai.request.parameters"]) == {"temperature": 0.2, "max_tokens": 64}


def test_llm_span_without_usage_records_no_tokens():
    mapped = map_ag2_attributes({"ag2.span.type": "llm", "gen_ai.operation.name": "chat"})
    assert "gen_ai.usage.total_tokens" not in mapped
    assert "gen_ai.cost.total" not in mapped


def test_conversation_aggregate_usage_is_not_relabelled():
    attrs = {
        "ag2.span.type": "conversation",
        "gen_ai.usage.input_tokens": 22,
        "gen_ai.usage.output_tokens": 14,
        "gen_ai.usage.cost": 0.0002,
    }
    mapped = map_ag2_attributes(attrs)
    assert "gen_ai.usage.total_tokens" not in mapped
    assert "gen_ai.cost.total" not in mapped
    # The chat-wide cost moves off the gen_ai.usage.* namespace like the tokens.
    assert "gen_ai.usage.cost" not in mapped
    assert mapped["ag2.usage.cost"] == 0.0002


@pytest.mark.parametrize("span_type", sorted(set(EXPECTED_KINDS) - {"llm"}))
def test_no_gen_ai_usage_key_survives_on_a_non_llm_span(span_type):
    attrs = {
        "ag2.span.type": span_type,
        "gen_ai.usage.input_tokens": 22,
        "gen_ai.usage.output_tokens": 14,
        "gen_ai.usage.total_tokens": 36,
        "gen_ai.usage.cost": 0.0002,
    }
    mapped = map_ag2_attributes(attrs)
    assert not [key for key in mapped if key.startswith("gen_ai.usage.") or key == "gen_ai.cost.total"]
    assert mapped["ag2.usage.total_tokens"] == 36


def test_conversation_aggregate_tokens_do_not_double_count_the_trace():
    """chat.py:81-82 puts the chat-wide sum on the conversation span.

    fi-collector promotes gen_ai.usage.* on any span and Observe sums
    total_tokens over a trace, so only LLM spans keep the promoted keys.
    """
    attrs = {
        "ag2.span.type": "conversation",
        "gen_ai.usage.input_tokens": 22,
        "gen_ai.usage.output_tokens": 14,
    }
    mapped = map_ag2_attributes(attrs)
    assert "gen_ai.usage.input_tokens" not in mapped
    assert "gen_ai.usage.output_tokens" not in mapped
    assert mapped["ag2.usage.input_tokens"] == 22
    assert mapped["ag2.usage.output_tokens"] == 14


# Session ----------------------------------------------------------------------


def test_root_conversation_id_becomes_session_id():
    mapped = map_ag2_attributes({"ag2.span.type": "conversation", "gen_ai.conversation.id": "123"})
    assert mapped["session.id"] == "123"


def test_nested_conversation_id_is_not_a_session():
    mapped = map_ag2_attributes(
        {"ag2.span.type": "conversation", "gen_ai.conversation.id": "456"}, root_conversation=False
    )
    assert "session.id" not in mapped
    assert mapped["gen_ai.conversation.id"] == "456"


def test_existing_session_id_wins():
    mapped = map_ag2_attributes(
        {"ag2.span.type": "conversation", "gen_ai.conversation.id": "1", "session.id": "mine"}
    )
    assert mapped["session.id"] == "mine"


# AC-06 content default ------------------------------------------------------------


CONTENT_SAMPLE = {
    "gen_ai.input.messages": '[{"role": "user", "parts": [{"type": "text", "content": "secret"}]}]',
    "gen_ai.output.messages": '[{"role": "assistant", "parts": []}]',
    "gen_ai.tool.call.arguments": '{"city": "secret"}',
    "gen_ai.tool.call.result": "secret result",
    "ag2.human_input.prompt": "secret prompt",
    "ag2.human_input.response": "secret answer",
    "ag2.code_execution.output": "secret output",
    "ag2.chats.summaries": '["secret summary"]',
}


@pytest.mark.parametrize("span_type", sorted(EXPECTED_KINDS))
def test_content_is_dropped_by_default(span_type):
    attrs = dict(CONTENT_SAMPLE, **{"ag2.span.type": span_type, "gen_ai.agent.name": "a"})
    mapped = map_ag2_attributes(attrs)
    for key in CONTENT_KEYS:
        assert key not in mapped
    assert "secret" not in json.dumps(mapped)
    assert mapped["gen_ai.agent.name"] == "a"


def test_capture_on_lifts_messages_to_input_output():
    attrs = {
        "ag2.span.type": "agent",
        "gen_ai.input.messages": CONTENT_SAMPLE["gen_ai.input.messages"],
        "gen_ai.output.messages": CONTENT_SAMPLE["gen_ai.output.messages"],
    }
    mapped = map_ag2_attributes(attrs, capture_content=True)
    assert mapped["input.value"] == attrs["gen_ai.input.messages"]
    assert mapped["input.mime_type"] == "application/json"
    assert mapped["output.value"] == attrs["gen_ai.output.messages"]
    assert mapped["gen_ai.input.messages"] == attrs["gen_ai.input.messages"]


def test_capture_on_tool_human_and_code_spans():
    tool = map_ag2_attributes(
        {"ag2.span.type": "tool", "gen_ai.tool.call.arguments": '{"a": 1}', "gen_ai.tool.call.result": "ok"},
        capture_content=True,
    )
    assert tool["input.value"] == '{"a": 1}' and tool["input.mime_type"] == "application/json"
    assert tool["output.value"] == "ok" and tool["output.mime_type"] == "text/plain"

    human = map_ag2_attributes(
        {"ag2.span.type": "human_input", "ag2.human_input.prompt": "p", "ag2.human_input.response": "r"},
        capture_content=True,
    )
    assert (human["input.value"], human["output.value"]) == ("p", "r")

    code = map_ag2_attributes(
        {"ag2.span.type": "code_execution", "ag2.code_execution.output": "42"}, capture_content=True
    )
    assert code["output.value"] == "42"
    assert code["gen_ai.span.kind"] == "TOOL"


# The processor on a real SDK pipeline -------------------------------------------


def _pipeline(capture_content=False):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = AG2ClassicSpanProcessor(capture_content=capture_content)
    provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_processor_maps_only_the_ag2_scope():
    provider, exporter = _pipeline()
    ag2 = provider.get_tracer(AG2_SCOPE)
    other = provider.get_tracer("someone.else")
    with ag2.start_as_current_span("chat m") as span:
        span.set_attribute("ag2.span.type", "llm")
        span.set_attribute("gen_ai.input.messages", "secret")
    with other.start_as_current_span("x") as span:
        span.set_attribute("ag2.span.type", "llm")
        span.set_attribute("gen_ai.input.messages", "kept")
    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert by_name["chat m"].attributes["gen_ai.span.kind"] == "LLM"
    assert "gen_ai.input.messages" not in by_name["chat m"].attributes
    assert "gen_ai.span.kind" not in by_name["x"].attributes
    assert by_name["x"].attributes["gen_ai.input.messages"] == "kept"


def test_processor_promotes_error_type_to_error_status():
    provider, exporter = _pipeline()
    tracer = provider.get_tracer(AG2_SCOPE)
    with tracer.start_as_current_span("execute_tool t") as span:
        span.set_attribute("ag2.span.type", "tool")
        span.set_attribute("error.type", "ExecutionError")
    with tracer.start_as_current_span("execute_tool ok") as span:
        span.set_attribute("ag2.span.type", "tool")
    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert by_name["execute_tool t"].status.status_code is StatusCode.ERROR
    assert by_name["execute_tool t"].status.description == "ExecutionError"
    assert by_name["execute_tool ok"].status.status_code is StatusCode.UNSET


def test_processor_keeps_session_on_outer_conversation_only():
    provider, exporter = _pipeline()
    tracer = provider.get_tracer(AG2_SCOPE)
    with tracer.start_as_current_span("conversation outer") as outer:
        outer.set_attribute("ag2.span.type", "conversation")
        with tracer.start_as_current_span("speaker_selection") as sel:
            sel.set_attribute("ag2.span.type", "speaker_selection")
            with tracer.start_as_current_span("conversation inner") as inner:
                inner.set_attribute("ag2.span.type", "conversation")
                inner.set_attribute("gen_ai.conversation.id", "inner-id")
        outer.set_attribute("gen_ai.conversation.id", "outer-id")
    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert by_name["conversation outer"].attributes["session.id"] == "outer-id"
    assert "session.id" not in by_name["conversation inner"].attributes
    assert by_name["speaker_selection"].attributes["gen_ai.span.kind"] == "CHAIN"


def test_non_ag2_span_between_conversations_does_not_split_the_session():
    """Agent-as-tool: the user's own span wraps an inner initiate_chat."""
    provider, exporter = _pipeline()
    processor = provider._active_span_processor._span_processors[0]
    ag2 = provider.get_tracer(AG2_SCOPE)
    user = provider.get_tracer("user.app")
    with ag2.start_as_current_span("conversation outer") as outer:
        outer.set_attribute("ag2.span.type", "conversation")
        with user.start_as_current_span("user work"):
            with user.start_as_current_span("user inner work"):
                with ag2.start_as_current_span("conversation inner") as inner:
                    inner.set_attribute("ag2.span.type", "conversation")
                    inner.set_attribute("gen_ai.conversation.id", "inner-id")
        outer.set_attribute("gen_ai.conversation.id", "outer-id")
    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert by_name["conversation outer"].attributes["session.id"] == "outer-id"
    assert "session.id" not in by_name["conversation inner"].attributes
    assert "session.id" not in by_name["user work"].attributes
    # Every tracked span, AG2 or not, is released when it ends.
    assert processor._live == {}


def test_live_span_tracking_is_bounded(monkeypatch):
    from traceai_ag2_classic import _processor

    monkeypatch.setattr(_processor, "_MAX_LIVE_SPANS", 2)
    provider, exporter = _pipeline()
    processor = provider._active_span_processor._span_processors[0]
    user = provider.get_tracer("user.app")
    with user.start_as_current_span("a"), user.start_as_current_span("b"), user.start_as_current_span("c"):
        assert len(processor._live) == 2
    assert processor._live == {}
    assert len(exporter.get_finished_spans()) == 3


# Context attributes (using_attributes / using_session / using_user) ------------


def test_context_attributes_are_copied_onto_ag2_spans():
    from fi_instrumentation import using_attributes

    provider, exporter = _pipeline()
    ag2 = provider.get_tracer(AG2_SCOPE)
    other = provider.get_tracer("someone.else")
    with using_attributes(session_id="my-session", user_id="u-1", metadata={"k": "v"}, tags=["t1"]):
        with ag2.start_as_current_span("conversation c") as conversation:
            conversation.set_attribute("ag2.span.type", "conversation")
            with ag2.start_as_current_span("chat m") as llm:
                llm.set_attribute("ag2.span.type", "llm")
            conversation.set_attribute("gen_ai.conversation.id", "chat-1")
        with other.start_as_current_span("x"):
            pass
    by_name = {s.name: s.attributes for s in exporter.get_finished_spans()}
    for name in ("conversation c", "chat m"):
        assert by_name[name]["session.id"] == "my-session", name
        assert by_name[name]["user.id"] == "u-1", name
        assert json.loads(by_name[name]["metadata"]) == {"k": "v"}, name
        assert tuple(by_name[name]["tag.tags"]) == ("t1",), name
    # The user's session wins over the upstream chat id; the chat id is kept.
    assert by_name["conversation c"]["gen_ai.conversation.id"] == "chat-1"
    # Spans from other instrumentations are left to their own tracer.
    assert "session.id" not in by_name["x"] and "user.id" not in by_name["x"]


def test_context_attributes_do_not_overwrite_upstream_values():
    from fi_instrumentation import using_session

    provider, exporter = _pipeline()
    ag2 = provider.get_tracer(AG2_SCOPE)
    with using_session("my-session"):
        with ag2.start_as_current_span(
            "conversation c", attributes={"ag2.span.type": "conversation", "gen_ai.conversation.id": "chat-1"}
        ):
            pass
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["session.id"] == "my-session"
    assert attrs["gen_ai.conversation.id"] == "chat-1"


def test_processor_preserves_bounded_attribute_limits():
    from opentelemetry.attributes import BoundedAttributes

    provider, exporter = _pipeline()
    with provider.get_tracer(AG2_SCOPE).start_as_current_span("chat") as span:
        span.set_attribute("ag2.span.type", "llm")
    finished = exporter.get_finished_spans()[0]
    assert isinstance(finished._attributes, BoundedAttributes)
    with pytest.raises(TypeError):
        finished._attributes["x"] = 1  # immutable after end, as the SDK leaves it


def test_processor_never_raises_into_the_sdk():
    provider, exporter = _pipeline()
    processor = provider._active_span_processor._span_processors[0]
    live = processor._live
    processor._live = None  # force an internal failure
    try:
        with provider.get_tracer(AG2_SCOPE).start_as_current_span("chat") as span:
            span.set_attribute("ag2.span.type", "llm")
    finally:
        processor._live = live
    assert [s.name for s in exporter.get_finished_spans()] == ["chat"]
