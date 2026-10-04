"""Synthetic-span tests for the mapping-only Semantic Kernel span processor.

The spans here carry the exact names, scopes and keys semantic-kernel 1.44.1
emits (read from the installed SDK; see README "Attribute inventory"), but are
created directly with the OpenTelemetry SDK, so no Semantic Kernel code runs.
"""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from fi_instrumentation import using_attributes
from traceai_semantic_kernel import processor as processor_module
from traceai_semantic_kernel.processor import (
    CONTENT_KEYS,
    FI_SPAN_KIND,
    PROMOTED_USAGE_KEYS,
    SemanticKernelSpanProcessor,
    kind_for,
    map_sk_attributes,
)

MODEL_SCOPE = "semantic_kernel.utils.telemetry.model_diagnostics.decorators"
AGENT_SCOPE = "semantic_kernel.utils.telemetry.agent_diagnostics.decorators"
FUNCTION_SCOPE = "semantic_kernel.functions.kernel_function"
LOOP_SCOPE = "semantic_kernel.connectors.ai.chat_completion_client_base"

CHAT_ATTRS = {
    "gen_ai.operation.name": "chat",
    "gen_ai.system": "openai",
    "gen_ai.request.model": "gpt-4o-mini",
    "server.address": "http://127.0.0.1:1/v1/",
    "gen_ai.response.id": "chatcmpl-1",
    "gen_ai.response.finish_reason": "FinishReason.STOP",
    "gen_ai.usage.input_tokens": 11,
    "gen_ai.usage.output_tokens": 7,
}
AGENT_ATTRS = {
    "gen_ai.operation.name": "invoke_agent",
    "gen_ai.agent.id": "agent-1",
    "gen_ai.agent.name": "Assistant",
}
TOOL_ATTRS = {
    "gen_ai.operation.name": "execute_tool",
    "gen_ai.tool.name": "Weather-get_weather",
    "gen_ai.tool.call.id": "call_1",
    "gen_ai.tool.description": "Weather for a city",
}


@pytest.fixture()
def pipeline():
    def build(sensitive: bool = False):
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        processor = SemanticKernelSpanProcessor(sensitive=sensitive)
        # Same order instrument() uses: the mapping processor runs before the exporter.
        provider.add_span_processor(processor)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        return provider, exporter, processor

    return build


def _emit(provider, scope, name, attributes, status=None, parent=None):
    tracer = provider.get_tracer(scope)
    span = tracer.start_span(name, attributes=attributes, context=parent)
    if status is not None:
        span.set_status(status)
    span.end()
    return span


def _attrs(exporter, name):
    spans = [s for s in exporter.get_finished_spans() if s.name == name]
    assert spans, "no span named {0!r}".format(name)
    return dict(spans[-1].attributes)


# Kind mapping ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,attributes,expected",
    [
        ("chat gpt-4o-mini", CHAT_ATTRS, "LLM"),
        ("text_completions gpt-3.5", {"gen_ai.operation.name": "text_completions"}, "LLM"),
        ("invoke_agent Assistant", AGENT_ATTRS, "AGENT"),
        ("execute_tool Weather-get_weather", TOOL_ATTRS, "TOOL"),
        (
            "execute_tool MKIxhdgYdFMWOMho",
            {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "MKIxhdgYdFMWOMho"},
            "CHAIN",
        ),
        ("AutoFunctionInvocationLoop", {"sk.available_functions": "Weather-get_weather"}, "CHAIN"),
        # Fallbacks for an operation string this package has not seen.
        ("something new", {"gen_ai.operation.name": "new_op", "gen_ai.request.model": "m"}, "LLM"),
        ("something new", {"gen_ai.operation.name": "new_op", "gen_ai.agent.name": "a"}, "AGENT"),
        ("something new", {"gen_ai.operation.name": "new_op", "gen_ai.tool.name": "t"}, "TOOL"),
        ("something new", {"gen_ai.operation.name": "new_op"}, None),
    ],
)
def test_kind_for(name, attributes, expected):
    assert kind_for(name, attributes) == expected


def test_llm_span_gets_kind_provider_alias_and_total(pipeline):
    provider, exporter, _ = pipeline()
    _emit(provider, MODEL_SCOPE, "chat gpt-4o-mini", CHAT_ATTRS)
    attrs = _attrs(exporter, "chat gpt-4o-mini")
    assert attrs[FI_SPAN_KIND] == "LLM"
    assert attrs["gen_ai.span.kind"] == "LLM"
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.system"] == "openai"
    assert attrs["gen_ai.request.model"] == "gpt-4o-mini"
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs["gen_ai.usage.total_tokens"] == 18
    # AC-02: every key Semantic Kernel emitted survives the processor unchanged.
    for key, value in CHAT_ATTRS.items():
        assert attrs[key] == value, key


def test_existing_provider_name_and_total_are_not_overwritten():
    mapped = map_sk_attributes(
        dict(CHAT_ATTRS, **{"gen_ai.provider.name": "azure.openai", "gen_ai.usage.total_tokens": 99}),
        name="chat gpt-4o-mini",
    )
    assert mapped["gen_ai.provider.name"] == "azure.openai"
    assert mapped["gen_ai.usage.total_tokens"] == 99


def test_agent_tool_and_chain_kinds(pipeline):
    provider, exporter, _ = pipeline()
    _emit(provider, AGENT_SCOPE, "invoke_agent Assistant", AGENT_ATTRS)
    _emit(provider, FUNCTION_SCOPE, "execute_tool Weather-get_weather", TOOL_ATTRS)
    _emit(
        provider,
        FUNCTION_SCOPE,
        "execute_tool Prompt-fn",
        {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "Prompt-fn"},
    )
    _emit(provider, LOOP_SCOPE, "AutoFunctionInvocationLoop", {"sk.available_functions": "Weather-get_weather"})
    assert _attrs(exporter, "invoke_agent Assistant")["gen_ai.span.kind"] == "AGENT"
    assert _attrs(exporter, "execute_tool Weather-get_weather")[FI_SPAN_KIND] == "TOOL"
    assert _attrs(exporter, "execute_tool Prompt-fn")[FI_SPAN_KIND] == "CHAIN"
    assert _attrs(exporter, "AutoFunctionInvocationLoop")["gen_ai.span.kind"] == "CHAIN"


def test_spans_from_other_scopes_pass_through_untouched(pipeline):
    provider, exporter, _ = pipeline()
    _emit(provider, "openai.client", "chat gpt-4o-mini", dict(CHAT_ATTRS, **{"gen_ai.input.messages": "[]"}))
    attrs = _attrs(exporter, "chat gpt-4o-mini")
    assert FI_SPAN_KIND not in attrs
    assert "gen_ai.provider.name" not in attrs
    assert attrs["gen_ai.input.messages"] == "[]"


# Usage: promoted keys only on model-call spans -------------------------------


def test_usage_on_non_llm_spans_moves_to_namespaced_keys():
    aggregate = {
        "gen_ai.usage.input_tokens": 22,
        "gen_ai.usage.output_tokens": 14,
        "gen_ai.usage.total_tokens": 36,
        "llm.token_count.prompt": 22,
        "llm.usage.total_tokens": 36,
        "gen_ai.cost.total": 0.5,
        "llm.cost.total": 0.5,
    }
    mapped = map_sk_attributes(dict(AGENT_ATTRS, **aggregate), name="invoke_agent Assistant")
    for key in aggregate:
        assert key not in mapped, key
    assert mapped["semantic_kernel.usage.input_tokens"] == 22
    assert mapped["semantic_kernel.usage.output_tokens"] == 14
    assert mapped["semantic_kernel.usage.total_tokens"] == 36
    assert mapped["semantic_kernel.usage.llm.token_count.prompt"] == 22
    assert mapped["semantic_kernel.usage.gen_ai.cost.total"] == 0.5
    assert set(PROMOTED_USAGE_KEYS) >= set(aggregate)


def test_trace_wide_promoted_input_tokens_equal_model_calls(pipeline):
    """Observe sums promoted token keys over every span of a trace."""
    provider, exporter, _ = pipeline()
    tracer = provider.get_tracer(AGENT_SCOPE)
    from opentelemetry import trace as trace_api

    with tracer.start_as_current_span(
        "invoke_agent Assistant",
        # A future SK that reports the agent-run aggregate on the agent span.
        attributes=dict(AGENT_ATTRS, **{"gen_ai.usage.input_tokens": 22, "gen_ai.usage.output_tokens": 14}),
    ):
        ctx = trace_api.set_span_in_context(trace_api.get_current_span())
        _emit(provider, MODEL_SCOPE, "chat gpt-4o-mini", CHAT_ATTRS, parent=ctx)
        _emit(provider, FUNCTION_SCOPE, "execute_tool Weather-get_weather", TOOL_ATTRS, parent=ctx)
        _emit(provider, MODEL_SCOPE, "chat gpt-4o-mini", CHAT_ATTRS, parent=ctx)

    spans = exporter.get_finished_spans()
    assert len({s.context.trace_id for s in spans}) == 1
    model_calls = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert len(model_calls) == 2

    def promoted_input(span):
        return sum(
            span.attributes.get(key, 0)
            for key in ("gen_ai.usage.input_tokens", "llm.token_count.prompt", "llm.usage.prompt_tokens")
        )

    assert sum(promoted_input(s) for s in spans) == 2 * 11
    assert sum(promoted_input(s) for s in spans) == sum(promoted_input(s) for s in model_calls)


# Session ----------------------------------------------------------------------


def test_conversation_id_copied_to_session_id_unless_set():
    mapped = map_sk_attributes(dict(AGENT_ATTRS, **{"gen_ai.conversation.id": "thread-1"}), name="invoke_agent A")
    assert mapped["session.id"] == "thread-1"
    kept = map_sk_attributes(
        dict(AGENT_ATTRS, **{"gen_ai.conversation.id": "thread-1", "session.id": "app-session"}),
        name="invoke_agent A",
    )
    assert kept["session.id"] == "app-session"


def test_no_session_id_invented_without_conversation_id(pipeline):
    provider, exporter, _ = pipeline()
    _emit(provider, AGENT_SCOPE, "invoke_agent Assistant", AGENT_ATTRS)
    assert "session.id" not in _attrs(exporter, "invoke_agent Assistant")


def test_using_attributes_context_lands_on_native_spans(pipeline):
    provider, exporter, _ = pipeline()
    with using_attributes(session_id="session-42", user_id="user-7"):
        _emit(provider, AGENT_SCOPE, "invoke_agent Assistant", AGENT_ATTRS)
        _emit(provider, "someone.else", "other", {})
    attrs = _attrs(exporter, "invoke_agent Assistant")
    assert attrs["session.id"] == "session-42"
    assert attrs["user.id"] == "user-7"
    assert "session.id" not in _attrs(exporter, "other")


# Content ----------------------------------------------------------------------


SENSITIVE_AGENT = dict(
    AGENT_ATTRS,
    **{
        "gen_ai.input.messages": json.dumps([{"role": "user", "content": "SECRET-IN"}]),
        "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": "SECRET-OUT"}]),
    },
)
SENSITIVE_TOOL = dict(
    TOOL_ATTRS,
    **{"gen_ai.tool.call.arguments": '{"city": "SECRET-CITY"}', "gen_ai.tool.call.result": "sunny in SECRET-CITY"},
)


def test_content_removed_when_sensitive_off(pipeline):
    provider, exporter, _ = pipeline(sensitive=False)
    _emit(provider, AGENT_SCOPE, "invoke_agent Assistant", SENSITIVE_AGENT)
    _emit(provider, FUNCTION_SCOPE, "execute_tool Weather-get_weather", SENSITIVE_TOOL)
    for name in ("invoke_agent Assistant", "execute_tool Weather-get_weather"):
        attrs = _attrs(exporter, name)
        for key in CONTENT_KEYS:
            assert key not in attrs, (name, key)
        assert "SECRET" not in json.dumps(attrs)


def test_content_surfaced_when_sensitive_on(pipeline):
    provider, exporter, _ = pipeline(sensitive=True)
    _emit(provider, AGENT_SCOPE, "invoke_agent Assistant", SENSITIVE_AGENT)
    _emit(provider, FUNCTION_SCOPE, "execute_tool Weather-get_weather", SENSITIVE_TOOL)
    agent = _attrs(exporter, "invoke_agent Assistant")
    assert "SECRET-IN" in agent["input.value"]
    assert "SECRET-OUT" in agent["output.value"]
    assert agent["input.mime_type"] == "application/json"
    assert agent["gen_ai.input.messages"] == SENSITIVE_AGENT["gen_ai.input.messages"]
    tool = _attrs(exporter, "execute_tool Weather-get_weather")
    assert json.loads(tool["input.value"]) == {"city": "SECRET-CITY"}
    assert tool["input.mime_type"] == "application/json"
    assert tool["output.value"] == "sunny in SECRET-CITY"
    assert tool["output.mime_type"] == "text/plain"


# Status -----------------------------------------------------------------------


def test_error_type_with_unset_status_becomes_error(pipeline):
    provider, exporter, _ = pipeline()
    _emit(provider, FUNCTION_SCOPE, "execute_tool broken", dict(TOOL_ATTRS, **{"error.type": "ValueError"}))
    span = [s for s in exporter.get_finished_spans() if s.name == "execute_tool broken"][0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "ValueError"


def test_processor_never_clears_status(pipeline):
    provider, exporter, _ = pipeline()
    _emit(provider, MODEL_SCOPE, "chat err", dict(CHAT_ATTRS, **{"error.type": "X"}), status=Status(StatusCode.ERROR, "boom"))
    _emit(provider, MODEL_SCOPE, "chat ok", CHAT_ATTRS, status=Status(StatusCode.OK))
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["chat err"].status.status_code is StatusCode.ERROR
    assert spans["chat err"].status.description == "boom"
    assert spans["chat ok"].status.status_code is StatusCode.OK


# Failure isolation --------------------------------------------------------------


def test_processor_exceptions_are_swallowed(pipeline, monkeypatch):
    provider, exporter, processor = pipeline()

    def boom(*_args, **_kwargs):
        raise RuntimeError("mapping bug")

    monkeypatch.setattr(processor_module, "map_sk_attributes", boom)
    monkeypatch.setattr(processor_module, "_context_attributes", boom)
    _emit(provider, MODEL_SCOPE, "chat gpt-4o-mini", CHAT_ATTRS)
    # The span still reaches the exporter, unmapped.
    attrs = _attrs(exporter, "chat gpt-4o-mini")
    assert attrs["gen_ai.request.model"] == "gpt-4o-mini"


def test_shutdown_disables_mapping(pipeline):
    provider, exporter, processor = pipeline()
    processor.shutdown()
    _emit(provider, MODEL_SCOPE, "chat gpt-4o-mini", CHAT_ATTRS)
    assert FI_SPAN_KIND not in _attrs(exporter, "chat gpt-4o-mini")
    assert processor.force_flush() is True
