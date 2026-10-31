"""Unsupported SDKs fail visibly and installation is transactional."""

import asyncio
import sys
from importlib import import_module
from types import ModuleType

import pytest
import traceai_together
from fi_instrumentation import FITracer
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_together import TogetherInstrumentor
from traceai_together._response_attributes_extractor import _StreamingChatResponseAccumulator, _ChatCompletionsResponseAttributesExtractor
from traceai_together._stream import _AsyncStream, _StreamState
from traceai_together._with_span import _WithSpan


def resource_methods():
    result = []
    for module_name, current, legacy, _ in traceai_together._RESOURCES:
        module = import_module(module_name)
        owner = getattr(module, current, None) or getattr(module, legacy)
        result.append((module, owner, vars(owner)["create"]))
    return result


def test_no_supported_resources_is_not_reported_as_instrumented(monkeypatch, caplog):
    methods = resource_methods()
    for module, owner, _ in methods:
        monkeypatch.delattr(module, owner.__name__, raising=False)
    instrumentor = TogetherInstrumentor()
    instrumentor.instrument()
    assert not instrumentor.is_instrumented_by_opentelemetry
    assert "no supported SDK resources" in caplog.text
    for _, owner, original in methods:
        assert vars(owner)["create"] is original


def test_missing_resource_is_reported_and_others_are_traced(monkeypatch, caplog):
    methods = resource_methods()
    module, owner, original = methods[2]
    monkeypatch.delattr(module, owner.__name__)
    instrumentor = TogetherInstrumentor()
    instrumentor.instrument()
    try:
        assert instrumentor.is_instrumented_by_opentelemetry
        assert "these calls will not be traced" in caplog.text
        assert "together.resources.completions" in caplog.text
        assert vars(owner)["create"] is original
        assert vars(methods[0][1])["create"] is not methods[0][2]
    finally:
        instrumentor.uninstrument()
    for _, owner, original in methods:
        assert vars(owner)["create"] is original


def test_partial_install_is_rolled_back(monkeypatch):
    methods = resource_methods()
    original_wrap = traceai_together.wrap_function_wrapper
    calls = 0

    def fail_third(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("synthetic wrapping failure")
        original_wrap(*args, **kwargs)

    monkeypatch.setattr(traceai_together, "wrap_function_wrapper", fail_third)
    instrumentor = TogetherInstrumentor()
    with pytest.raises(RuntimeError, match="synthetic wrapping failure"):
        instrumentor.instrument()
    assert not instrumentor.is_instrumented_by_opentelemetry
    for _, owner, original in methods:
        assert vars(owner)["create"] is original


def test_protect_is_restored(monkeypatch):
    class Protect:
        def protect(self):
            return "unchanged"

    module = ModuleType("fi.evals")
    module.Protect = Protect
    parent = ModuleType("fi")
    parent.evals = module
    monkeypatch.setitem(sys.modules, "fi", parent)
    monkeypatch.setitem(sys.modules, "fi.evals", module)
    monkeypatch.setattr(traceai_together, "Protect", Protect)
    original = vars(Protect)["protect"]
    instrumentor = TogetherInstrumentor()
    instrumentor.instrument()
    try:
        assert vars(Protect)["protect"] is not original
    finally:
        instrumentor.uninstrument()
    assert vars(Protect)["protect"] is original


def test_uninstrument_does_not_clobber_a_later_replacement():
    _, owner, original = resource_methods()[0]
    instrumentor = TogetherInstrumentor()
    instrumentor.instrument()

    def replacement(*args, **kwargs):
        return None

    try:
        owner.create = replacement
        instrumentor.uninstrument()
        assert vars(owner)["create"] is replacement
    finally:
        owner.create = original


def test_async_iteration_cancellation_ends_span():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer("test"))
    state = _StreamState(_WithSpan(tracer.start_span("stream")), _StreamingChatResponseAccumulator(), _ChatCompletionsResponseAttributesExtractor(), {})

    async def cancelled():
        raise asyncio.CancelledError()
        yield  # pragma: no cover - make this an async generator

    async def run():
        stream = _AsyncStream(cancelled(), state)
        with pytest.raises(asyncio.CancelledError):
            await stream.__anext__()
        await stream.aclose()

    try:
        asyncio.run(run())
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
    finally:
        provider.shutdown()
