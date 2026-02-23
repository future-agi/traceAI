"""Tests for traceai_cerebras instrumentor."""

import pytest
from unittest.mock import MagicMock, patch

from traceai_cerebras import CerebrasInstrumentor, __version__


class TestCerebrasInstrumentor:
    """Tests for CerebrasInstrumentor class."""

    def test_version_defined(self):
        """Test that version is defined."""
        assert __version__ == "0.1.0"

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        instrumentor = CerebrasInstrumentor()
        deps = instrumentor.instrumentation_dependencies()

        assert "cerebras-cloud-sdk >= 1.0.0" in deps

    @patch("traceai_cerebras.wrap_function_wrapper")
    @patch("traceai_cerebras.trace_api")
    def test_instrument_with_tracer_provider(self, mock_trace_api, mock_wrap):
        """Test instrumentation with tracer provider."""
        mock_provider = MagicMock()
        mock_trace_api.get_tracer_provider.return_value = mock_provider
        mock_trace_api.get_tracer.return_value = MagicMock()

        # Mock Cerebras SDK import
        mock_completions_module = MagicMock()
        mock_completions_module.CompletionsResource = MagicMock()
        mock_completions_module.AsyncCompletionsResource = MagicMock()

        with patch.dict("sys.modules", {
            "cerebras": MagicMock(),
            "cerebras.cloud": MagicMock(),
            "cerebras.cloud.sdk": MagicMock(),
            "cerebras.cloud.sdk.resources": MagicMock(),
            "cerebras.cloud.sdk.resources.chat": MagicMock(),
            "cerebras.cloud.sdk.resources.chat.completions": mock_completions_module,
        }):
            instrumentor = CerebrasInstrumentor()
            instrumentor._instrument(tracer_provider=mock_provider)

    @patch("traceai_cerebras.wrap_function_wrapper")
    @patch("traceai_cerebras.trace_api")
    def test_instrument_without_tracer_provider(self, mock_trace_api, mock_wrap):
        """Test instrumentation without tracer provider uses default."""
        mock_provider = MagicMock()
        mock_trace_api.get_tracer_provider.return_value = mock_provider
        mock_trace_api.get_tracer.return_value = MagicMock()

        mock_completions_module = MagicMock()
        mock_completions_module.CompletionsResource = MagicMock()
        mock_completions_module.AsyncCompletionsResource = MagicMock()

        with patch.dict("sys.modules", {
            "cerebras": MagicMock(),
            "cerebras.cloud": MagicMock(),
            "cerebras.cloud.sdk": MagicMock(),
            "cerebras.cloud.sdk.resources": MagicMock(),
            "cerebras.cloud.sdk.resources.chat": MagicMock(),
            "cerebras.cloud.sdk.resources.chat.completions": mock_completions_module,
        }):
            instrumentor = CerebrasInstrumentor()
            instrumentor._instrument()

            mock_trace_api.get_tracer_provider.assert_called()

    def test_instrument_handles_import_error(self):
        """Test that instrumentation handles missing SDK gracefully."""
        instrumentor = CerebrasInstrumentor()

        # Should not raise even if SDK is not installed
        with patch("traceai_cerebras.wrap_function_wrapper"):
            with patch("traceai_cerebras.trace_api"):
                # Remove the cerebras module from sys.modules to simulate import error
                import sys
                saved_modules = {}
                for key in list(sys.modules.keys()):
                    if key.startswith("cerebras"):
                        saved_modules[key] = sys.modules.pop(key)

                try:
                    # Mock an ImportError when trying to import
                    def raise_import_error(*args, **kwargs):
                        raise ImportError("No module named 'cerebras'")

                    with patch("builtins.__import__", side_effect=raise_import_error):
                        # This should log a warning but not raise
                        instrumentor._instrument()
                finally:
                    # Restore the modules
                    sys.modules.update(saved_modules)

    def test_uninstrument(self):
        """Test uninstrumentation."""
        instrumentor = CerebrasInstrumentor()
        instrumentor._original_completions_create = MagicMock()
        instrumentor._original_async_completions_create = MagicMock()

        # Mock the module for uninstrument
        mock_completions_module = MagicMock()
        mock_completions_module.CompletionsResource = MagicMock()
        mock_completions_module.AsyncCompletionsResource = MagicMock()

        with patch.dict("sys.modules", {
            "cerebras.cloud.sdk.resources.chat.completions": mock_completions_module,
        }):
            # Should not raise
            instrumentor._uninstrument()


class TestCerebrasInstrumentorIntegration:
    """Integration tests for CerebrasInstrumentor."""

    def test_instrumentor_singleton_pattern(self):
        """Test that instrumentor uses singleton pattern."""
        instrumentor1 = CerebrasInstrumentor()
        instrumentor2 = CerebrasInstrumentor()

        # BaseInstrumentor implements singleton pattern
        assert instrumentor1 is instrumentor2

    def test_instrument_idempotent(self):
        """Test that calling instrument multiple times is safe."""
        instrumentor = CerebrasInstrumentor()

        mock_completions_module = MagicMock()
        mock_completions_module.CompletionsResource = MagicMock()
        mock_completions_module.AsyncCompletionsResource = MagicMock()

        with patch("traceai_cerebras.wrap_function_wrapper"):
            with patch("traceai_cerebras.trace_api"):
                with patch.dict("sys.modules", {
                    "cerebras": MagicMock(),
                    "cerebras.cloud": MagicMock(),
                    "cerebras.cloud.sdk": MagicMock(),
                    "cerebras.cloud.sdk.resources": MagicMock(),
                    "cerebras.cloud.sdk.resources.chat": MagicMock(),
                    "cerebras.cloud.sdk.resources.chat.completions": mock_completions_module,
                }):
                    instrumentor._instrument()
                    instrumentor._instrument()  # Should not raise
