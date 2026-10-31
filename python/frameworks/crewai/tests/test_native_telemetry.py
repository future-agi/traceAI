"""Native telemetry belongs to the application, not the instrumentor."""

import inspect
import os
from importlib import import_module
from importlib.metadata import version
from unittest.mock import patch

import pytest
from crewai.telemetry import Telemetry
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_crewai import CrewAIInstrumentor


@pytest.mark.parametrize("disabled", ["true", "false"])
def test_native_telemetry_instrumentation_does_not_change_user_settings(
    monkeypatch, disabled
):
    for key in (
        "CREWAI_DISABLE_TELEMETRY",
        "CREWAI_DISABLE_TRACKING",
        "OTEL_SDK_DISABLED",
    ):
        monkeypatch.setenv(key, disabled)
    before_env = dict(os.environ)
    before = dict(vars(Telemetry))
    provider = TracerProvider(shutdown_on_exit=False)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CrewAIInstrumentor()
    # Instrumentation must not instantiate or patch the native singleton/provider.
    with patch.object(
        Telemetry, "__init__", side_effect=AssertionError("native construction")
    ):
        init = Telemetry.__init__
        instrumentor.instrument(tracer_provider=provider)
        try:
            assert Telemetry.__init__ is init
            assert dict(os.environ) == before_env
            for key, value in before.items():
                if key != "__init__":
                    assert vars(Telemetry)[key] is value
            with instrumentor._tracer.start_as_current_span("positive-control"):
                pass
            assert len(exporter.get_finished_spans()) == (
                0 if disabled == "true" else 1
            )
        finally:
            instrumentor.uninstrument()
    assert dict(vars(Telemetry)) == before
    assert dict(os.environ) == before_env
    provider.shutdown()


def test_native_telemetry_installed_source_and_opt_out_flags(monkeypatch):
    if version("crewai") == "0.41.1":
        # This version has no native opt-out check at all. Do not advertise a
        # variable it ignores, or instantiate its exporter to demonstrate it.
        source = inspect.getsource(Telemetry)
        assert not hasattr(Telemetry, "_is_telemetry_disabled")
        for flag in (
            "CREWAI_DISABLE_TELEMETRY",
            "CREWAI_DISABLE_TRACKING",
            "OTEL_SDK_DISABLED",
            "CREWAI_TELEMETRY_OPT_OUT",
            "DO_NOT_TRACK",
        ):
            assert flag not in source
        assert "https://telemetry.crewai.com:4319" in source
        return
    # Maintained CrewAI delegates to crewai-core; the repository-locked 0.152
    # implements the same checks in CrewAI itself.
    cls = (
        import_module("crewai_core.telemetry").Telemetry
        if version("crewai") == "1.15.33"
        else Telemetry
    )
    honored = (
        "CREWAI_DISABLE_TELEMETRY",
        "CREWAI_DISABLE_TRACKING",
        "OTEL_SDK_DISABLED",
    )
    ignored = ("CREWAI_TELEMETRY_OPT_OUT", "DO_NOT_TRACK")
    source = inspect.getsource(cls._is_telemetry_disabled)
    for flag in honored + ignored:
        monkeypatch.setenv(flag, "false")
    assert not Telemetry._is_telemetry_disabled()
    for flag in honored:
        assert flag in source
        monkeypatch.setenv(flag, "true")
        assert Telemetry._is_telemetry_disabled()
        monkeypatch.setenv(flag, "false")
    for flag in ignored:
        assert flag not in source
        monkeypatch.setenv(flag, "true")
    assert not Telemetry._is_telemetry_disabled()
