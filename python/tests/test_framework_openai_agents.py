"""
Comprehensive test suite for OpenAI Agents instrumentation framework.
Tests the OpenAIAgentsInstrumentor and FiTracingProcessor functionality.
"""

import logging
import unittest
from unittest.mock import Mock, patch, MagicMock, ANY, call
from typing import Any, Collection

import pytest
from agents import Trace, TracingProcessor
from agents.tracing import Span
from agents.tracing.span_data import SpanData, ResponseSpanData, GenerationSpanData, FunctionSpanData

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.trace import Tracer, Status, StatusCode
from opentelemetry.trace.span import Span as OtelSpan
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.context import attach, detach
from opentelemetry.util.types import AttributeValue

from traceai_openai_agents import OpenAIAgentsInstrumentor
from traceai_openai_agents._processor import FiTracingProcessor


class TestOpenAIAgentsInstrumentor:
    """Test suite for OpenAIAgentsInstrumentor class."""

    def test_inheritance(self):
        """Test that OpenAIAgentsInstrumentor inherits from BaseInstrumentor."""
        instrumentor = OpenAIAgentsInstrumentor()
        assert isinstance(instrumentor, BaseInstrumentor)

    def test_instrumentation_dependencies(self):
        """Test that instrumentor returns correct dependencies."""
        instrumentor = OpenAIAgentsInstrumentor()
        dependencies = instrumentor.instrumentation_dependencies()
        assert isinstance(dependencies, Collection)
        assert "openai-agents >= 0.0.7" in dependencies

    @patch('traceai_openai_agents.trace_api.get_tracer_provider')
    @patch('traceai_openai_agents.trace_api.get_tracer')
    @patch('agents.add_trace_processor')
    def test_instrument_with_default_config(self, mock_add_processor, mock_get_tracer, mock_get_provider):
        """Test instrumentation with default configuration."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_provider.return_value = mock_provider
        mock_get_tracer.return_value = mock_tracer

        instrumentor = OpenAIAgentsInstrumentor()
        instrumentor._instrument()

        mock_get_provider.assert_called_once()
        mock_get_tracer.assert_called_once()
        mock_add_processor.assert_called_once()
        
        # Verify FiTracingProcessor was created with correct tracer
        args, kwargs = mock_add_processor.call_args
        processor = args[0]
        assert isinstance(processor, FiTracingProcessor)

    @patch('traceai_openai_agents.trace_api.get_tracer')
    @patch('agents.add_trace_processor')
    def test_instrument_with_custom_tracer_provider(self, mock_add_processor, mock_get_tracer):
        """Test instrumentation with custom tracer provider."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_tracer.return_value = mock_tracer

        instrumentor = OpenAIAgentsInstrumentor()
        instrumentor._instrument(tracer_provider=mock_provider)

        mock_get_tracer.assert_called_once()
        mock_add_processor.assert_called_once()

    @patch('traceai_openai_agents.trace_api.get_tracer_provider')
    @patch('traceai_openai_agents.trace_api.get_tracer')
    @patch('agents.add_trace_processor')
    def test_instrument_with_custom_config(self, mock_add_processor, mock_get_tracer, mock_get_provider):
        """Test instrumentation with custom trace config."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_provider.return_value = mock_provider
        mock_get_tracer.return_value = mock_tracer
        
        custom_config = TraceConfig()
        instrumentor = OpenAIAgentsInstrumentor()
        instrumentor._instrument(config=custom_config)

        mock_add_processor.assert_called_once()

    @patch('traceai_openai_agents.trace_api.get_tracer_provider')
    @patch('traceai_openai_agents.trace_api.get_tracer')
    @patch('agents.add_trace_processor')
    def test_instrument_with_invalid_config_type(self, mock_add_processor, mock_get_tracer, mock_get_provider):
        """Test instrumentation fails with invalid config type."""
        mock_provider = Mock()
        mock_get_provider.return_value = mock_provider

        instrumentor = OpenAIAgentsInstrumentor()
        
        with pytest.raises(AssertionError):
            instrumentor._instrument(config="invalid_config")

    @patch('agents.add_trace_processor')
    def test_instrument_missing_agents_import(self, mock_add_processor):
        """Test instrumentation handles missing agents import gracefully."""
        mock_add_processor.side_effect = ImportError("No module named 'agents'")
        
        instrumentor = OpenAIAgentsInstrumentor()
        
        with pytest.raises(ImportError):
            instrumentor._instrument()

    @patch('traceai_openai_agents.trace_api.get_tracer_provider')
    @patch('agents.add_trace_processor')
    def test_instrument_exception_handling(self, mock_add_processor, mock_get_provider):
        """Test instrumentation handles exceptions properly."""
        mock_get_provider.side_effect = Exception("Test exception")
        
        instrumentor = OpenAIAgentsInstrumentor()
        
        with pytest.raises(Exception, match="Test exception"):
            instrumentor._instrument()

    @patch('traceai_openai_agents.logger')
    @patch('traceai_openai_agents.trace_api.get_tracer_provider')
    @patch('agents.add_trace_processor')
    def test_instrument_logs_exception(self, mock_add_processor, mock_get_provider, mock_logger):
        """Test that instrumentation logs exceptions."""
        mock_get_provider.side_effect = Exception("Test exception")
        
        instrumentor = OpenAIAgentsInstrumentor()
        
        with pytest.raises(Exception):
            instrumentor._instrument()
        
        mock_logger.exception.assert_called_once()

    def test_uninstrument_todo(self):
        """Test that uninstrument method exists but is not implemented."""
        instrumentor = OpenAIAgentsInstrumentor()
        # Should not raise an exception
        instrumentor._uninstrument()


class TestFiTracingProcessor:
    """Test suite for FiTracingProcessor class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = Mock(spec=Tracer)
        self.processor = FiTracingProcessor(self.mock_tracer)

    def test_initialization(self):
        """Test processor initialization."""
        assert self.processor._tracer == self.mock_tracer
        assert isinstance(self.processor._root_spans, dict)
        assert isinstance(self.processor._otel_spans, dict)
        assert isinstance(self.processor._tokens, dict)

    def test_inheritance(self):
        """Test that FiTracingProcessor inherits from TracingProcessor."""
        assert isinstance(self.processor, TracingProcessor)

    def test_on_trace_start(self):
        """Test trace start handling."""
        mock_span = Mock(spec=OtelSpan)
        self.mock_tracer.start_span.return_value = mock_span
        
        trace = Mock(spec=Trace)
        trace.name = "test_trace"
        trace.trace_id = "trace_123"
        
        self.processor.on_trace_start(trace)
        
        self.mock_tracer.start_span.assert_called_once_with(
            name="test_trace",
            attributes={"fi.span.kind": "AGENT"}
        )
        assert self.processor._root_spans["trace_123"] == mock_span

    def test_on_trace_end(self):
        """Test trace end handling."""
        mock_span = Mock(spec=OtelSpan)
        trace_id = "trace_123"
        self.processor._root_spans[trace_id] = mock_span
        
        trace = Mock(spec=Trace)
        trace.trace_id = trace_id
        
        self.processor.on_trace_end(trace)
        
        mock_span.set_status.assert_called_once_with(ANY)
        mock_span.end.assert_called_once()
        assert trace_id not in self.processor._root_spans

    def test_on_trace_end_missing_span(self):
        """Test trace end handling when span doesn't exist."""
        trace = Mock(spec=Trace)
        trace.trace_id = "nonexistent_trace"
        
        # Should not raise an exception
        self.processor.on_trace_end(trace)

    @patch('traceai_openai_agents._processor._as_utc_nano')
    @patch('traceai_openai_agents._processor._get_span_name')
    @patch('traceai_openai_agents._processor._get_span_kind')
    @patch('traceai_openai_agents._processor.safe_json_dumps')
    @patch('traceai_openai_agents._processor.set_span_in_context')
    @patch('traceai_openai_agents._processor.attach')
    def test_on_span_start(self, mock_attach, mock_set_context, mock_json_dumps, 
                          mock_get_kind, mock_get_name, mock_utc_nano):
        """Test span start handling."""
        # Setup mocks
        mock_otel_span = Mock(spec=OtelSpan)
        self.mock_tracer.start_span.return_value = mock_otel_span
        mock_get_name.return_value = "test_span"
        mock_get_kind.return_value = "test_kind"
        mock_json_dumps.return_value = '{"test": "data"}'
        mock_utc_nano.return_value = 1234567890
        mock_context = Mock()
        mock_set_context.return_value = mock_context
        mock_token = Mock()
        mock_attach.return_value = mock_token
        
        # Create test span
        span = Mock()
        span.span_id = "span_123"
        span.trace_id = "trace_123"
        span.parent_id = None
        span.started_at = "2023-01-01T00:00:00"
        span.span_data = Mock(spec=SpanData)
        
        # Add root span
        root_span = Mock(spec=OtelSpan)
        self.processor._root_spans["trace_123"] = root_span
        
        self.processor.on_span_start(span)
        
        # Verify span creation
        self.mock_tracer.start_span.assert_called_once()
        assert self.processor._otel_spans["span_123"] == mock_otel_span
        assert self.processor._tokens["span_123"] == mock_token

    def test_on_span_start_no_timestamp(self):
        """Test span start handling when timestamp is missing."""
        span = Mock()
        span.started_at = None
        
        self.processor.on_span_start(span)
        
        # Should not call start_span if no timestamp
        self.mock_tracer.start_span.assert_not_called()

    @patch('traceai_openai_agents._processor.detach')
    @patch('traceai_openai_agents._processor._get_span_name')
    def test_on_span_end(self, mock_get_name, mock_detach):
        """Test span end handling."""
        # Setup
        span_id = "span_123"
        mock_token = Mock()
        mock_otel_span = Mock(spec=OtelSpan)
        mock_get_name.return_value = "test_span"
        
        self.processor._tokens[span_id] = mock_token
        self.processor._otel_spans[span_id] = mock_otel_span
        
        span = Mock()
        span.span_id = span_id
        span.ended_at = "2023-01-01T00:01:00"
        span.span_data = Mock(spec=SpanData)
        
        self.processor.on_span_end(span)
        
        # Verify cleanup
        mock_detach.assert_called_once_with(mock_token)
        mock_otel_span.update_name.assert_called_once_with("test_span")
        mock_otel_span.end.assert_called_once()
        assert span_id not in self.processor._tokens
        assert span_id not in self.processor._otel_spans

    @patch('traceai_openai_agents._processor._get_attributes_from_response')
    def test_on_span_end_response_span_data(self, mock_get_attributes):
        """Test span end handling with ResponseSpanData."""
        from openai.types.responses import Response
        
        # Setup
        span_id = "span_123"
        mock_otel_span = Mock(spec=OtelSpan)
        self.processor._otel_spans[span_id] = mock_otel_span
        
        # Create response span data
        mock_response = Mock(spec=Response)
        mock_response.model_dump_json.return_value = '{"test": "response"}'
        mock_get_attributes.return_value = [("attr1", "value1")]
        
        response_data = Mock(spec=ResponseSpanData)
        response_data.response = mock_response
        response_data.input = "test input"
        
        span = Mock()
        span.span_id = span_id
        span.ended_at = None
        span.span_data = response_data
        
        self.processor.on_span_end(span)
        
        # Verify response handling
        mock_otel_span.set_attribute.assert_any_call("output.mime_type", "application/json")
        mock_otel_span.set_attribute.assert_any_call("raw.output", '{"test": "response"}')

    @patch('traceai_openai_agents._processor._get_attributes_from_generation_span_data')
    def test_on_span_end_generation_span_data(self, mock_get_attributes):
        """Test span end handling with GenerationSpanData."""
        # Setup
        span_id = "span_123"
        mock_otel_span = Mock(spec=OtelSpan)
        self.processor._otel_spans[span_id] = mock_otel_span
        mock_get_attributes.return_value = [("attr1", "value1")]
        
        generation_data = Mock(spec=GenerationSpanData)
        generation_data.input = {"test": "input"}
        generation_data.output = {"test": "output"}
        
        span = Mock()
        span.span_id = span_id
        span.ended_at = None
        span.span_data = generation_data
        
        self.processor.on_span_end(span)
        
        # Verify generation handling
        mock_get_attributes.assert_called_once_with(generation_data)

    @patch('traceai_openai_agents._processor._get_attributes_from_function_span_data')
    def test_on_span_end_function_span_data(self, mock_get_attributes):
        """Test span end handling with FunctionSpanData."""
        # Setup
        span_id = "span_123"
        mock_otel_span = Mock(spec=OtelSpan)
        self.processor._otel_spans[span_id] = mock_otel_span
        mock_get_attributes.return_value = [("attr1", "value1")]
        
        function_data = Mock(spec=FunctionSpanData)
        function_data.input = {"test": "input"}
        function_data.output = {"test": "output"}
        
        span = Mock()
        span.span_id = span_id
        span.ended_at = None
        span.span_data = function_data
        
        self.processor.on_span_end(span)
        
        # Verify function handling
        mock_get_attributes.assert_called_once_with(function_data)

    def test_on_span_end_missing_span(self):
        """Test span end handling when span doesn't exist."""
        span = Mock()
        span.span_id = "nonexistent_span"
        span.span_data = Mock(spec=SpanData)
        
        # Should not raise an exception
        self.processor.on_span_end(span)

    @patch('traceai_openai_agents._processor._as_utc_nano')
    def test_on_span_end_with_end_time(self, mock_utc_nano):
        """Test span end handling with end timestamp."""
        # Setup
        span_id = "span_123"
        mock_otel_span = Mock(spec=OtelSpan)
        self.processor._otel_spans[span_id] = mock_otel_span
        mock_utc_nano.return_value = 1234567890
        
        span = Mock()
        span.span_id = span_id
        span.ended_at = "2023-01-01T00:01:00"
        span.span_data = Mock(spec=SpanData)
        
        self.processor.on_span_end(span)
        
        mock_otel_span.end.assert_called_once_with(1234567890)

    def test_on_span_end_invalid_end_time(self):
        """Test span end handling with invalid timestamp."""
        # Setup
        span_id = "span_123"
        mock_otel_span = Mock(spec=OtelSpan)
        self.processor._otel_spans[span_id] = mock_otel_span
        
        span = Mock()
        span.span_id = span_id
        span.ended_at = "invalid_timestamp"
        span.span_data = Mock(spec=SpanData)
        
        self.processor.on_span_end(span)
        
        # Should call end without timestamp
        mock_otel_span.end.assert_called_once_with(None)

    def test_force_flush(self):
        """Test force flush method."""
        # Should not raise an exception (TODO implementation)
        self.processor.force_flush()

    def test_shutdown(self):
        """Test shutdown method."""
        # Should not raise an exception (TODO implementation)
        self.processor.shutdown()


class TestProcessorUtilityFunctions:
    """Test suite for processor utility functions."""

    def test_as_utc_nano(self):
        """Test UTC nano timestamp conversion."""
        from traceai_openai_agents._processor import _as_utc_nano
        from datetime import datetime, timezone
        
        dt = datetime(2023, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = _as_utc_nano(dt)
        
        expected = int(dt.timestamp() * 1_000_000_000)
        assert result == expected

    def test_get_span_name_with_name_attribute(self):
        """Test span name extraction when span data has name."""
        from traceai_openai_agents._processor import _get_span_name
        
        span_data = Mock()
        span_data.name = "test_span_name"
        
        span = Mock()
        span.span_data = span_data
        
        result = _get_span_name(span)
        assert result == "test_span_name"

    def test_get_span_name_handoff_span(self):
        """Test span name extraction for handoff spans."""
        from traceai_openai_agents._processor import _get_span_name
        from agents.tracing.span_data import HandoffSpanData
        
        span_data = Mock(spec=HandoffSpanData)
        span_data.to_agent = "target_agent"
        
        span = Mock()
        span.span_data = span_data
        
        result = _get_span_name(span)
        assert result == "handoff to target_agent"

    def test_get_span_name_fallback_to_type(self):
        """Test span name extraction falls back to type."""
        from traceai_openai_agents._processor import _get_span_name
        
        span_data = Mock()
        span_data.type = "test_type"
        
        span = Mock()
        span.span_data = span_data
        
        result = _get_span_name(span)
        assert result == "test_type"

    def test_get_span_kind_response_span(self):
        """Test span kind extraction for response spans."""
        from traceai_openai_agents._processor import _get_span_kind
        
        span_data = Mock(spec=ResponseSpanData)
        
        result = _get_span_kind(span_data)
        assert result == "LLM"

    def test_get_span_kind_generation_span(self):
        """Test span kind extraction for generation spans."""
        from traceai_openai_agents._processor import _get_span_kind
        
        span_data = Mock(spec=GenerationSpanData)
        
        result = _get_span_kind(span_data)
        assert result == "LLM"

    def test_get_span_kind_function_span(self):
        """Test span kind extraction for function spans."""
        from traceai_openai_agents._processor import _get_span_kind
        
        span_data = Mock(spec=FunctionSpanData)
        
        result = _get_span_kind(span_data)
        assert result == "TOOL"

    def test_get_span_kind_default(self):
        """Test span kind extraction for other span types."""
        from traceai_openai_agents._processor import _get_span_kind
        
        span_data = Mock(spec=SpanData)
        
        result = _get_span_kind(span_data)
        assert result == "CHAIN"


class TestIntegrationScenarios:
    """Integration tests covering end-to-end scenarios."""

    @patch('agents.add_trace_processor')
    @patch('traceai_openai_agents.trace_api.get_tracer_provider')
    @patch('traceai_openai_agents.trace_api.get_tracer')
    def test_full_instrumentation_lifecycle(self, mock_get_tracer, mock_get_provider, mock_add_processor):
        """Test complete instrumentation and processor setup."""
        mock_provider = Mock()
        mock_tracer = Mock(spec=Tracer)
        mock_get_provider.return_value = mock_provider
        mock_get_tracer.return_value = mock_tracer

        instrumentor = OpenAIAgentsInstrumentor()
        instrumentor._instrument()

        # Verify tracer setup
        mock_get_tracer.assert_called_once()
        mock_add_processor.assert_called_once()
        
        # Verify processor is FiTracingProcessor
        args, kwargs = mock_add_processor.call_args
        processor = args[0]
        assert isinstance(processor, FiTracingProcessor)

    def test_processor_trace_lifecycle(self):
        """Test complete trace lifecycle through processor."""
        mock_tracer = Mock(spec=Tracer)
        mock_root_span = Mock(spec=OtelSpan)
        mock_tracer.start_span.return_value = mock_root_span
        
        processor = FiTracingProcessor(mock_tracer)
        
        # Start trace
        trace = Mock(spec=Trace)
        trace.name = "test_trace"
        trace.trace_id = "trace_123"
        
        processor.on_trace_start(trace)
        assert "trace_123" in processor._root_spans
        
        # End trace
        processor.on_trace_end(trace)
        assert "trace_123" not in processor._root_spans
        mock_root_span.set_status.assert_called_once()
        mock_root_span.end.assert_called_once()

    @patch('traceai_openai_agents._processor._as_utc_nano')
    @patch('traceai_openai_agents._processor._get_span_name')
    @patch('traceai_openai_agents._processor._get_span_kind')
    @patch('traceai_openai_agents._processor.safe_json_dumps')
    @patch('traceai_openai_agents._processor.set_span_in_context')
    @patch('traceai_openai_agents._processor.attach')
    @patch('traceai_openai_agents._processor.detach')
    def test_processor_span_lifecycle(self, mock_detach, mock_attach, mock_set_context, 
                                    mock_json_dumps, mock_get_kind, mock_get_name, mock_utc_nano):
        """Test complete span lifecycle through processor."""
        # Setup mocks
        mock_tracer = Mock(spec=Tracer)
        mock_otel_span = Mock(spec=OtelSpan)
        mock_tracer.start_span.return_value = mock_otel_span
        mock_get_name.return_value = "test_span"
        mock_get_kind.return_value = "test_kind"
        mock_json_dumps.return_value = '{"test": "data"}'
        mock_utc_nano.return_value = 1234567890
        mock_token = Mock()
        mock_attach.return_value = mock_token
        
        processor = FiTracingProcessor(mock_tracer)
        
        # Create test span
        span = Mock()
        span.span_id = "span_123"
        span.trace_id = "trace_123"
        span.parent_id = None
        span.started_at = "2023-01-01T00:00:00"
        span.ended_at = "2023-01-01T00:01:00"
        span.span_data = Mock(spec=SpanData)
        
        # Add root span
        root_span = Mock(spec=OtelSpan)
        processor._root_spans["trace_123"] = root_span
        
        # Test span lifecycle
        processor.on_span_start(span)
        assert "span_123" in processor._otel_spans
        assert "span_123" in processor._tokens
        
        processor.on_span_end(span)
        assert "span_123" not in processor._otel_spans
        assert "span_123" not in processor._tokens
        
        # Verify calls
        mock_detach.assert_called_once_with(mock_token)
        mock_otel_span.end.assert_called_once()

    def test_processor_error_handling(self):
        """Test processor handles errors gracefully."""
        mock_tracer = Mock(spec=Tracer)
        processor = FiTracingProcessor(mock_tracer)
        
        # Test with invalid trace
        invalid_trace = Mock()
        invalid_trace.trace_id = None
        
        # Should not raise exceptions
        processor.on_trace_start(invalid_trace)
        processor.on_trace_end(invalid_trace)
        
        # Test with invalid span
        invalid_span = Mock()
        invalid_span.span_id = None
        invalid_span.started_at = None
        
        # Should not raise exceptions
        processor.on_span_start(invalid_span)
        processor.on_span_end(invalid_span)


class TestErrorHandling:
    """Test error handling scenarios."""

    @patch('agents.add_trace_processor')
    def test_instrumentation_with_import_error(self, mock_add_processor):
        """Test instrumentation when agents module is not available."""
        mock_add_processor.side_effect = ImportError("No module named 'agents'")
        
        instrumentor = OpenAIAgentsInstrumentor()
        
        with pytest.raises(ImportError):
            instrumentor._instrument()

    @patch('traceai_openai_agents.trace_api.get_tracer_provider')
    def test_instrumentation_with_tracer_error(self, mock_get_provider):
        """Test instrumentation when tracer provider fails."""
        mock_get_provider.side_effect = Exception("Tracer provider error")
        
        instrumentor = OpenAIAgentsInstrumentor()
        
        with pytest.raises(Exception, match="Tracer provider error"):
            instrumentor._instrument()

    def test_processor_with_invalid_tracer(self):
        """Test processor creation with invalid tracer."""
        # Should not raise exception during creation
        processor = FiTracingProcessor(None)
        assert processor._tracer is None

    def test_processor_resilience_to_span_errors(self):
        """Test processor handles span creation errors properly."""
        mock_tracer = Mock(spec=Tracer)
        mock_tracer.start_span.side_effect = Exception("Span creation failed")
        
        processor = FiTracingProcessor(mock_tracer)
        
        # Should raise exception as expected
        trace = Mock()
        trace.name = "test"
        trace.trace_id = "123"
        
        with pytest.raises(Exception, match="Span creation failed"):
            processor.on_trace_start(trace)


if __name__ == "__main__":
    pytest.main([__file__]) 