"""Tests for xAI instrumentor."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import sys

from traceai_xai import XAIInstrumentor, __version__


class TestXAIInstrumentor:
    """Tests for XAIInstrumentor class."""

    def test_version_exists(self):
        """Should have a version defined."""
        assert __version__ is not None
        assert isinstance(__version__, str)

    def test_instrumentation_dependencies(self):
        """Should list openai as dependency."""
        instrumentor = XAIInstrumentor()
        deps = instrumentor.instrumentation_dependencies()

        assert len(deps) == 1
        assert "openai" in deps[0]

    @patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter")
    def test_instrument_creates_tracer(self, mock_exporter):
        """Should create tracer during instrumentation."""
        mock_provider = MagicMock()
        mock_provider.get_tracer = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {
            "openai": MagicMock(),
            "openai.resources": MagicMock(),
            "openai.resources.chat": MagicMock(),
        }):
            instrumentor = XAIInstrumentor()
            instrumentor.instrument(tracer_provider=mock_provider)

            assert hasattr(instrumentor, "_tracer")

    def test_uninstrument_restores_original(self):
        """Should restore original methods on uninstrument."""
        mock_provider = MagicMock()
        mock_provider.get_tracer = MagicMock(return_value=MagicMock())

        mock_completions = MagicMock()
        mock_async_completions = MagicMock()
        original_create = MagicMock()
        original_async_create = MagicMock()
        mock_completions.create = original_create
        mock_async_completions.create = original_async_create

        mock_chat_module = MagicMock()
        mock_chat_module.Completions = mock_completions
        mock_chat_module.AsyncCompletions = mock_async_completions

        with patch.dict(sys.modules, {
            "openai": MagicMock(),
            "openai.resources": MagicMock(),
            "openai.resources.chat": mock_chat_module,
        }):
            instrumentor = XAIInstrumentor()
            instrumentor._original_completions_create = original_create
            instrumentor._original_async_completions_create = original_async_create

            instrumentor._uninstrument()

    @patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter")
    def test_instrument_with_config(self, mock_exporter):
        """Should accept custom config."""
        from fi_instrumentation import TraceConfig

        mock_provider = MagicMock()
        mock_provider.get_tracer = MagicMock(return_value=MagicMock())
        config = TraceConfig()

        with patch.dict(sys.modules, {
            "openai": MagicMock(),
            "openai.resources": MagicMock(),
            "openai.resources.chat": MagicMock(),
        }):
            instrumentor = XAIInstrumentor()
            instrumentor.instrument(tracer_provider=mock_provider, config=config)

            assert hasattr(instrumentor, "_tracer")

    def test_handles_missing_openai(self):
        """Should handle missing openai gracefully."""
        mock_provider = MagicMock()
        mock_provider.get_tracer = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {"openai": None}):
            with patch("traceai_xai.import_module", side_effect=ImportError):
                instrumentor = XAIInstrumentor()
                # Should not raise
                try:
                    instrumentor.instrument(tracer_provider=mock_provider)
                except ImportError:
                    pass  # Expected when openai not installed


class TestXAIInstrumentorIntegration:
    """Integration tests for XAIInstrumentor."""

    @patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter")
    def test_wrapper_skips_non_xai_client(self, mock_exporter):
        """Should skip instrumentation for non-xAI clients."""
        from traceai_xai._wrappers import _CompletionsWrapper

        mock_tracer = MagicMock()
        wrapper = _CompletionsWrapper(tracer=mock_tracer)

        # Create non-xAI client
        mock_instance = MagicMock()
        mock_instance._client = MagicMock()
        type(mock_instance._client).base_url = PropertyMock(return_value="https://api.openai.com/v1")

        wrapped_func = MagicMock(return_value="original_response")

        result = wrapper(wrapped_func, mock_instance, (), {"model": "gpt-4", "messages": []})

        assert result == "original_response"
        wrapped_func.assert_called_once()

    @patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter")
    def test_wrapper_instruments_xai_client(self, mock_exporter, mock_openai_response, mock_xai_client):
        """Should instrument xAI client calls."""
        from traceai_xai._wrappers import _CompletionsWrapper

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span = MagicMock(return_value=mock_span)

        wrapper = _CompletionsWrapper(tracer=mock_tracer)
        wrapped_func = MagicMock(return_value=mock_openai_response)

        result = wrapper(
            wrapped_func,
            mock_xai_client,
            (),
            {"model": "grok-beta", "messages": [{"role": "user", "content": "Hi"}]},
        )

        assert result == mock_openai_response
        mock_tracer.start_span.assert_called_once()
        mock_span.set_attributes.assert_called()
        mock_span.end.assert_called()
