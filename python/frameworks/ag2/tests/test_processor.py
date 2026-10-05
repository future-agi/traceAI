"""Unit tests for AG2SpanProcessor on synthetic spans (AC-02, AC-03)."""

from __future__ import annotations

import logging

import pytest
from fi_instrumentation.instrumentation.config import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_ag2 import (
    AG2_INSTRUMENTATION_SCOPE,
    OPERATION_TO_SPAN_KIND,
    USAGE_KEY_ALIASES,
    AG2SpanProcessor,
    install_span_processor,
    normalize_attributes,
)

from ._support import attrs, memory_provider


def _emit(provider: TracerProvider, attributes: dict, scope: str = AG2_INSTRUMENTATION_SCOPE, name: str = "s"):
    tracer = provider.get_tracer(scope)
    with tracer.start_as_current_span(name, attributes=attributes):
        pass


@pytest.fixture()
def pipeline():
    provider, exporter = memory_provider()
    assert install_span_processor(provider, config=TraceConfig()) is True
    return provider, exporter


# --- AC-02: kind map -------------------------------------------------------


@pytest.mark.parametrize(
    "operation, kind",
    [("chat", "LLM"), ("execute_tool", "TOOL"), ("invoke_agent", "AGENT")],
)
def test_operation_maps_to_span_kind(pipeline, operation, kind):
    provider, exporter = pipeline
    _emit(provider, {"gen_ai.operation.name": operation})
    (span,) = exporter.get_finished_spans()
    assert attrs(span)["gen_ai.span.kind"] == kind


@pytest.mark.parametrize(
    "attributes",
    [
        {"gen_ai.operation.name": "await_human_input"},  # read, but not in the MAF table
        {"ag2.span.type": "usage", "ag2.usage.kind": "model_call"},  # record_usage: no operation
        {"gen_ai.operation.name": "embeddings"},  # MAF string AG2 does not emit
        {},
    ],
)
def test_unmapped_operations_get_no_kind(pipeline, attributes):
    provider, exporter = pipeline
    _emit(provider, attributes)
    (span,) = exporter.get_finished_spans()
    assert "gen_ai.span.kind" not in attrs(span)


def test_kind_table_is_only_the_strings_read_from_ag2():
    assert dict(OPERATION_TO_SPAN_KIND) == {"chat": "LLM", "execute_tool": "TOOL", "invoke_agent": "AGENT"}


def test_existing_span_kind_is_not_overwritten(pipeline):
    provider, exporter = pipeline
    _emit(provider, {"gen_ai.operation.name": "chat", "gen_ai.span.kind": "CHAIN"})
    (span,) = exporter.get_finished_spans()
    assert attrs(span)["gen_ai.span.kind"] == "CHAIN"


def test_foreign_scope_is_untouched(pipeline):
    provider, exporter = pipeline
    original = {
        "gen_ai.operation.name": "chat",
        "gen_ai.usage.cache_read_input_tokens": 4,
    }
    _emit(provider, original, scope="some.other.instrumentation")
    (span,) = exporter.get_finished_spans()
    assert attrs(span) == original


# --- AC-03: usage aliases --------------------------------------------------


def test_usage_aliases_copy_and_keep_originals(pipeline):
    provider, exporter = pipeline
    source = {
        "gen_ai.operation.name": "chat",
        "gen_ai.usage.input_tokens": 11,
        "gen_ai.usage.output_tokens": 7,
        "gen_ai.usage.cache_creation_input_tokens": 2,
        "gen_ai.usage.cache_read_input_tokens": 3,
        "gen_ai.usage.thinking_tokens": 5,
    }
    _emit(provider, source)
    (span,) = exporter.get_finished_spans()
    got = attrs(span)
    for key, value in source.items():
        assert got[key] == value, key  # originals kept, input/output unchanged
    assert got["gen_ai.usage.cache_creation.input_tokens"] == 2
    assert got["gen_ai.usage.cache_read.input_tokens"] == 3
    assert got["gen_ai.usage.reasoning.output_tokens"] == 5
    assert set(got) == set(source) | {
        "gen_ai.span.kind",
        "gen_ai.usage.cache_creation.input_tokens",
        "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.reasoning.output_tokens",
    }


def test_alias_table_matches_spec():
    assert dict(USAGE_KEY_ALIASES) == {
        "gen_ai.usage.cache_creation_input_tokens": "gen_ai.usage.cache_creation.input_tokens",
        "gen_ai.usage.cache_read_input_tokens": "gen_ai.usage.cache_read.input_tokens",
        "gen_ai.usage.thinking_tokens": "gen_ai.usage.reasoning.output_tokens",
    }


def test_alias_absent_when_source_absent(pipeline):
    provider, exporter = pipeline
    _emit(provider, {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 1})
    (span,) = exporter.get_finished_spans()
    got = attrs(span)
    for target in USAGE_KEY_ALIASES.values():
        assert target not in got


def test_alias_does_not_overwrite_existing_target():
    got = normalize_attributes(
        {
            "gen_ai.usage.thinking_tokens": 5,
            "gen_ai.usage.reasoning.output_tokens": 9,
        }
    )
    assert got["gen_ai.usage.reasoning.output_tokens"] == 9
    assert got["gen_ai.usage.thinking_tokens"] == 5


def test_aliases_apply_to_record_usage_spans(pipeline):
    provider, exporter = pipeline
    _emit(
        provider,
        {"ag2.span.type": "usage", "gen_ai.usage.cache_read_input_tokens": 3},
        name="record_usage model_call",
    )
    (span,) = exporter.get_finished_spans()
    got = attrs(span)
    assert got["gen_ai.usage.cache_read.input_tokens"] == 3
    assert "gen_ai.span.kind" not in got


def test_model_call_usage_span_does_not_double_count_trace_tokens(pipeline):
    """record_usage model_call repeats the chat span's tokens (telemetry.py:437-440 vs 501-504).

    fi-collector promotes gen_ai.usage.* on any span and Observe sums
    total_tokens over every span in a trace, so the duplicate moves to ag2.usage.*.
    """
    provider, exporter = pipeline
    _emit(
        provider,
        {
            "ag2.span.type": "usage",
            "ag2.usage.kind": "model_call",
            "gen_ai.usage.input_tokens": 11,
            "gen_ai.usage.output_tokens": 7,
            "ag2.usage.total_tokens": 18,
        },
        name="record_usage model_call",
    )
    (span,) = exporter.get_finished_spans()
    got = attrs(span)
    assert "gen_ai.usage.input_tokens" not in got
    assert "gen_ai.usage.output_tokens" not in got
    assert got["ag2.usage.input_tokens"] == 11
    assert got["ag2.usage.output_tokens"] == 7
    assert got["ag2.usage.total_tokens"] == 18


def test_subtask_usage_span_keeps_promoted_tokens(pipeline):
    """A subtask rollup with no instrumented worker in the trace is the only copy."""
    provider, exporter = pipeline
    _emit(
        provider,
        {
            "ag2.span.type": "usage",
            "ag2.usage.kind": "subtask",
            "ag2.usage.label": "worker",
            "gen_ai.usage.input_tokens": 4,
        },
        name="record_usage subtask",
    )
    (span,) = exporter.get_finished_spans()
    assert attrs(span)["gen_ai.usage.input_tokens"] == 4


@pytest.mark.parametrize("kind", ["aggregation", "compaction"])
def test_out_of_band_usage_spans_keep_promoted_tokens(pipeline, kind):
    """aggregate.py / compact.py call the model client outside on_llm_call, so
    no chat span carries these tokens."""
    provider, exporter = pipeline
    _emit(
        provider,
        {
            "ag2.span.type": "usage",
            "ag2.usage.kind": kind,
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 50,
        },
        name=f"record_usage {kind}",
    )
    (span,) = exporter.get_finished_spans()
    got = attrs(span)
    assert got["gen_ai.usage.input_tokens"] == 100
    assert got["gen_ai.usage.output_tokens"] == 50
    assert "ag2.usage.input_tokens" not in got


def _subtask_after_worker(provider, worker_name: str, label: str) -> None:
    tracer = provider.get_tracer(AG2_INSTRUMENTATION_SCOPE)
    with tracer.start_as_current_span("invoke_agent planner", attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "planner"}):
        with tracer.start_as_current_span(
            f"invoke_agent {worker_name}",
            attributes={"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": worker_name},
        ):
            pass
        with tracer.start_as_current_span(
            "record_usage subtask",
            attributes={
                "ag2.span.type": "usage",
                "ag2.usage.kind": "subtask",
                "ag2.usage.label": label,
                "gen_ai.usage.input_tokens": 40,
                "gen_ai.usage.output_tokens": 4,
            },
        ):
            pass


def test_subtask_usage_span_demoted_when_its_agent_ran_instrumented_in_the_trace(pipeline):
    provider, exporter = pipeline
    _subtask_after_worker(provider, worker_name="worker", label="worker")
    (rollup,) = [s for s in exporter.get_finished_spans() if s.name == "record_usage subtask"]
    got = attrs(rollup)
    assert "gen_ai.usage.input_tokens" not in got and "gen_ai.usage.output_tokens" not in got
    assert (got["ag2.usage.input_tokens"], got["ag2.usage.output_tokens"]) == (40, 4)


def test_subtask_usage_span_kept_when_label_is_another_agent(pipeline):
    provider, exporter = pipeline
    _subtask_after_worker(provider, worker_name="other_worker", label="worker")
    (rollup,) = [s for s in exporter.get_finished_spans() if s.name == "record_usage subtask"]
    assert attrs(rollup)["gen_ai.usage.input_tokens"] == 40


def test_agent_name_map_is_bounded():
    processor = AG2SpanProcessor(config=TraceConfig(), max_tracked_traces=2)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    for name in ("a", "b", "c"):
        _emit(provider, {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name})
    assert len(processor._agents_by_trace) == 2
    assert [set(v) for v in processor._agents_by_trace.values()] == [{"b"}, {"c"}]


def test_pending_context_sessions_are_bounded_and_released_on_end():
    from fi_instrumentation import using_session

    processor = AG2SpanProcessor(config=TraceConfig(), max_tracked_traces=1)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    tracer = provider.get_tracer(AG2_INSTRUMENTATION_SCOPE)
    with using_session("s"):
        open_spans = [tracer.start_span(f"s{i}") for i in range(40)]
    assert len(processor._session_by_span) == processor._max_pending_spans == 16
    for span in open_spans:
        span.end()
    assert len(processor._session_by_span) == 0


def _limited_pipeline(max_span_attributes: int, config: TraceConfig = TraceConfig(), max_attribute_length=None):
    from opentelemetry.sdk.trace import SpanLimits

    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        span_limits=SpanLimits(max_span_attributes=max_span_attributes, max_attribute_length=max_attribute_length),
        shutdown_on_exit=False,
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    assert install_span_processor(provider, config=config) is True
    return provider, exporter


def test_normalizing_keeps_the_sdk_dropped_attribute_count():
    """ReadableSpan.dropped_attributes (exported as dropped_attributes_count)
    reads the BoundedAttributes counter; replacing the attributes must keep it."""
    from opentelemetry.attributes import BoundedAttributes

    provider, exporter = _limited_pipeline(2, TraceConfig(hide_inputs=True), max_attribute_length=64)
    # The SDK keeps the two newest keys and counts "a" as dropped.
    _emit(provider, {"a": 1, "b": 2, "gen_ai.input.messages": "secret"})
    (span,) = exporter.get_finished_spans()
    assert attrs(span) == {"b": 2}
    assert span.dropped_attributes == 1
    assert isinstance(span._attributes, BoundedAttributes)
    assert (span._attributes.maxlen, span._attributes.max_value_len) == (2, 64)
    with pytest.raises(TypeError):
        span._attributes["late"] = "write"  # ended spans stay immutable


def test_keys_the_processor_adds_count_against_span_limits():
    provider, exporter = _limited_pipeline(2)
    _emit(provider, {"x": 1, "gen_ai.operation.name": "chat"})  # full, nothing dropped yet
    (span,) = exporter.get_finished_spans()
    # Adding gen_ai.span.kind evicts the oldest key, as the SDK would, and
    # the eviction is counted.
    assert attrs(span) == {"gen_ai.operation.name": "chat", "gen_ai.span.kind": "LLM"}
    assert span.dropped_attributes == 1


def test_processor_only_adds_keys_by_default(pipeline):
    """Nothing AG2 set is stripped, including propagation-related keys."""
    provider, exporter = pipeline
    source = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": "a",
        "ag2.otel.traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "ag2.span.type": "agent",
    }
    _emit(provider, source)
    (span,) = exporter.get_finished_spans()
    got = attrs(span)
    assert {k: got[k] for k in source} == source


# --- TraceConfig second gate ----------------------------------------------


def test_trace_config_hides_content_when_capture_was_on():
    content = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.input.messages": '[{"role":"user","content":"hi"}]',
        "gen_ai.output.messages": '[{"role":"assistant","content":"yo"}]',
        "gen_ai.tool.call.arguments": '{"city":"Paris"}',
        "gen_ai.tool.call.result": "sunny",
        "ag2.human_input.prompt": "ok?",
        "ag2.human_input.response": "yes",
        "gen_ai.tool.name": "get_weather",
    }
    got = normalize_attributes(content, TraceConfig(hide_inputs=True, hide_outputs=True))
    assert got == {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.span.kind": "TOOL",
        "gen_ai.tool.name": "get_weather",
    }
    kept = normalize_attributes(content, TraceConfig(hide_inputs=False, hide_outputs=False))
    for key, value in content.items():
        assert kept[key] == value


# AG2 records message content as JSON strings, not as the flattened
# ``gen_ai.input.messages.{i}.message.content`` keys TraceConfig.mask's text
# and image rules match, so those flags drop the whole JSON attribute.
_AG2_CONTENT = {
    "gen_ai.operation.name": "chat",
    "gen_ai.request.model": "m",
    "gen_ai.system_instructions": '[{"type": "text", "content": "be terse"}]',
    "gen_ai.input.messages": '[{"content": "my secret prompt", "role": "user"}]',
    "gen_ai.output.messages": '[{"content": "secret answer", "role": "assistant"}]',
    "gen_ai.tool.call.arguments": '{"city": "Paris"}',
    "gen_ai.tool.call.result": "sunny",
    "ag2.human_input.prompt": "ok?",
    "ag2.human_input.response": "yes",
}
_INPUT_TEXT_KEYS = {
    "gen_ai.system_instructions",
    "gen_ai.input.messages",
    "gen_ai.tool.call.arguments",
    "ag2.human_input.prompt",
}
_OUTPUT_TEXT_KEYS = {"gen_ai.output.messages", "gen_ai.tool.call.result", "ag2.human_input.response"}


@pytest.mark.parametrize(
    "flags, dropped",
    [
        ({"hide_input_text": True}, _INPUT_TEXT_KEYS),
        ({"hide_output_text": True}, _OUTPUT_TEXT_KEYS),
        ({"hide_inputs": True}, _INPUT_TEXT_KEYS),
        ({"hide_outputs": True}, _OUTPUT_TEXT_KEYS),
        ({"hide_input_messages": True}, {"gen_ai.input.messages", "gen_ai.system_instructions"}),
        ({"hide_output_messages": True}, {"gen_ai.output.messages"}),
        ({"hide_input_text": True, "hide_output_text": True}, _INPUT_TEXT_KEYS | _OUTPUT_TEXT_KEYS),
    ],
    ids=lambda v: "+".join(sorted(v)) if isinstance(v, dict) else None,
)
def test_trace_config_flags_on_ag2_json_content(flags, dropped):
    got = normalize_attributes(_AG2_CONTENT, TraceConfig(**flags))
    assert not dropped & set(got), sorted(dropped & set(got))
    for key in set(_AG2_CONTENT) - dropped:
        assert got[key] == _AG2_CONTENT[key], key


@pytest.mark.parametrize("flag", ["hide_input_images", "hide_embedding_vectors"])
def test_flags_with_nothing_to_act_on_leave_ag2_content(flag):
    """AG2 records only text parts (binary inputs are omitted) and no
    embeddings, so these flags change nothing."""
    got = normalize_attributes(_AG2_CONTENT, TraceConfig(**{flag: True}))
    for key, value in _AG2_CONTENT.items():
        assert got[key] == value, key


def test_trace_config_reads_env(monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    processor = AG2SpanProcessor()
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _emit(provider, {"gen_ai.operation.name": "chat", "gen_ai.input.messages": "[]"})
    (span,) = exporter.get_finished_spans()
    assert "gen_ai.input.messages" not in attrs(span)


# --- installation ----------------------------------------------------------


def test_install_is_idempotent_and_prepends():
    provider, exporter = memory_provider()
    assert install_span_processor(provider) is True
    assert install_span_processor(provider) is False
    chain = provider._active_span_processor._span_processors
    assert isinstance(chain[0], AG2SpanProcessor)
    assert sum(isinstance(p, AG2SpanProcessor) for p in chain) == 1
    assert isinstance(chain[1], SimpleSpanProcessor)


def test_install_keeps_fi_register_exporter():
    """register() marks its exporter as a replaceable default; install must not drop it."""
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    exporter = InMemorySpanExporter()
    provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="ag2-unit",
        batch=False,
        span_exporter=exporter,
        verbose=False,
    )
    try:
        assert install_span_processor(provider) is True
        _emit(provider, {"gen_ai.operation.name": "chat", "gen_ai.usage.thinking_tokens": 2})
        (span,) = exporter.get_finished_spans()
        got = attrs(span)
        assert got["gen_ai.span.kind"] == "LLM"
        assert got["gen_ai.usage.reasoning.output_tokens"] == 2
    finally:
        provider.shutdown()


def _fi_provider(exporter):
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    return register(
        project_type=ProjectType.OBSERVE,
        project_name="ag2-unit",
        batch=False,
        span_exporter=exporter,
        verbose=False,
    )


def test_later_add_span_processor_on_fi_provider_keeps_ag2_processor_first():
    """fi's provider shuts down and clears every processor on the first
    add_span_processor() after register() (otel.py 329-340); the user's new
    exporter must still receive normalized AG2 spans."""
    provider = _fi_provider(InMemorySpanExporter())
    try:
        assert install_span_processor(provider) is True
        replacement = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(replacement))

        chain = provider._active_span_processor._span_processors
        assert isinstance(chain[0], AG2SpanProcessor), chain
        assert sum(isinstance(p, AG2SpanProcessor) for p in chain) == 1
        assert [type(p) for p in chain[1:]] == [SimpleSpanProcessor]

        _emit(provider, {"gen_ai.operation.name": "chat", "gen_ai.usage.thinking_tokens": 2})
        (span,) = replacement.get_finished_spans()
        got = attrs(span)
        assert got["gen_ai.span.kind"] == "LLM"
        assert got["gen_ai.usage.reasoning.output_tokens"] == 2
    finally:
        provider.shutdown()


def test_later_add_span_processor_on_sdk_provider_keeps_ag2_processor_first():
    provider, first = memory_provider()
    assert install_span_processor(provider) is True
    assert install_span_processor(provider) is False  # no second wrapper either
    second = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(second))
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))

    chain = provider._active_span_processor._span_processors
    assert isinstance(chain[0], AG2SpanProcessor), chain
    assert sum(isinstance(p, AG2SpanProcessor) for p in chain) == 1
    assert len(chain) == 4
    _emit(provider, {"gen_ai.operation.name": "chat"})
    for exporter in (first, second):
        (span,) = exporter.get_finished_spans()
        assert attrs(span)["gen_ai.span.kind"] == "LLM"


def test_provider_shutdown_still_shuts_the_processor_down():
    provider, _ = memory_provider()
    install_span_processor(provider)
    (processor,) = [p for p in provider._active_span_processor._span_processors if isinstance(p, AG2SpanProcessor)]
    provider.shutdown()
    assert processor._shutdown is True


def test_install_warns_when_processors_run_concurrently(caplog):
    """A ConcurrentMultiSpanProcessor runs on_end of every processor in
    parallel, so the exporter may read a span before it is normalized."""
    from opentelemetry.sdk.trace import ConcurrentMultiSpanProcessor

    provider = TracerProvider(active_span_processor=ConcurrentMultiSpanProcessor(), shutdown_on_exit=False)
    try:
        with caplog.at_level(logging.WARNING, logger="traceai_ag2"):
            assert install_span_processor(provider) is True
        assert any("ConcurrentMultiSpanProcessor" in r.getMessage() for r in caplog.records), caplog.text
    finally:
        provider.shutdown()


def test_install_without_sdk_provider_returns_false():
    class _NoChain:
        pass

    assert install_span_processor(_NoChain()) is False


def test_shutdown_processor_stops_mapping():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    processor = AG2SpanProcessor(config=TraceConfig())
    provider.add_span_processor(processor)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    processor.shutdown()
    _emit(provider, {"gen_ai.operation.name": "chat"})
    (span,) = exporter.get_finished_spans()
    assert "gen_ai.span.kind" not in attrs(span)
    assert processor.force_flush() is True
