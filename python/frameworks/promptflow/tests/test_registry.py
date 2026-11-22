"""The auto registry row for Prompt flow: detection, install hint and activation."""
import sys

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation.auto import _manager, _registry


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(_manager, "_state", _manager._State())
    yield _manager
    _manager.shutdown()


def test_the_registry_row_names_the_package_and_instrumentor():
    row = _registry.find("promptflow")
    assert row is not None
    assert row.targets == ("promptflow",)
    assert row.package == "traceAI-promptflow"
    assert row.module == "traceai_promptflow"
    assert row.kind == _registry.INSTRUMENTOR
    assert row.default is True
    import traceai_promptflow

    assert hasattr(traceai_promptflow, row.entry)


def test_promptflow_is_detected_and_planned_when_installed(auto):
    row = _registry.find("promptflow")
    status, action, installed = auto._plan(row, auto.AutoConfig(), lazy=True)
    assert status.status == auto.PENDING
    assert action in ("now", "hook")  # "now" once promptflow is imported, else on first import
    assert installed == ["promptflow"]


def test_the_install_hint_is_given_when_the_traceai_package_is_missing(auto, monkeypatch):
    monkeypatch.setattr(auto, "module_available", lambda dotted: dotted != "traceai_promptflow")
    row = _registry.find("promptflow")
    status, _action, _ = auto._plan(row, auto.AutoConfig(), lazy=True)
    assert status.status == auto.MISSING_INSTRUMENTOR
    assert "pip install traceAI-promptflow" in status.detail


def test_auto_instrument_activates_promptflow_and_traces_a_function(auto, monkeypatch):
    from promptflow.tracing import _trace, trace

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = _trace.start_as_current_span
    statuses = auto.auto_instrument(tracer_provider=provider, instrumentations=["promptflow"], lazy=False)
    assert statuses["promptflow"] == auto.INSTRUMENTED
    assert _trace.start_as_current_span is not original

    @trace
    def hello():
        return "hi"

    assert hello() == "hi"
    assert [s.name for s in exporter.get_finished_spans()]
    auto.shutdown()
    assert _trace.start_as_current_span is original


def test_zero_code_enables_promptflow_on_the_first_import_of_the_tracing_package(auto, monkeypatch):
    """With promptflow-core / promptflow-tracing only, ``promptflow`` is a namespace package: a hook on its
    import never fires, so the hook has to sit on a module the install really provides."""
    import subprocess

    script = (
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "from fi_instrumentation.auto import _manager\n"
        "_manager.auto_instrument(tracer_provider=TracerProvider(), instrumentations=['promptflow'], lazy=True)\n"
        "assert 'promptflow' not in __import__('sys').modules\n"
        "import promptflow.tracing\n"
        "print({s.name: s.status for s in _manager.report()}['promptflow'])\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "instrumented", out.stdout + out.stderr
