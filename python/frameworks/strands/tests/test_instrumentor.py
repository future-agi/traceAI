"""Tests for the instrumentor module."""

import os
import sys
from types import SimpleNamespace

import pytest
from unittest.mock import MagicMock, patch

from traceai_strands._instrumentor import (
    StrandsInstrumentor,
    configure_strands_tracing,
    create_traced_agent,
)


class TestStrandsInstrumentor:
    """Tests for StrandsInstrumentor class."""

    def setup_method(self):
        """Reset singleton state before each test."""
        StrandsInstrumentor._instance = None
        StrandsInstrumentor._is_instrumented = False

    def test_singleton_pattern(self):
        """Test that instrumentor follows singleton pattern."""
        inst1 = StrandsInstrumentor()
        inst2 = StrandsInstrumentor()

        assert inst1 is inst2

    def test_instrument_sets_environment_variables(self, mock_tracer_provider):
        """Test that instrumentation sets OTEL environment variables."""
        instrumentor = StrandsInstrumentor()

        with patch.dict(os.environ, {}, clear=True):
            instrumentor.instrument(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318",
                otlp_headers={"Authorization": "Bearer token"},
            )

            assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://localhost:4318"
            assert "Authorization=Bearer token" in os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")

    def test_instrument_returns_self(self, mock_tracer_provider):
        """Test that instrument returns self for method chaining."""
        instrumentor = StrandsInstrumentor()

        result = instrumentor.instrument(tracer_provider=mock_tracer_provider)

        assert result is instrumentor

    def test_instrument_idempotent(self, mock_tracer_provider):
        """Test that calling instrument multiple times is safe."""
        instrumentor = StrandsInstrumentor()

        with patch.dict(os.environ, {}, clear=True):
            instrumentor.instrument(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://first:4318",
            )
            instrumentor.instrument(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://second:4318",
            )

            # Should keep first value since already instrumented
            assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://first:4318"

    def test_is_instrumented_property(self, mock_tracer_provider):
        """Test is_instrumented property."""
        instrumentor = StrandsInstrumentor()

        assert instrumentor.is_instrumented is False

        instrumentor.instrument(tracer_provider=mock_tracer_provider)

        assert instrumentor.is_instrumented is True

    def test_uninstrument(self, mock_tracer_provider):
        """Test uninstrumentation."""
        instrumentor = StrandsInstrumentor()

        instrumentor.instrument(tracer_provider=mock_tracer_provider)
        assert instrumentor.is_instrumented is True

        instrumentor.uninstrument()
        assert instrumentor.is_instrumented is False


class TestConfigureStrandsTracing:
    """Tests for configure_strands_tracing function."""

    def setup_method(self):
        """Reset singleton state before each test."""
        StrandsInstrumentor._instance = None
        StrandsInstrumentor._is_instrumented = False

    def test_returns_instrumentor(self, mock_tracer_provider):
        """Test that function returns StrandsInstrumentor."""
        result = configure_strands_tracing(tracer_provider=mock_tracer_provider)

        assert isinstance(result, StrandsInstrumentor)

    def test_with_otlp_endpoint(self, mock_tracer_provider):
        """Test configuration with OTLP endpoint."""
        with patch.dict(os.environ, {}, clear=True):
            configure_strands_tracing(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318",
            )

            assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://localhost:4318"

    def test_with_project_name(self, mock_tracer_provider):
        """Test configuration with project name."""
        with patch.dict(os.environ, {}, clear=True):
            configure_strands_tracing(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318",
                project_name="my-project",
            )

            headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
            assert "x-project-name=my-project" in headers

    def test_with_headers(self, mock_tracer_provider):
        """Test configuration with custom headers."""
        with patch.dict(os.environ, {}, clear=True):
            configure_strands_tracing(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318",
                otlp_headers={"Authorization": "Bearer token"},
            )

            headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
            assert "Authorization=Bearer token" in headers


class TestCreateTracedAgent:
    """Tests for create_traced_agent function."""

    def test_creates_agent_with_trace_attributes(self):
        """Test creating agent with trace attributes."""
        # Mock strands.Agent
        mock_agent_class = MagicMock()
        mock_agent_instance = MagicMock()
        mock_agent_class.return_value = mock_agent_instance

        with patch.dict("sys.modules", {"strands": MagicMock(Agent=mock_agent_class)}):
            from traceai_strands._instrumentor import create_traced_agent

            agent = create_traced_agent(
                model="gpt-4",
                system_prompt="You are helpful.",
                session_id="sess-123",
                user_id="user@example.com",
                tags=["production"],
            )

            # Verify Agent was called with trace_attributes
            call_kwargs = mock_agent_class.call_args[1]
            assert "trace_attributes" in call_kwargs
            assert call_kwargs["trace_attributes"]["session.id"] == "sess-123"
            assert call_kwargs["trace_attributes"]["user.id"] == "user@example.com"
            assert call_kwargs["trace_attributes"]["tags"] == ["production"]

    def test_merges_existing_trace_attributes(self):
        """Test that existing trace_attributes are merged."""
        mock_agent_class = MagicMock()
        mock_agent_instance = MagicMock()
        mock_agent_class.return_value = mock_agent_instance

        with patch.dict("sys.modules", {"strands": MagicMock(Agent=mock_agent_class)}):
            from traceai_strands._instrumentor import create_traced_agent

            agent = create_traced_agent(
                model="gpt-4",
                system_prompt="You are helpful.",
                session_id="sess-123",
                trace_attributes={"custom": "value"},
            )

            call_kwargs = mock_agent_class.call_args[1]
            assert call_kwargs["trace_attributes"]["session.id"] == "sess-123"
            assert call_kwargs["trace_attributes"]["custom"] == "value"


class _RecordingStrandsTelemetry:
    """Stands in for strands.telemetry.StrandsTelemetry and records which exporters were set up."""

    calls: list = []

    def __init__(self, tracer_provider=None):
        self.tracer_provider = tracer_provider

    def setup_otlp_exporter(self, **kwargs):
        self.calls.append("otlp")
        return self

    def setup_console_exporter(self, **kwargs):
        self.calls.append("console")
        return self

    def setup_meter(self, **kwargs):
        self.calls.append("meter")
        return self


def _provider_exporting_to(endpoint, batch=False):
    """A tracer provider shaped like the SDK's, with one processor exporting to `endpoint`."""
    exporter = SimpleNamespace(_endpoint=endpoint)
    if batch:
        # Recent OTel SDKs keep a BatchSpanProcessor's exporter on an inner _batch_processor.
        processor = SimpleNamespace(_batch_processor=SimpleNamespace(_exporter=exporter))
    else:
        processor = SimpleNamespace(span_exporter=exporter)
    return SimpleNamespace(
        _active_span_processor=SimpleNamespace(_span_processors=(processor,))
    )


_FUTURE_AGI = "http://127.0.0.1:5999/tracer/v1/traces"


class TestStrandsOtlpExporterIsNotDoubled:
    """Strands' OTLP exporter is not added for a collector the provider already exports to.

    With register()'s provider, the Future AGI exporter already sends every span. Pointing
    OTEL_EXPORTER_OTLP_* at Future AGI as well used to add a second exporter for the same
    collector, so every span arrived twice once fi-instrumentation-otel stopped dropping
    register()'s exporter on add_span_processor() (TH-8727).
    """

    @pytest.fixture(autouse=True)
    def _recording_telemetry(self, monkeypatch):
        StrandsInstrumentor._instance = None
        StrandsInstrumentor._is_instrumented = False
        _RecordingStrandsTelemetry.calls = []
        monkeypatch.setattr(
            sys.modules["strands.telemetry"], "StrandsTelemetry", _RecordingStrandsTelemetry
        )
        for name in ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
            monkeypatch.delenv(name, raising=False)
        yield
        StrandsInstrumentor._instance = None
        StrandsInstrumentor._is_instrumented = False

    @pytest.mark.parametrize("batch", [False, True], ids=["simple", "batch"])
    def test_not_added_when_the_traces_endpoint_is_the_providers_collector(
        self, monkeypatch, batch
    ):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI, batch=batch),
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == []

    def test_not_added_when_the_base_endpoint_is_the_providers_collector(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:5999/")

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == []

    def test_not_added_when_otlp_endpoint_is_the_providers_collector(self, monkeypatch):
        # configure_strands_tracing() stores otlp_endpoint in OTEL_EXPORTER_OTLP_ENDPOINT;
        # monkeypatch puts the variable back afterwards.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to("https://api.futureagi.com/tracer/v1/traces"),
            otlp_endpoint="https://API.futureagi.com:443",
            enable_metrics=False,
        )

        assert _RecordingStrandsTelemetry.calls == []

    def test_console_exporter_and_meter_are_still_set_up(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI),
            enable_console_exporter=True,
            enable_metrics=True,
        )

        assert _RecordingStrandsTelemetry.calls == ["console", "meter"]

    @pytest.mark.parametrize(
        "traces_endpoint",
        [
            "http://127.0.0.1:6000/v1/traces",  # another port
            "http://collector.internal:5999/v1/traces",  # another host
        ],
    )
    def test_added_for_another_collector(self, monkeypatch, traces_endpoint):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", traces_endpoint)

        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    def test_added_for_the_default_endpoint_when_nothing_is_set(self):
        configure_strands_tracing(
            tracer_provider=_provider_exporting_to(_FUTURE_AGI), enable_metrics=False
        )

        assert _RecordingStrandsTelemetry.calls == ["otlp"]

    def test_added_when_the_provider_has_no_span_processors(
        self, monkeypatch, mock_tracer_provider
    ):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", _FUTURE_AGI)

        configure_strands_tracing(tracer_provider=mock_tracer_provider, enable_metrics=False)

        assert _RecordingStrandsTelemetry.calls == ["otlp"]
