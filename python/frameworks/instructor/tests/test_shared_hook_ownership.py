"""Removing Instructor must not remove or resurrect another Protect wrapper."""

import inspect
import sys
from types import ModuleType

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from wrapt import FunctionWrapper

import traceai_instructor
from traceai_instructor import InstructorInstrumentor


@pytest.mark.parametrize("instructor_first", [True, False])
@pytest.mark.parametrize("remove_instructor_first", [True, False])
def test_shared_protect_hook_coexists_in_both_orders(monkeypatch, instructor_first, remove_instructor_first):
    calls = []
    result = {"status": "passed"}
    class Protect:
        def protect(self, inputs, protect_rules):
            calls.append("original")
            return result
    original = inspect.getattr_static(Protect, "protect")
    fi, evals = ModuleType("fi"), ModuleType("fi.evals")
    fi.evals = evals
    evals.Protect = Protect
    monkeypatch.setitem(sys.modules, "fi", fi)
    monkeypatch.setitem(sys.modules, "fi.evals", evals)
    monkeypatch.setattr(traceai_instructor, "Protect", Protect)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = InstructorInstrumentor()
    other = None

    def install_other():
        def record(wrapped, instance, args, kwargs):
            calls.append("other")
            return wrapped(*args, **kwargs)
        wrapper = FunctionWrapper(inspect.getattr_static(Protect, "protect"), record)
        Protect.protect = wrapper
        return wrapper

    def remove_other():
        # Stand-in integration removes only its own wrapper, including from
        # under Instructor, so this test isolates Instructor's ownership rule.
        current = inspect.getattr_static(Protect, "protect")
        if current is other:
            Protect.protect = other.__wrapped__
        else:
            while hasattr(current, "__wrapped__"):
                if current.__wrapped__ is other:
                    current.__wrapped__ = other.__wrapped__
                    break
                current = current.__wrapped__

    def invoke(expected_other, expected_spans):
        calls.clear()
        exporter.clear()
        assert Protect().protect("input", [{"rule": "local"}]) is result
        assert calls == (["other", "original"] if expected_other else ["original"])
        assert len(exporter.get_finished_spans()) == expected_spans

    try:
        if instructor_first:
            instrumentor.instrument(tracer_provider=provider)
            other = install_other()
        else:
            other = install_other()
            instrumentor.instrument(tracer_provider=provider)
        invoke(True, 1)
        if remove_instructor_first:
            instrumentor.uninstrument()
            invoke(True, 0)
            # A fresh setup/teardown must not duplicate or detach the other hook.
            instrumentor.instrument(tracer_provider=provider)
            invoke(True, 1)
            instrumentor.uninstrument()
            invoke(True, 0)
            remove_other()
        else:
            remove_other()
            invoke(False, 1)
            instrumentor.uninstrument()
        invoke(False, 0)
        assert inspect.getattr_static(Protect, "protect") is original
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        Protect.protect = original
        provider.shutdown()
