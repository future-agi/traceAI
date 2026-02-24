"""Tests for the instrumentor module."""

import os
import pytest
from unittest.mock import MagicMock, patch

from traceai_beeai._instrumentor import (
    BeeAIInstrumentorWrapper,
    configure_beeai_tracing,
)


class TestBeeAIInstrumentorWrapper:
    """Tests for BeeAIInstrumentorWrapper class."""

    def setup_method(self):
        """Reset singleton state before each test."""
        BeeAIInstrumentorWrapper._instance = None
        BeeAIInstrumentorWrapper._is_instrumented = False

    def test_singleton_pattern(self):
        """Test that wrapper follows singleton pattern."""
        wrapper1 = BeeAIInstrumentorWrapper()
        wrapper2 = BeeAIInstrumentorWrapper()

        assert wrapper1 is wrapper2

    def test_instrument_sets_environment_variables(self, mock_tracer_provider):
        """Test that instrumentation sets OTEL environment variables."""
        wrapper = BeeAIInstrumentorWrapper()

        with patch.dict(os.environ, {}, clear=True):
            wrapper.instrument(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318/v1/traces",
                otlp_headers={"Authorization": "Bearer token"},
            )

            assert os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") == "http://localhost:4318/v1/traces"
            assert "Authorization=Bearer token" in os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")

    def test_instrument_returns_self(self, mock_tracer_provider):
        """Test that instrument returns self for method chaining."""
        wrapper = BeeAIInstrumentorWrapper()

        result = wrapper.instrument(tracer_provider=mock_tracer_provider)

        assert result is wrapper

    def test_instrument_idempotent(self, mock_tracer_provider):
        """Test that calling instrument multiple times is safe."""
        wrapper = BeeAIInstrumentorWrapper()

        with patch.dict(os.environ, {}, clear=True):
            wrapper.instrument(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://first:4318",
            )
            wrapper.instrument(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://second:4318",
            )

            # Should keep first value since already instrumented
            assert os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") == "http://first:4318"

    def test_is_instrumented_property(self, mock_tracer_provider):
        """Test is_instrumented property."""
        wrapper = BeeAIInstrumentorWrapper()

        assert wrapper.is_instrumented is False

        wrapper.instrument(tracer_provider=mock_tracer_provider)

        assert wrapper.is_instrumented is True

    def test_uninstrument(self, mock_tracer_provider):
        """Test uninstrumentation."""
        wrapper = BeeAIInstrumentorWrapper()

        wrapper.instrument(tracer_provider=mock_tracer_provider)
        assert wrapper.is_instrumented is True

        wrapper.uninstrument()
        assert wrapper.is_instrumented is False


class TestConfigureBeeAITracing:
    """Tests for configure_beeai_tracing function."""

    def setup_method(self):
        """Reset singleton state before each test."""
        BeeAIInstrumentorWrapper._instance = None
        BeeAIInstrumentorWrapper._is_instrumented = False

    def test_returns_wrapper(self, mock_tracer_provider):
        """Test that function returns BeeAIInstrumentorWrapper."""
        result = configure_beeai_tracing(tracer_provider=mock_tracer_provider)

        assert isinstance(result, BeeAIInstrumentorWrapper)

    def test_with_otlp_endpoint(self, mock_tracer_provider):
        """Test configuration with OTLP endpoint."""
        with patch.dict(os.environ, {}, clear=True):
            configure_beeai_tracing(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318/v1/traces",
            )

            assert os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") == "http://localhost:4318/v1/traces"

    def test_with_project_name(self, mock_tracer_provider):
        """Test configuration with project name."""
        with patch.dict(os.environ, {}, clear=True):
            configure_beeai_tracing(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318/v1/traces",
                project_name="my-project",
            )

            headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
            assert "x-project-name=my-project" in headers

    def test_with_headers(self, mock_tracer_provider):
        """Test configuration with custom headers."""
        with patch.dict(os.environ, {}, clear=True):
            configure_beeai_tracing(
                tracer_provider=mock_tracer_provider,
                otlp_endpoint="http://localhost:4318/v1/traces",
                otlp_headers={"Authorization": "Bearer token"},
            )

            headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
            assert "Authorization=Bearer token" in headers
