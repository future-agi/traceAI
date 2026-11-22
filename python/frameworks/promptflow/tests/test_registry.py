"""The auto registry row for Prompt flow: detection, install hint and activation.

These tests run against the fi-instrumentation-otel the environment resolved, which can be any release the
package allows (``>=1.1.0``): releases before 1.4.0 have no ``fi_instrumentation.auto``, 1.4.0 and 1.5.0 have
no ``promptflow`` row, and 1.5.1 hooks the row on ``promptflow``. Tests that need the row, or the row hooked on
``promptflow.tracing``, are skipped with the reason when the installed release does not have it.
"""
import importlib
import subprocess
import sys
import types
from importlib.metadata import version

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

TRACING_TARGETS = ("promptflow.tracing",)
CORE_VERSION = version("fi-instrumentation-otel")

try:
    _manager = importlib.import_module("fi_instrumentation.auto._manager")
    _registry = importlib.import_module("fi_instrumentation.auto._registry")
except ImportError:
    _manager = _registry = None

INSTALLED_ROW = _registry.find("promptflow") if _registry is not None else None


def row_skip_reason(row, auto_available=True):
    """Why a test that needs a ``promptflow`` row cannot run, or None when it can."""
    if not auto_available:
        return f"fi-instrumentation-otel {CORE_VERSION} has no fi_instrumentation.auto"
    if row is None:
        return f"fi-instrumentation-otel {CORE_VERSION} has no promptflow row in fi_instrumentation.auto"
    return None


def tracing_row_skip_reason(row, auto_available=True):
    """Why a test that needs the row hooked on ``promptflow.tracing`` cannot run, or None when it can."""
    reason = row_skip_reason(row, auto_available)
    if reason is None and tuple(row.targets) != TRACING_TARGETS:
        reason = (
            f"fi-instrumentation-otel {CORE_VERSION} hooks promptflow on {tuple(row.targets)!r}, "
            f"not {TRACING_TARGETS!r}; zero-code tracing of a promptflow-core install needs the release with that row"
        )
    return reason


_ROW_REASON = row_skip_reason(INSTALLED_ROW, _registry is not None)
_TRACING_REASON = tracing_row_skip_reason(INSTALLED_ROW, _registry is not None)
needs_row = pytest.mark.skipif(_ROW_REASON is not None, reason=_ROW_REASON or "")
needs_tracing_row = pytest.mark.skipif(_TRACING_REASON is not None, reason=_TRACING_REASON or "")


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(_manager, "_state", _manager._State())
    yield _manager
    _manager.shutdown()


# --- the skip itself: it runs on every fi-instrumentation-otel -----------------------------------
def test_the_tracing_row_skip_triggers_only_when_the_installed_row_is_not_promptflow_tracing():
    new_row = types.SimpleNamespace(targets=("promptflow.tracing",))
    old_row = types.SimpleNamespace(targets=("promptflow",))
    assert tracing_row_skip_reason(new_row) is None
    assert row_skip_reason(new_row) is None and row_skip_reason(old_row) is None
    reason = tracing_row_skip_reason(old_row)
    assert reason is not None and "('promptflow',)" in reason and CORE_VERSION in reason
    # A release without the row, or without fi_instrumentation.auto, cannot run these tests either.
    assert tracing_row_skip_reason(None) is not None and row_skip_reason(None) is not None
    assert tracing_row_skip_reason(new_row, auto_available=False) is not None


def test_the_row_dependent_tests_carry_the_skip():
    def marks(test):
        return [m for m in getattr(test, "pytestmark", []) if m.name == "skipif"]

    for test in (test_the_registry_row_hooks_promptflow_tracing,
                 test_promptflow_tracing_is_detected_on_a_promptflow_core_install,
                 test_zero_code_enables_promptflow_on_the_first_import_of_the_tracing_package):
        assert [(m.args, m.kwargs) for m in marks(test)] == [(needs_tracing_row.args, needs_tracing_row.kwargs)]


# --- any release with a promptflow row ------------------------------------------------------------
@needs_row
def test_the_registry_row_names_the_package_and_instrumentor():
    row = INSTALLED_ROW
    assert row.package == "traceAI-promptflow"
    assert row.module == "traceai_promptflow"
    assert row.kind == _registry.INSTRUMENTOR
    assert row.default is True
    import traceai_promptflow

    assert hasattr(traceai_promptflow, row.entry)


@needs_row
def test_promptflow_is_detected_and_planned_when_installed(auto):
    row = INSTALLED_ROW
    status, action, installed = auto._plan(row, auto.AutoConfig(), lazy=True)
    assert status.status == auto.PENDING
    assert action in ("now", "hook")  # "now" once promptflow is imported, else on first import
    assert installed == list(row.targets)


@needs_row
def test_the_install_hint_is_given_when_the_traceai_package_is_missing(auto, monkeypatch):
    monkeypatch.setattr(auto, "module_available", lambda dotted: dotted != "traceai_promptflow")
    status, _action, _ = auto._plan(INSTALLED_ROW, auto.AutoConfig(), lazy=True)
    assert status.status == auto.MISSING_INSTRUMENTOR
    assert "pip install traceAI-promptflow" in status.detail


@needs_row
def test_auto_instrument_activates_promptflow_and_traces_a_function(auto):
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


# --- only with the row hooked on promptflow.tracing -----------------------------------------------
@needs_tracing_row
def test_the_registry_row_hooks_promptflow_tracing():
    assert INSTALLED_ROW.targets == TRACING_TARGETS


@needs_tracing_row
def test_promptflow_tracing_is_detected_on_a_promptflow_core_install(auto):
    _status, _action, installed = auto._plan(INSTALLED_ROW, auto.AutoConfig(), lazy=True)
    assert installed == ["promptflow.tracing"]


@needs_tracing_row
def test_zero_code_enables_promptflow_on_the_first_import_of_the_tracing_package():
    """With promptflow-core / promptflow-tracing only, ``promptflow`` is a namespace package: a hook on its
    import never fires, so the hook has to sit on a module the install really provides."""
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
