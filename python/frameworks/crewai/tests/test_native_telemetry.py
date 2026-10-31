"""Verify the package's opt-out, including floor versions without disable flags."""

from importlib import import_module
from inspect import getattr_static
from unittest.mock import patch

from crewai.telemetry import Telemetry
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_crewai import CrewAIInstrumentor
from traceai_crewai._telemetry import _NativeTelemetryGuard


def test_guard_native_exporter_never_constructed_and_traceai_still_records(monkeypatch):
    # The import was safe under pre-import flags; now prove instrument() itself
    # disables native telemetry even when those flags are explicitly false.
    for key in (
        "CREWAI_DISABLE_TELEMETRY",
        "CREWAI_DISABLE_TRACKING",
        "OTEL_SDK_DISABLED",
    ):
        monkeypatch.setenv(key, "false")
    if hasattr(Telemetry, "_instance"):
        monkeypatch.setattr(Telemetry, "_instance", None)
    original = getattr_static(Telemetry, "__init__")
    check = getattr_static(Telemetry, "_is_telemetry_disabled", None)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CrewAIInstrumentor()
    # A constructor spy is stronger than an unreachable network endpoint: a
    # native exporter must not even be instantiated in the opted-out branch.
    module = "crewai.telemetry.telemetry"
    target = module + (
        ".SafeOTLPSpanExporter"
        if hasattr(import_module(module), "SafeOTLPSpanExporter")
        else ".OTLPSpanExporter"
    )
    with patch(
        target, side_effect=AssertionError("native exporter constructed")
    ) as native_exporter:
        instrumentor.instrument(tracer_provider=provider)
        try:
            telemetry = Telemetry()
            assert not telemetry.ready
            assert not getattr(telemetry, "provider", None)
            if check is not None:
                assert Telemetry._is_telemetry_disabled()
            with instrumentor._tracer.start_as_current_span("positive-control"):
                pass
            assert len(exporter.get_finished_spans()) == 1
            assert not native_exporter.called
        finally:
            instrumentor.uninstrument()
    assert getattr_static(Telemetry, "__init__") is original
    assert getattr_static(Telemetry, "_is_telemetry_disabled", None) is check
    provider.shutdown()


def test_guard_floor_blocks_existing_instance_and_restores(monkeypatch):
    calls = []

    class LegacyTelemetry:
        def __init__(self):
            self.ready, self.trace_set = True, True
            calls.append("constructor")

        def crew_creation(self, *args):
            calls.append("export")

    legacy = LegacyTelemetry()
    from types import SimpleNamespace
    import traceai_crewai._telemetry as module

    def fake_import(name):
        if name == "crewai.telemetry":
            return SimpleNamespace(Telemetry=LegacyTelemetry)
        raise ModuleNotFoundError(name="crewai_core")

    monkeypatch.setattr(module, "import_module", fake_import)
    guard = _NativeTelemetryGuard()
    guard.enable()
    try:
        assert not LegacyTelemetry().ready
        assert legacy.crew_creation(None, None) is None
        assert calls == ["constructor"]
    finally:
        guard.disable()
    assert LegacyTelemetry().ready
    legacy.crew_creation(None, None)
    assert calls == ["constructor", "constructor", "export"]
