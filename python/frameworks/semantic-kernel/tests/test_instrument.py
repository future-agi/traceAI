"""instrument() / uninstrument() against the installed semantic-kernel.

These run in process. ``instrument()`` changes process-wide Semantic Kernel
state (module-level settings objects and module-level tracers), so every test
uninstruments in a fixture finalizer.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import logging
import os
from pathlib import Path

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_semantic_kernel import (
    DIAGNOSTICS_SETTINGS_MODULES,
    TRACER_MODULES,
    SemanticKernelInstrumentor,
    SemanticKernelSpanProcessor,
)

PACKAGE_DIR = Path(__file__).resolve().parent.parent


def _settings():
    return [importlib.import_module(name).MODEL_DIAGNOSTICS_SETTINGS for name in DIAGNOSTICS_SETTINGS_MODULES]


@pytest.fixture()
def instrumentor():
    inst = SemanticKernelInstrumentor()
    yield inst
    inst.uninstrument()


@pytest.fixture()
def provider():
    tp = TracerProvider()
    exporter = InMemorySpanExporter()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    tp.exporter = exporter  # type: ignore[attr-defined]
    return tp


def test_no_env_vars_needed_and_diagnostics_flip_on(instrumentor, provider, monkeypatch):
    for key in list(os.environ):
        if key.startswith("SEMANTICKERNEL_"):
            monkeypatch.delenv(key)
    before = [(s.enable_otel_diagnostics, s.enable_otel_diagnostics_sensitive) for s in _settings()]
    assert before == [(False, False)] * len(DIAGNOSTICS_SETTINGS_MODULES)

    instrumentor.instrument(tracer_provider=provider)

    assert [(s.enable_otel_diagnostics, s.enable_otel_diagnostics_sensitive) for s in _settings()] == [
        (True, False)
    ] * len(DIAGNOSTICS_SETTINGS_MODULES)
    assert not any(key.startswith("SEMANTICKERNEL_") for key in os.environ)

    from semantic_kernel.utils.telemetry.agent_diagnostics import decorators as agent_decorators
    from semantic_kernel.utils.telemetry.model_diagnostics import decorators as model_decorators
    from semantic_kernel.utils.telemetry.model_diagnostics import function_tracer

    assert model_decorators.are_model_diagnostics_enabled() is True
    assert agent_decorators.are_model_diagnostics_enabled() is True
    assert model_decorators.are_sensitive_events_enabled() is False
    assert function_tracer.are_sensitive_events_enabled() is False

    instrumentor.uninstrument()
    assert [(s.enable_otel_diagnostics, s.enable_otel_diagnostics_sensitive) for s in _settings()] == before


def test_sensitive_opt_in_flips_sensitive(instrumentor, provider):
    instrumentor.instrument(tracer_provider=provider, sensitive=True)
    assert all(s.enable_otel_diagnostics_sensitive for s in _settings())
    assert instrumentor.processor is not None and instrumentor.processor.sensitive is True


def test_sensitive_false_overrides_env_opt_in(instrumentor, provider, monkeypatch, caplog):
    for settings in _settings():
        monkeypatch.setattr(settings, "enable_otel_diagnostics_sensitive", True)
    instrumentor.instrument(tracer_provider=provider)
    assert not any(s.enable_otel_diagnostics_sensitive for s in _settings())
    assert "sensitive" in caplog.text.lower()


# TraceConfig / FI_HIDE_INPUTS / FI_HIDE_OUTPUTS (N1) ---------------------------

AGENT_SCOPE = "semantic_kernel.utils.telemetry.agent_diagnostics.decorators"
HIDE_ENV = ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS")


@pytest.mark.parametrize(
    "config_kwargs,env,hide_in,hide_out",
    [
        pytest.param({"hide_inputs": True}, {}, True, False, id="config-hide_inputs"),
        pytest.param({"hide_outputs": True}, {}, False, True, id="config-hide_outputs"),
        pytest.param({"hide_inputs": True, "hide_outputs": True}, {}, True, True, id="config-both"),
        pytest.param(None, {"FI_HIDE_INPUTS": "true"}, True, False, id="env-FI_HIDE_INPUTS"),
        pytest.param(None, {"FI_HIDE_OUTPUTS": "true"}, False, True, id="env-FI_HIDE_OUTPUTS"),
        pytest.param(None, {}, False, False, id="control-no-hiding"),
    ],
)
def test_trace_config_hides_content_with_sensitive_on(
    instrumentor, provider, monkeypatch, config_kwargs, env, hide_in, hide_out
):
    """sensitive=True copies content; TraceConfig / FI_HIDE_* then drop the hidden side."""
    from fi_instrumentation import TraceConfig
    from semantic_kernel import Kernel
    from semantic_kernel.functions import kernel_function

    for key in HIDE_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    kwargs = {"config": TraceConfig(**config_kwargs)} if config_kwargs is not None else {}

    class Plugin:
        @kernel_function(name="answer", description="Answer")
        def answer(self, question: str) -> str:
            return "OUT-MARKER"

    kernel = Kernel()
    kernel.add_plugin(Plugin(), "P")
    instrumentor.instrument(tracer_provider=provider, sensitive=True, **kwargs)

    # Real Semantic Kernel execute_tool span: arguments and result attributes.
    asyncio.run(kernel.invoke(plugin_name="P", function_name="answer", question="IN-MARKER"))
    # Agent span with Semantic Kernel's agent scope and message keys.
    provider.get_tracer(AGENT_SCOPE).start_span(
        "invoke_agent A",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "A",
            "gen_ai.input.messages": '[{"role": "user", "content": "IN-MARKER"}]',
            "gen_ai.output.messages": '[{"role": "assistant", "content": "OUT-MARKER"}]',
        },
    ).end()

    spans = {s.name: dict(s.attributes) for s in provider.exporter.get_finished_spans()}
    tool, agent = spans["execute_tool P-answer"], spans["invoke_agent A"]
    for attrs, input_key, output_key in (
        (tool, "gen_ai.tool.call.arguments", "gen_ai.tool.call.result"),
        (agent, "gen_ai.input.messages", "gen_ai.output.messages"),
    ):
        blob = str(attrs)
        assert ("IN-MARKER" in blob) is (not hide_in), attrs
        assert ("OUT-MARKER" in blob) is (not hide_out), attrs
        for key in (input_key, "input.value", "input.mime_type"):
            assert (key in attrs) is (not hide_in), (key, attrs)
        for key in (output_key, "output.value", "output.mime_type"):
            assert (key in attrs) is (not hide_out), (key, attrs)


def test_trace_config_of_the_wrong_type_is_rejected(instrumentor, provider):
    with pytest.raises(TypeError, match="TraceConfig"):
        instrumentor.instrument(tracer_provider=provider, sensitive=True, config={"hide_inputs": True})
    assert not instrumentor.is_instrumented
    assert not any(s.enable_otel_diagnostics for s in _settings())


def test_kernel_invoke_is_not_wrapped(instrumentor, provider):
    from semantic_kernel import Kernel
    from semantic_kernel.functions.kernel_function import KernelFunction

    originals = {
        "Kernel.invoke": Kernel.__dict__["invoke"],
        "Kernel.invoke_stream": Kernel.__dict__["invoke_stream"],
        "Kernel.invoke_prompt": Kernel.__dict__["invoke_prompt"],
        "KernelFunction.invoke": KernelFunction.__dict__["invoke"],
        "KernelFunction.invoke_stream": KernelFunction.__dict__["invoke_stream"],
    }
    instrumentor.instrument(tracer_provider=provider)
    after = {
        "Kernel.invoke": Kernel.__dict__["invoke"],
        "Kernel.invoke_stream": Kernel.__dict__["invoke_stream"],
        "Kernel.invoke_prompt": Kernel.__dict__["invoke_prompt"],
        "KernelFunction.invoke": KernelFunction.__dict__["invoke"],
        "KernelFunction.invoke_stream": KernelFunction.__dict__["invoke_stream"],
    }
    for name, original in originals.items():
        assert after[name] is original, name
        assert not hasattr(after[name], "__wrapped__"), name


def test_package_does_not_import_or_depend_on_wrapt():
    for path in (PACKAGE_DIR / "traceai_semantic_kernel").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "wrapt" for alias in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "wrapt", path
    pyproject = (PACKAGE_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert "wrapt" not in pyproject


def test_user_installed_processor_is_not_adopted_or_removed(provider):
    """instrument() owns only the processor it installs (R5)."""
    users = SemanticKernelSpanProcessor(sensitive=False)
    active = provider._active_span_processor
    active._span_processors = (users,) + tuple(active._span_processors)
    inst = SemanticKernelInstrumentor()
    try:
        inst.instrument(tracer_provider=provider, sensitive=True)
        assert inst.processor is not users
        assert inst.processor.sensitive is True
        assert users.sensitive is False
        assert users in active._span_processors
    finally:
        inst.uninstrument()

    # The user's processor is still installed, not shut down, and still maps spans.
    assert [p for p in active._span_processors if isinstance(p, SemanticKernelSpanProcessor)] == [users]
    tracer = provider.get_tracer("semantic_kernel.utils.telemetry.model_diagnostics.decorators")
    tracer.start_span("chat m", attributes={"gen_ai.operation.name": "chat"}).end()
    assert provider.exporter.get_finished_spans()[-1].attributes["fi.span.kind"] == "LLM"


def test_instrument_twice_installs_one_processor_first_in_line(provider):
    first = SemanticKernelInstrumentor()
    second = SemanticKernelInstrumentor()
    try:
        first.instrument(tracer_provider=provider)
        first.instrument(tracer_provider=provider)
        second.instrument(tracer_provider=provider)
        processors = provider._active_span_processor._span_processors
        ours = [p for p in processors if isinstance(p, SemanticKernelSpanProcessor)]
        assert len(ours) == 1
        assert processors[0] is ours[0]
        assert first.is_instrumented and second.is_instrumented
    finally:
        second.uninstrument()
    assert not any(
        isinstance(p, SemanticKernelSpanProcessor) for p in provider._active_span_processor._span_processors
    )
    assert not first.is_instrumented


def test_native_spans_route_to_the_provider_passed(instrumentor, provider):
    """SK's tracers come from the global provider; instrument() routes them to ours."""
    assert isinstance(trace_api.get_tracer_provider(), trace_api.ProxyTracerProvider)
    from semantic_kernel import Kernel
    from semantic_kernel.functions import kernel_function

    class Plugin:
        @kernel_function(name="echo", description="Echo")
        def echo(self, text: str) -> str:
            return text

    kernel = Kernel()
    kernel.add_plugin(Plugin(), "P")
    instrumentor.instrument(tracer_provider=provider)
    result = asyncio.run(kernel.invoke(plugin_name="P", function_name="echo", text="hi"))
    assert str(result) == "hi"

    spans = provider.exporter.get_finished_spans()
    tool = [s for s in spans if s.name == "execute_tool P-echo"]
    assert tool, [s.name for s in spans]
    assert tool[0].attributes["gen_ai.span.kind"] == "CHAIN"  # invoked directly, no tool call id
    assert tool[0].attributes["fi.span.kind"] == "CHAIN"

    # After uninstrument() SK is back on the (unset) global provider: nothing reaches ours.
    instrumentor.uninstrument()
    provider.exporter.clear()
    asyncio.run(kernel.invoke(plugin_name="P", function_name="echo", text="hi"))
    assert provider.exporter.get_finished_spans() == ()


def test_auto_invoked_tool_without_call_id_is_tool(instrumentor, provider):
    """A connector that sends no tool call id (Ollama does not) still yields a TOOL span.

    The fake service returns a ``FunctionCallContent`` without ``id``, as
    ``ollama_chat_completion.py`` ``_parse_tool_calls`` does, so Semantic Kernel's
    own auto function invocation loop runs the tool.
    """
    from typing import ClassVar

    from semantic_kernel import Kernel
    from semantic_kernel.connectors.ai.chat_completion_client_base import ChatCompletionClientBase
    from semantic_kernel.connectors.ai.function_choice_behavior import FunctionChoiceBehavior
    from semantic_kernel.connectors.ai.prompt_execution_settings import PromptExecutionSettings
    from semantic_kernel.contents import AuthorRole, ChatHistory, ChatMessageContent, FunctionCallContent
    from semantic_kernel.contents import FunctionResultContent
    from semantic_kernel.functions import KernelArguments, kernel_function

    class Plugin:
        @kernel_function(name="echo", description="Echo")
        def echo(self, text: str) -> str:
            return text

    class IdlessToolCaller(ChatCompletionClientBase):
        SUPPORTS_FUNCTION_CALLING: ClassVar[bool] = True

        async def _inner_get_chat_message_contents(self, chat_history, settings):
            answered = any(isinstance(i, FunctionResultContent) for m in chat_history.messages for i in m.items)
            if answered:
                return [ChatMessageContent(role=AuthorRole.ASSISTANT, content="done")]
            call = FunctionCallContent(name="P-echo", arguments='{"text": "hi"}')
            return [ChatMessageContent(role=AuthorRole.ASSISTANT, items=[call])]

    kernel = Kernel()
    kernel.add_plugin(Plugin(), "P")
    service = IdlessToolCaller(ai_model_id="fake-model", service_id="fake")
    instrumentor.instrument(tracer_provider=provider)

    history = ChatHistory()
    history.add_user_message("echo hi")
    settings = PromptExecutionSettings(function_choice_behavior=FunctionChoiceBehavior.Auto())
    result = asyncio.run(
        service.get_chat_message_contents(history, settings, kernel=kernel, arguments=KernelArguments())
    )
    assert str(result[0]) == "done"

    spans = {s.name: s for s in provider.exporter.get_finished_spans()}
    tool, loop = spans["execute_tool P-echo"], spans["AutoFunctionInvocationLoop"]
    assert "gen_ai.tool.call.id" not in tool.attributes
    assert tool.parent.span_id == loop.context.span_id
    assert tool.attributes["fi.span.kind"] == "TOOL"
    assert tool.attributes["gen_ai.span.kind"] == "TOOL"
    assert loop.attributes["fi.span.kind"] == "CHAIN"


def test_uninstrument_restores_module_tracers(provider):
    originals = {name: importlib.import_module(name).tracer for name in TRACER_MODULES}
    inst = SemanticKernelInstrumentor()
    inst.instrument(tracer_provider=provider)
    for name in TRACER_MODULES:
        assert importlib.import_module(name).tracer is not originals[name], name
    inst.uninstrument()
    for name in TRACER_MODULES:
        assert importlib.import_module(name).tracer is originals[name], name


def test_non_sdk_provider_warns_and_changes_nothing(instrumentor, caplog):
    """A provider without an SDK span-processor chain must not break host startup."""
    originals = {name: importlib.import_module(name).tracer for name in TRACER_MODULES}
    assert isinstance(trace_api.get_tracer_provider(), trace_api.ProxyTracerProvider)
    with caplog.at_level(logging.WARNING, logger="traceai_semantic_kernel"):
        instrumentor.instrument(tracer_provider=trace_api.NoOpTracerProvider())
        instrumentor.instrument()  # global provider not set yet: a ProxyTracerProvider
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("NoOpTracerProvider" in m and "register" in m for m in warnings), warnings
    assert any("ProxyTracerProvider" in m for m in warnings), warnings
    assert not instrumentor.is_instrumented
    assert not any(s.enable_otel_diagnostics for s in _settings())
    for name in TRACER_MODULES:
        assert importlib.import_module(name).tracer is originals[name], name
    instrumentor.uninstrument()  # still safe


def test_moved_sk_module_is_skipped_with_a_warning(instrumentor, provider, monkeypatch, caplog):
    """An experimental module that moves in a future Semantic Kernel is skipped, not raised."""
    import traceai_semantic_kernel as package

    missing = "semantic_kernel.utils.telemetry.moved_in_a_future_release"
    monkeypatch.setattr(package, "DIAGNOSTICS_SETTINGS_MODULES", (missing,) + DIAGNOSTICS_SETTINGS_MODULES)
    monkeypatch.setattr(package, "TRACER_MODULES", TRACER_MODULES + (missing,))
    originals = {name: importlib.import_module(name).tracer for name in TRACER_MODULES}

    with caplog.at_level(logging.WARNING, logger="traceai_semantic_kernel"):
        instrumentor.instrument(tracer_provider=provider)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(missing in m for m in warnings), warnings
    # Everything that does exist is still switched, routed and mapped.
    assert instrumentor.is_instrumented
    assert all(s.enable_otel_diagnostics for s in _settings())
    for name in TRACER_MODULES:
        assert importlib.import_module(name).tracer is not originals[name], name
    assert provider._active_span_processor._span_processors[0] is instrumentor.processor

    instrumentor.uninstrument()
    instrumentor.uninstrument()
    assert not any(s.enable_otel_diagnostics for s in _settings())
    for name in TRACER_MODULES:
        assert importlib.import_module(name).tracer is originals[name], name
    assert not any(
        isinstance(p, SemanticKernelSpanProcessor) for p in provider._active_span_processor._span_processors
    )


def test_later_add_span_processor_drops_ours_until_reinstrumented(monkeypatch):
    """Pins the README ordering note (R4); the reset is fi_instrumentation behaviour.

    fi's TracerProvider.add_span_processor shuts down and empties the chain on
    its first call after construction (fi_instrumentation/otel.py), which also
    removes this package's processor. uninstrument() + instrument() recovers.
    """
    from fi_instrumentation.otel import TracerProvider as FiTracerProvider

    monkeypatch.setenv("FI_API_KEY", "placeholder-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "placeholder-secret-key")
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")  # default exporter is replaced before any span
    fi_provider = FiTracerProvider(verbose=False)
    tracer_scope = "semantic_kernel.utils.telemetry.model_diagnostics.decorators"
    inst = SemanticKernelInstrumentor()
    try:
        inst.instrument(tracer_provider=fi_provider)
        exporter = InMemorySpanExporter()
        fi_provider.add_span_processor(SimpleSpanProcessor(exporter))  # after instrument(): wrong order
        assert inst.processor not in fi_provider._active_span_processor._span_processors
        fi_provider.get_tracer(tracer_scope).start_span("chat m", attributes={"gen_ai.operation.name": "chat"}).end()
        assert "fi.span.kind" not in exporter.get_finished_spans()[-1].attributes

        inst.uninstrument()
        inst.instrument(tracer_provider=fi_provider)
        fi_provider.get_tracer(tracer_scope).start_span("chat m", attributes={"gen_ai.operation.name": "chat"}).end()
        assert exporter.get_finished_spans()[-1].attributes["fi.span.kind"] == "LLM"
    finally:
        inst.uninstrument()


def test_failed_instrument_rolls_back(provider, monkeypatch):
    import traceai_semantic_kernel as package

    originals = {name: importlib.import_module(name).tracer for name in TRACER_MODULES}

    def boom():
        raise RuntimeError("version lookup failed")

    monkeypatch.setattr(package, "_sk_version", boom)
    inst = SemanticKernelInstrumentor()
    with pytest.raises(RuntimeError):
        inst.instrument(tracer_provider=provider)
    assert not inst.is_instrumented
    assert not any(s.enable_otel_diagnostics for s in _settings())
    assert not any(
        isinstance(p, SemanticKernelSpanProcessor) for p in provider._active_span_processor._span_processors
    )
    for name in TRACER_MODULES:
        assert importlib.import_module(name).tracer is originals[name], name


def test_uninstrument_without_instrument_is_a_noop():
    SemanticKernelInstrumentor().uninstrument()
