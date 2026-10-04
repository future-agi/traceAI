"""instrument() / uninstrument() against the installed semantic-kernel.

These run in process. ``instrument()`` changes process-wide Semantic Kernel
state (module-level settings objects and module-level tracers), so every test
uninstruments in a fixture finalizer.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
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


def test_uninstrument_restores_module_tracers(provider):
    originals = {name: importlib.import_module(name).tracer for name in TRACER_MODULES}
    inst = SemanticKernelInstrumentor()
    inst.instrument(tracer_provider=provider)
    for name in TRACER_MODULES:
        assert importlib.import_module(name).tracer is not originals[name], name
    inst.uninstrument()
    for name in TRACER_MODULES:
        assert importlib.import_module(name).tracer is originals[name], name


def test_non_sdk_provider_is_rejected(instrumentor):
    with pytest.raises(TypeError, match="register"):
        instrumentor.instrument(tracer_provider=trace_api.NoOpTracerProvider())
    assert not instrumentor.is_instrumented
    assert not any(s.enable_otel_diagnostics for s in _settings())


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
