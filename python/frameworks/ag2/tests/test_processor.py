"""Unit tests for AG2SpanProcessor on synthetic spans (AC-02, AC-03)."""

from __future__ import annotations

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
