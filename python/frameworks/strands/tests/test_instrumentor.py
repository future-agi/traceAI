"""Tests for the instrumentor module."""

import os
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
