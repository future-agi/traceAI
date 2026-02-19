"""Tests for traceai_agno._instrumentor module."""

import os
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from traceai_agno._instrumentor import (
    AgnoInstrumentorWrapper,
    configure_agno_tracing,
    setup_traceai_exporter,
)


class TestAgnoInstrumentorWrapper:
    """Tests for AgnoInstrumentorWrapper class."""

    def test_singleton_pattern(self, reset_instrumentor):
        """Test that wrapper uses singleton pattern."""
        wrapper1 = AgnoInstrumentorWrapper()
        wrapper2 = AgnoInstrumentorWrapper()

        assert wrapper1 is wrapper2

    def test_initial_state(self, reset_instrumentor):
        """Test initial state of wrapper."""
        wrapper = AgnoInstrumentorWrapper()

        assert wrapper.is_instrumented is False
        assert wrapper._tracer_provider is None
        assert wrapper._agno_instrumentor is None

    @patch.dict(os.environ, {}, clear=True)
    def test_instrument_sets_otlp_endpoint(self, reset_instrumentor):
        """Test that instrument sets OTLP endpoint environment variable."""
        wrapper = AgnoInstrumentorWrapper()

        with patch.object(wrapper, "_setup_agno_instrumentor"):
            wrapper.instrument(otlp_endpoint="https://api.test.com/v1/traces")

        assert os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") == "https://api.test.com/v1/traces"

    @patch.dict(os.environ, {}, clear=True)
    def test_instrument_sets_otlp_headers(self, reset_instrumentor):
        """Test that instrument sets OTLP headers environment variable."""
        wrapper = AgnoInstrumentorWrapper()

        with patch.object(wrapper, "_setup_agno_instrumentor"):
            wrapper.instrument(
                otlp_headers={
                    "Authorization": "Bearer test-key",
                    "x-project-name": "test-project",
                }
            )

        headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
        assert "Authorization=Bearer test-key" in headers
        assert "x-project-name=test-project" in headers

    def test_instrument_with_tracer_provider(self, reset_instrumentor, mock_tracer_provider):
        """Test instrumentation with custom tracer provider."""
        wrapper = AgnoInstrumentorWrapper()

        with patch.object(wrapper, "_setup_agno_instrumentor"):
            with patch("traceai_agno._instrumentor.trace") as mock_trace:
                wrapper.instrument(tracer_provider=mock_tracer_provider)

        mock_trace.set_tracer_provider.assert_called_once_with(mock_tracer_provider)
        assert wrapper._tracer_provider is mock_tracer_provider

    def test_instrument_idempotent(self, reset_instrumentor):
        """Test that calling instrument multiple times is idempotent."""
        wrapper = AgnoInstrumentorWrapper()

        with patch.object(wrapper, "_setup_agno_instrumentor") as mock_setup:
            wrapper.instrument()
            wrapper.instrument()
            wrapper.instrument()

        # Should only be called once
        mock_setup.assert_called_once()

    def test_instrument_returns_self(self, reset_instrumentor):
        """Test that instrument returns self for chaining."""
        wrapper = AgnoInstrumentorWrapper()

        with patch.object(wrapper, "_setup_agno_instrumentor"):
            result = wrapper.instrument()

        assert result is wrapper

    def test_is_instrumented_property(self, reset_instrumentor):
        """Test is_instrumented property."""
        wrapper = AgnoInstrumentorWrapper()

        assert wrapper.is_instrumented is False

        with patch.object(wrapper, "_setup_agno_instrumentor"):
            wrapper.instrument()

        assert wrapper.is_instrumented is True

    def test_uninstrument(self, reset_instrumentor):
        """Test uninstrument method."""
        wrapper = AgnoInstrumentorWrapper()

        mock_instrumentor = MagicMock()
        wrapper._agno_instrumentor = mock_instrumentor
        wrapper._is_instrumented = True
        wrapper._tracer_provider = MagicMock()

        wrapper.uninstrument()

        mock_instrumentor.uninstrument.assert_called_once()
        assert wrapper.is_instrumented is False
        assert wrapper._tracer_provider is None

    def test_uninstrument_when_not_instrumented(self, reset_instrumentor):
        """Test uninstrument when not instrumented."""
        wrapper = AgnoInstrumentorWrapper()

        # Should not raise
        wrapper.uninstrument()

        assert wrapper.is_instrumented is False

    def test_uninstrument_handles_exception(self, reset_instrumentor):
        """Test uninstrument handles exception from underlying instrumentor."""
        wrapper = AgnoInstrumentorWrapper()

        mock_instrumentor = MagicMock()
        mock_instrumentor.uninstrument.side_effect = Exception("Test error")
        wrapper._agno_instrumentor = mock_instrumentor
        wrapper._is_instrumented = True

        # Should not raise
        wrapper.uninstrument()

        assert wrapper.is_instrumented is False

    def test_setup_agno_instrumentor_success(self, reset_instrumentor):
        """Test successful setup of Agno instrumentor."""
        wrapper = AgnoInstrumentorWrapper()

        # Create a mock module with AgnoInstrumentor
        mock_instrumentor = MagicMock()
        mock_agno_module = MagicMock()
        mock_agno_module.AgnoInstrumentor = MagicMock(return_value=mock_instrumentor)

        with patch.dict("sys.modules", {"openinference.instrumentation.agno": mock_agno_module}):
            # Call the internal setup method
            wrapper._setup_agno_instrumentor()

            # Verify instrumentor was created and instrument() was called
            mock_agno_module.AgnoInstrumentor.assert_called_once()
            mock_instrumentor.instrument.assert_called_once()

    def test_setup_agno_instrumentor_import_error(self, reset_instrumentor):
        """Test handling of ImportError when agno instrumentor not installed."""
        wrapper = AgnoInstrumentorWrapper()

        # This will raise ImportError since openinference-instrumentation-agno
        # is not actually installed in test environment
        # The instrument() method handles this gracefully
        with patch.object(wrapper, "_setup_agno_instrumentor", side_effect=ImportError("not installed")):
            # Should not raise
            wrapper.instrument()

        assert wrapper.is_instrumented is True


class TestConfigureAgnoTracing:
    """Tests for configure_agno_tracing function."""

    def test_returns_wrapper(self, reset_instrumentor):
        """Test that configure_agno_tracing returns wrapper instance."""
        with patch.object(AgnoInstrumentorWrapper, "instrument", return_value=AgnoInstrumentorWrapper()):
            result = configure_agno_tracing()

        assert isinstance(result, AgnoInstrumentorWrapper)

    def test_passes_tracer_provider(self, reset_instrumentor, mock_tracer_provider):
        """Test that tracer_provider is passed to instrument."""
        with patch.object(AgnoInstrumentorWrapper, "instrument") as mock_instrument:
            mock_instrument.return_value = AgnoInstrumentorWrapper()
            configure_agno_tracing(tracer_provider=mock_tracer_provider)

        mock_instrument.assert_called_once()
        call_kwargs = mock_instrument.call_args[1]
        assert call_kwargs["tracer_provider"] is mock_tracer_provider

    def test_passes_otlp_endpoint(self, reset_instrumentor):
        """Test that otlp_endpoint is passed to instrument."""
        with patch.object(AgnoInstrumentorWrapper, "instrument") as mock_instrument:
            mock_instrument.return_value = AgnoInstrumentorWrapper()
            configure_agno_tracing(otlp_endpoint="https://api.test.com/v1/traces")

        call_kwargs = mock_instrument.call_args[1]
        assert call_kwargs["otlp_endpoint"] == "https://api.test.com/v1/traces"

    def test_passes_otlp_headers(self, reset_instrumentor):
        """Test that otlp_headers are passed to instrument."""
        headers = {"Authorization": "Bearer test-key"}

        with patch.object(AgnoInstrumentorWrapper, "instrument") as mock_instrument:
            mock_instrument.return_value = AgnoInstrumentorWrapper()
            configure_agno_tracing(otlp_headers=headers)

        call_kwargs = mock_instrument.call_args[1]
        assert call_kwargs["otlp_headers"] == headers

    def test_project_name_added_to_headers(self, reset_instrumentor):
        """Test that project_name is added to headers."""
        with patch.object(AgnoInstrumentorWrapper, "instrument") as mock_instrument:
            mock_instrument.return_value = AgnoInstrumentorWrapper()
            configure_agno_tracing(project_name="my-project")

        call_kwargs = mock_instrument.call_args[1]
        assert call_kwargs["otlp_headers"]["x-project-name"] == "my-project"

    def test_project_name_added_to_existing_headers(self, reset_instrumentor):
        """Test that project_name is added to existing headers."""
        headers = {"Authorization": "Bearer test-key"}

        with patch.object(AgnoInstrumentorWrapper, "instrument") as mock_instrument:
            mock_instrument.return_value = AgnoInstrumentorWrapper()
            configure_agno_tracing(otlp_headers=headers, project_name="my-project")

        call_kwargs = mock_instrument.call_args[1]
        assert call_kwargs["otlp_headers"]["Authorization"] == "Bearer test-key"
        assert call_kwargs["otlp_headers"]["x-project-name"] == "my-project"


class TestSetupTraceaiExporter:
    """Tests for setup_traceai_exporter function."""

    def test_creates_tracer_provider(self):
        """Test that function creates a tracer provider."""
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"):
            with patch("traceai_agno._instrumentor.trace") as mock_trace:
                provider = setup_traceai_exporter(endpoint="https://api.test.com/v1/traces")

                assert provider is not None
                mock_trace.set_tracer_provider.assert_called_once()

    def test_creates_otlp_exporter(self):
        """Test that function creates OTLP exporter with correct params."""
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter") as mock_exporter_class:
            with patch("traceai_agno._instrumentor.trace"):
                setup_traceai_exporter(
                    endpoint="https://api.test.com/v1/traces",
                    headers={"Authorization": "Bearer test-key"},
                )

                mock_exporter_class.assert_called_once_with(
                    endpoint="https://api.test.com/v1/traces",
                    headers={"Authorization": "Bearer test-key"},
                )

    def test_uses_batch_processor_by_default(self):
        """Test that batch processor is used by default."""
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"):
            with patch("traceai_agno._instrumentor.trace"):
                with patch("traceai_agno._instrumentor.BatchSpanProcessor") as mock_batch_processor:
                    setup_traceai_exporter(endpoint="https://api.test.com/v1/traces")

                    mock_batch_processor.assert_called_once()

    def test_can_use_simple_processor(self):
        """Test that simple processor can be used."""
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"):
            with patch("traceai_agno._instrumentor.trace"):
                with patch("traceai_agno._instrumentor.SimpleSpanProcessor") as mock_simple_processor:
                    setup_traceai_exporter(
                        endpoint="https://api.test.com/v1/traces",
                        use_batch_processor=False,
                    )

                    mock_simple_processor.assert_called_once()

    def test_creates_resource_with_service_name(self):
        """Test that resource is created with service name."""
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"):
            with patch("traceai_agno._instrumentor.trace"):
                with patch("traceai_agno._instrumentor.Resource") as mock_resource:
                    setup_traceai_exporter(
                        endpoint="https://api.test.com/v1/traces",
                        service_name="my-agno-service",
                    )

                    mock_resource.create.assert_called_once_with({"service.name": "my-agno-service"})

    def test_default_service_name(self):
        """Test default service name."""
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"):
            with patch("traceai_agno._instrumentor.trace"):
                with patch("traceai_agno._instrumentor.Resource") as mock_resource:
                    setup_traceai_exporter(endpoint="https://api.test.com/v1/traces")

                    mock_resource.create.assert_called_once_with({"service.name": "agno-agent"})

    def test_import_error_for_missing_otlp_exporter(self):
        """Test ImportError when OTLP exporter not installed."""
        # Make the import inside setup_traceai_exporter fail
        with patch.dict("sys.modules", {"opentelemetry.exporter.otlp.proto.http.trace_exporter": None}):
            with pytest.raises(ImportError) as exc_info:
                setup_traceai_exporter(endpoint="https://api.test.com/v1/traces")

            assert "opentelemetry-exporter-otlp" in str(exc_info.value)


class TestIntegration:
    """Integration tests for the instrumentor module."""

    def test_full_configuration_flow(self, reset_instrumentor, mock_tracer_provider):
        """Test complete configuration flow."""
        with patch.object(AgnoInstrumentorWrapper, "_setup_agno_instrumentor"):
            with patch("traceai_agno._instrumentor.trace"):
                wrapper = configure_agno_tracing(
                    tracer_provider=mock_tracer_provider,
                    otlp_endpoint="https://api.test.com/v1/traces",
                    otlp_headers={"Authorization": "Bearer test-key"},
                    project_name="test-project",
                )

        assert wrapper.is_instrumented is True

    def test_wrapper_reuse(self, reset_instrumentor):
        """Test that wrapper is reused across calls."""
        with patch.object(AgnoInstrumentorWrapper, "_setup_agno_instrumentor"):
            wrapper1 = configure_agno_tracing()
            wrapper2 = configure_agno_tracing()

        assert wrapper1 is wrapper2

    def test_uninstrument_and_reinstrument(self, reset_instrumentor):
        """Test uninstrumenting and re-instrumenting."""
        with patch.object(AgnoInstrumentorWrapper, "_setup_agno_instrumentor"):
            wrapper = configure_agno_tracing()

        assert wrapper.is_instrumented is True

        wrapper.uninstrument()
        assert wrapper.is_instrumented is False

        # Re-instrument
        AgnoInstrumentorWrapper._is_instrumented = False  # Reset flag
        with patch.object(wrapper, "_setup_agno_instrumentor"):
            wrapper.instrument()

        assert wrapper.is_instrumented is True
