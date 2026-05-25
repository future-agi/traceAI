"""Unit tests for the attribute mapping helpers in processor."""

import json
from pathlib import Path

import pytest

from traceai_agent_framework.processor import (
    AgentFrameworkSpanProcessor,
    _classify_span_kind,
    _flatten_messages,
    _map_attributes_to_fi_conventions,
)


FIXTURES = json.loads(
    (Path(__file__).parent / "_fixtures" / "sample_spans.json").read_text()
)


def _fixture(name: str) -> dict:
    for f in FIXTURES:
        if f["name"] == name:
            return dict(f["attributes"])
    raise KeyError(f"fixture not found: {name}")


# ---------------------------------------------------------------------------
# Pass-through behaviour
# ---------------------------------------------------------------------------


def test_empty_attributes_returns_empty():
    assert _map_attributes_to_fi_conventions({}) == {}


def test_non_agent_framework_span_passes_through_untouched():
    src = {"foo": "bar", "http.method": "POST", "custom.attr": 42}
    out = _map_attributes_to_fi_conventions(src)
    assert out == src
    assert "gen_ai.span.kind" not in out


# ---------------------------------------------------------------------------
# Span-kind classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op,expected",
    [
        ("chat", "LLM"),
        ("embeddings", "EMBEDDING"),
        ("execute_tool", "TOOL"),
        ("invoke_agent", "AGENT"),
        ("create_agent", "AGENT"),
    ],
)
def test_classify_by_operation_name(op, expected):
    assert _classify_span_kind({"gen_ai.operation.name": op}) == expected


@pytest.mark.parametrize(
    "key",
    [
        "workflow.id",
        "workflow_builder.name",
        "executor.id",
        "edge_group.id",
        "message.type",
        "message.source_id",
    ],
)
def test_classify_workflow_attrs_as_chain(key):
    assert _classify_span_kind({key: "value"}) == "CHAIN"


def test_classify_unknown_returns_none():
    assert _classify_span_kind({"random.key": "value"}) is None


# ---------------------------------------------------------------------------
# LLM enrichment (synthetic chat span; fixtures don't include chat)
# ---------------------------------------------------------------------------


def _make_chat_attrs(input_msgs, output_msgs, input_tokens=None, output_tokens=None):
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.input.messages": json.dumps(input_msgs),
        "gen_ai.output.messages": json.dumps(output_msgs),
    }
    if input_tokens is not None:
        attrs["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        attrs["gen_ai.usage.output_tokens"] = output_tokens
    return attrs


def test_llm_lifts_messages_to_input_output_value():
    attrs = _make_chat_attrs(
        [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}],
        [{"role": "assistant", "parts": [{"type": "text", "content": "hello"}]}],
    )
    out = _map_attributes_to_fi_conventions(attrs)
    assert out["gen_ai.span.kind"] == "LLM"
    assert out["input.value"] == attrs["gen_ai.input.messages"]
    assert out["input.mime_type"] == "application/json"
    assert out["output.value"] == attrs["gen_ai.output.messages"]
    assert out["output.mime_type"] == "application/json"


def test_llm_flattens_messages_with_indexed_keys():
    attrs = _make_chat_attrs(
        [
            {"role": "system", "parts": [{"type": "text", "content": "be terse"}]},
            {"role": "user", "parts": [{"type": "text", "content": "hi"}]},
        ],
        [{"role": "assistant", "parts": [{"type": "text", "content": "ok"}]}],
    )
    out = _map_attributes_to_fi_conventions(attrs)
    assert out["gen_ai.input.messages.0.message.role"] == "system"
    assert out["gen_ai.input.messages.0.message.content"] == "be terse"
    assert out["gen_ai.input.messages.1.message.role"] == "user"
    assert out["gen_ai.input.messages.1.message.content"] == "hi"
    assert out["gen_ai.output.messages.0.message.role"] == "assistant"
    assert out["gen_ai.output.messages.0.message.content"] == "ok"


def test_llm_derives_total_tokens():
    attrs = _make_chat_attrs([], [], input_tokens=42, output_tokens=17)
    out = _map_attributes_to_fi_conventions(attrs)
    assert out["gen_ai.usage.total_tokens"] == 59


def test_llm_skips_total_tokens_when_one_missing():
    attrs = _make_chat_attrs([], [], input_tokens=42)
    out = _map_attributes_to_fi_conventions(attrs)
    assert "gen_ai.usage.total_tokens" not in out


def test_llm_preserves_existing_total_tokens():
    attrs = _make_chat_attrs([], [], input_tokens=10, output_tokens=10)
    attrs["gen_ai.usage.total_tokens"] = 999  # pre-set by some other source
    out = _map_attributes_to_fi_conventions(attrs)
    assert out["gen_ai.usage.total_tokens"] == 999


def test_llm_without_messages_still_stamps_kind():
    out = _map_attributes_to_fi_conventions({"gen_ai.operation.name": "chat"})
    assert out["gen_ai.span.kind"] == "LLM"
    assert "input.value" not in out


# ---------------------------------------------------------------------------
# TOOL enrichment (uses real fixture)
# ---------------------------------------------------------------------------


def test_tool_lifts_args_and_result_from_fixture():
    out = _map_attributes_to_fi_conventions(_fixture("execute_tool get_weather"))
    assert out["gen_ai.span.kind"] == "TOOL"
    assert out["input.value"] == '{"city": "Paris"}'
    assert out["input.mime_type"] == "application/json"
    assert "Paris is sunny" in out["output.value"]
    assert out["output.mime_type"] == "text/plain"


def test_tool_json_string_result_detected_as_json_mime():
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.call.result": '{"temp": 22}',
    }
    out = _map_attributes_to_fi_conventions(attrs)
    assert out["output.value"] == '{"temp": 22}'
    assert out["output.mime_type"] == "application/json"


def test_tool_with_no_args_or_result_just_stamps_kind():
    attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "noop"}
    out = _map_attributes_to_fi_conventions(attrs)
    assert out["gen_ai.span.kind"] == "TOOL"
    assert "input.value" not in out
    assert "output.value" not in out


# ---------------------------------------------------------------------------
# AGENT enrichment (uses real fixture)
# ---------------------------------------------------------------------------


def test_agent_from_fixture_flattens_messages():
    out = _map_attributes_to_fi_conventions(_fixture("invoke_agent weather_agent"))
    assert out["gen_ai.span.kind"] == "AGENT"
    assert out["gen_ai.input.messages.0.message.role"] == "user"
    assert out["gen_ai.input.messages.0.message.content"] == "What's the weather in Paris?"
    assert out["gen_ai.output.messages.0.message.role"] == "assistant"
    assert out["gen_ai.output.messages.0.message.content"] == "The weather in Paris is sunny."


def test_agent_preserves_native_gen_ai_attrs():
    out = _map_attributes_to_fi_conventions(_fixture("invoke_agent weather_agent"))
    assert out["gen_ai.agent.name"] == "weather_agent"
    assert out["gen_ai.agent.id"]
    assert out["gen_ai.provider.name"] == "microsoft.agent_framework"


# ---------------------------------------------------------------------------
# CHAIN enrichment (uses real fixtures)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "workflow.build",
        "workflow.run",
        "executor.process upper",
        "executor.process exclaim",
        "edge_group.process SingleEdgeGroup",
        "message.send",
    ],
)
def test_chain_kinds_from_fixtures(name):
    out = _map_attributes_to_fi_conventions(_fixture(name))
    assert out["gen_ai.span.kind"] == "CHAIN"
    assert "input.value" not in out
    assert "output.value" not in out


# ---------------------------------------------------------------------------
# Message-flattener edge cases (the framework's parts-shape varies)
# ---------------------------------------------------------------------------


def test_flatten_handles_malformed_json():
    assert _flatten_messages("not json", "gen_ai.input.messages") == {}


def test_flatten_handles_non_list_json():
    assert _flatten_messages('{"role": "user"}', "gen_ai.input.messages") == {}


def test_flatten_handles_empty_parts():
    out = _flatten_messages(
        json.dumps([{"role": "user", "parts": []}]),
        "gen_ai.input.messages",
    )
    assert out["gen_ai.input.messages.0.message.role"] == "user"
    assert "gen_ai.input.messages.0.message.content" not in out


def test_flatten_skips_non_text_parts():
    """Image/reasoning/function-call parts are not text-flattened in MVP."""
    out = _flatten_messages(
        json.dumps(
            [
                {
                    "role": "user",
                    "parts": [
                        {"type": "uri", "uri": "https://example.com/img.png"},
                        {"type": "text", "content": "look at this"},
                    ],
                }
            ]
        ),
        "gen_ai.input.messages",
    )
    assert out["gen_ai.input.messages.0.message.content"] == "look at this"


def test_flatten_joins_multiple_text_parts():
    out = _flatten_messages(
        json.dumps(
            [{"role": "user", "parts": [
                {"type": "text", "content": "line one"},
                {"type": "text", "content": "line two"},
            ]}]
        ),
        "gen_ai.input.messages",
    )
    assert out["gen_ai.input.messages.0.message.content"] == "line one\nline two"


def test_llm_malformed_messages_does_not_crash():
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.input.messages": "definitely not json {{{",
    }
    out = _map_attributes_to_fi_conventions(attrs)
    # input.value is still lifted (it's just the raw string)
    assert out["gen_ai.span.kind"] == "LLM"
    assert out["input.value"] == "definitely not json {{{"
    # but no flattened keys
    assert "gen_ai.input.messages.0.message.role" not in out


# ---------------------------------------------------------------------------
# AgentFrameworkSpanProcessor: per-span enrichment + chain bubble-up
# ---------------------------------------------------------------------------


class _FakeScope:
    def __init__(self, name="agent_framework"):
        self.name = name


class _FakeCtx:
    def __init__(self, span_id):
        self.span_id = span_id


class _FakeReadableSpan:
    """Minimal stand-in for a ReadableSpan with the attributes our processor reads."""

    def __init__(self, span_id, parent_id, start_time, end_time, attrs,
                 scope_name="agent_framework"):
        self._attributes = dict(attrs) if attrs else {}
        self.start_time = start_time
        self.end_time = end_time
        self.context = _FakeCtx(span_id)
        self.parent = _FakeCtx(parent_id) if parent_id is not None else None
        self.instrumentation_scope = _FakeScope(scope_name)


def test_processor_mutates_attrs_in_on_end():
    processor = AgentFrameworkSpanProcessor()
    span = _FakeReadableSpan(
        span_id=1, parent_id=None, start_time=0, end_time=100,
        attrs={"gen_ai.operation.name": "execute_tool",
               "gen_ai.tool.call.arguments": '{"x": 1}'},
    )
    processor.on_end(span)
    assert span._attributes["gen_ai.span.kind"] == "TOOL"
    assert span._attributes["input.value"] == '{"x": 1}'


def test_processor_ignores_non_agent_framework_spans():
    """Spans from other instrumentation scopes pass through untouched."""
    processor = AgentFrameworkSpanProcessor()
    span = _FakeReadableSpan(
        span_id=1, parent_id=None, start_time=0, end_time=100,
        attrs={"http.method": "POST"},
        scope_name="opentelemetry.instrumentation.requests",
    )
    processor.on_end(span)
    assert "gen_ai.span.kind" not in span._attributes
    assert span._attributes == {"http.method": "POST"}


def test_processor_continues_on_individual_span_failure():
    """One broken span shouldn't crash the processor for others."""
    processor = AgentFrameworkSpanProcessor()

    class _Broken:
        instrumentation_scope = _FakeScope("agent_framework")
        @property
        def _attributes(self):
            raise RuntimeError("boom")

    good = _FakeReadableSpan(
        span_id=1, parent_id=None, start_time=0, end_time=100,
        attrs={"gen_ai.operation.name": "execute_tool"},
    )
    processor.on_end(_Broken())  # must not raise
    processor.on_end(good)
    assert good._attributes["gen_ai.span.kind"] == "TOOL"


def test_chain_io_bubbles_up_from_descendant():
    """
    Tree (events arrive in end order: deepest first, root last):
      workflow.run (CHAIN)
        └── executor.process (CHAIN)
              └── invoke_agent (AGENT, has I/O)
    """
    processor = AgentFrameworkSpanProcessor()

    agent = _FakeReadableSpan(
        span_id=3, parent_id=2, start_time=20, end_time=80,
        attrs={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.input.messages": '[{"role":"user","parts":[{"type":"text","content":"hi"}]}]',
            "gen_ai.output.messages": '[{"role":"assistant","parts":[{"type":"text","content":"hello"}]}]',
        },
    )
    executor = _FakeReadableSpan(
        span_id=2, parent_id=1, start_time=10, end_time=90,
        attrs={"executor.id": "step1"},
    )
    workflow = _FakeReadableSpan(
        span_id=1, parent_id=None, start_time=0, end_time=100,
        attrs={"workflow.id": "wf-1"},
    )

    processor.on_end(agent)
    processor.on_end(executor)
    processor.on_end(workflow)

    # workflow.run got the bubbled input/output from its grandchild agent
    assert workflow._attributes["gen_ai.span.kind"] == "CHAIN"
    assert workflow._attributes["input.value"].startswith("[{")
    assert workflow._attributes["output.value"].startswith("[{")
    # executor.process also got them (still a chain)
    assert executor._attributes["gen_ai.span.kind"] == "CHAIN"
    assert "input.value" in executor._attributes
    assert "output.value" in executor._attributes


def test_chain_picks_earliest_input_latest_output_across_siblings():
    """Two descendants under one CHAIN parent: earliest input + latest output win."""
    processor = AgentFrameworkSpanProcessor()

    # Both children end before the parent (workflow). Earliest by start_time wins
    # for input; latest by end_time wins for output.
    early = _FakeReadableSpan(
        span_id=2, parent_id=1, start_time=10, end_time=50,
        attrs={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.input.messages": '[{"role":"user","parts":[{"type":"text","content":"first"}]}]',
            "gen_ai.output.messages": '[{"role":"assistant","parts":[{"type":"text","content":"first-out"}]}]',
        },
    )
    late = _FakeReadableSpan(
        span_id=3, parent_id=1, start_time=100, end_time=180,
        attrs={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.input.messages": '[{"role":"user","parts":[{"type":"text","content":"second"}]}]',
            "gen_ai.output.messages": '[{"role":"assistant","parts":[{"type":"text","content":"final-out"}]}]',
        },
    )
    workflow = _FakeReadableSpan(
        span_id=1, parent_id=None, start_time=0, end_time=200,
        attrs={"workflow.id": "wf-1"},
    )

    processor.on_end(early)
    processor.on_end(late)
    processor.on_end(workflow)

    # Earliest input (start_time=10) should be "first"
    assert "first" in workflow._attributes["input.value"]
    # Latest output (end_time=180) should be "final-out"
    assert "final-out" in workflow._attributes["output.value"]


def test_chain_with_no_descendants_seen_stays_without_io():
    """If we never saw the chain's children (e.g. across batches), no bubble."""
    processor = AgentFrameworkSpanProcessor()
    chain = _FakeReadableSpan(
        span_id=1, parent_id=None, start_time=0, end_time=100,
        attrs={"workflow.id": "wf-1"},
    )
    processor.on_end(chain)
    assert chain._attributes["gen_ai.span.kind"] == "CHAIN"
    assert "input.value" not in chain._attributes
    assert "output.value" not in chain._attributes


def test_chain_existing_io_not_overridden_by_bubble():
    """If the chain span somehow already has I/O, the bubble must not override it."""
    processor = AgentFrameworkSpanProcessor()

    child = _FakeReadableSpan(
        span_id=2, parent_id=1, start_time=10, end_time=50,
        attrs={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.input.messages": '[{"role":"user","parts":[{"type":"text","content":"child"}]}]',
            "gen_ai.output.messages": '[{"role":"assistant","parts":[{"type":"text","content":"child-out"}]}]',
        },
    )
    chain = _FakeReadableSpan(
        span_id=1, parent_id=None, start_time=0, end_time=100,
        attrs={
            "workflow.id": "wf-1",
            "input.value": "already set",
            "output.value": "already set",
        },
    )

    processor.on_end(child)
    processor.on_end(chain)

    assert chain._attributes["input.value"] == "already set"
    assert chain._attributes["output.value"] == "already set"


def test_processor_shutdown_clears_state():
    processor = AgentFrameworkSpanProcessor()
    child = _FakeReadableSpan(
        span_id=2, parent_id=999, start_time=10, end_time=50,
        attrs={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.input.messages": '[{"role":"user","parts":[{"type":"text","content":"x"}]}]',
        },
    )
    processor.on_end(child)
    assert 999 in processor._desc_io
    processor.shutdown()
    assert processor._desc_io == {}
    # After shutdown, further on_end calls are silently no-op.
    after = _FakeReadableSpan(
        span_id=3, parent_id=None, start_time=0, end_time=100,
        attrs={"gen_ai.operation.name": "execute_tool"},
    )
    processor.on_end(after)
    assert "gen_ai.span.kind" not in after._attributes
