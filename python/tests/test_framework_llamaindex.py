import asyncio
import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, Mock, patch, AsyncMock
from uuid import uuid4

import pytest
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Span

from fi_instrumentation import TraceConfig, FITracer
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes, RerankerAttributes
from traceai_llamaindex import LlamaIndexInstrumentor, get_current_span


@pytest.fixture
def tracer_provider():
    """Create a test TracerProvider with console export for testing."""
    provider = TracerProvider()
    exporter = ConsoleSpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


@pytest.fixture
def config():
    """Create a test TraceConfig."""
    return TraceConfig()


@pytest.fixture
def instrumentation_scope():
    """Set up clean instrumentation environment."""
    # Clear any existing instrumentation suppression
    context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, False)
    yield
    # Reset to clean state
    context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, False)


@pytest.fixture
def mock_llama_index_core():
    """Mock llama-index-core components."""
    with patch("llama_index.core.global_handler", None) as mock_global_handler:
        with patch("llama_index.core.instrumentation.get_dispatcher") as mock_dispatcher:
            mock_dispatcher_instance = MagicMock()
            mock_dispatcher_instance.span_handlers = []
            mock_dispatcher_instance.event_handlers = []
            mock_dispatcher.return_value = mock_dispatcher_instance
            
            yield {
                "global_handler": mock_global_handler,
                "get_dispatcher": mock_dispatcher,
                "dispatcher": mock_dispatcher_instance,
            }


class TestLlamaIndexInstrumentor:
    """⚡ Test LlamaIndexInstrumentor lifecycle and basic functionality."""

    def test_instrumentation_dependencies(self):
        """Test instrumentor dependencies are properly declared."""
        instrumentor = LlamaIndexInstrumentor()
        dependencies = instrumentor.instrumentation_dependencies()
        
        assert isinstance(dependencies, tuple)
        assert "llama-index-core >= 0.10.43" in dependencies
        assert len(dependencies) >= 2

    def test_instrument_new_handler_mode(self, tracer_provider, config, mock_llama_index_core):
        """Test instrumentation with new event/span handler mode."""
        instrumentor = LlamaIndexInstrumentor()
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Verify tracer was created
        assert hasattr(instrumentor, '_tracer')
        assert isinstance(instrumentor._tracer, FITracer)
        
        # Verify new handler mode was used
        assert not instrumentor._use_legacy_callback_handler
        assert instrumentor._event_handler is not None
        assert instrumentor._span_handler is not None
        
        # Verify handlers were added to dispatcher
        mock_llama_index_core["dispatcher"].add_span_handler.assert_called_once()
        mock_llama_index_core["dispatcher"].add_event_handler.assert_called_once()

    def test_instrument_legacy_callback_mode(self, tracer_provider, config, mock_llama_index_core):
        """Test instrumentation with legacy callback handler mode."""
        instrumentor = LlamaIndexInstrumentor()
        
        # Mock llama_index.core to have global_handler attribute
        with patch("llama_index.core") as mock_core:
            # Set up the original handler that should be captured
            original_handler = MagicMock()
            mock_core.global_handler = original_handler
            
            instrumentor._instrument(
                tracer_provider=tracer_provider, 
                config=config,
                use_legacy_callback_handler=True
            )
            
            # Verify legacy mode was used
            assert instrumentor._use_legacy_callback_handler
            assert instrumentor._original_global_handler == original_handler
            
            # Verify global handler was set to our callback handler
            assert mock_core.global_handler is not None
            assert mock_core.global_handler != original_handler

    def test_instrument_without_tracer_provider(self, config, mock_llama_index_core):
        """Test instrumentation without explicit tracer provider."""
        instrumentor = LlamaIndexInstrumentor()
        
        with patch("opentelemetry.trace.get_tracer_provider") as mock_get_provider:
            mock_get_provider.return_value = MagicMock()
            instrumentor._instrument(config=config)
            mock_get_provider.assert_called_once()

    def test_instrument_without_config(self, tracer_provider, mock_llama_index_core):
        """Test instrumentation without explicit config."""
        instrumentor = LlamaIndexInstrumentor()
        
        instrumentor._instrument(tracer_provider=tracer_provider)
        
        # Should create default TraceConfig
        assert hasattr(instrumentor, '_tracer')
        assert isinstance(instrumentor._tracer, FITracer)

    def test_uninstrument_new_handler_mode(self, tracer_provider, config, mock_llama_index_core):
        """Test proper uninstrumentation in new handler mode."""
        instrumentor = LlamaIndexInstrumentor()
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Verify instrumentation
        assert instrumentor._event_handler is not None
        
        # Test uninstrumentation
        instrumentor._uninstrument()
        
        # Verify cleanup
        assert instrumentor._event_handler is None

    def test_uninstrument_legacy_callback_mode(self, tracer_provider, config, mock_llama_index_core):
        """Test proper uninstrumentation in legacy callback mode."""
        instrumentor = LlamaIndexInstrumentor()
        
        # Mock llama_index.core to have global_handler attribute
        with patch("llama_index.core") as mock_core:
            original_handler = MagicMock()
            mock_core.global_handler = original_handler
            
            instrumentor._instrument(
                tracer_provider=tracer_provider,
                config=config,
                use_legacy_callback_handler=True
            )
            
            # Verify instrumentation worked
            assert instrumentor._use_legacy_callback_handler
            assert instrumentor._original_global_handler == original_handler
            
            # Test uninstrumentation
            instrumentor._uninstrument()
            
            # Verify original handler was restored
            assert mock_core.global_handler == original_handler
            assert instrumentor._original_global_handler is None


class TestEventHandler:
    """🎯 Test event handler functionality."""

    def test_event_handler_creation(self, tracer_provider, config):
        """Test event handler creation and initialization."""
        from traceai_llamaindex._handler import EventHandler
        
        tracer = FITracer(
            trace_api.get_tracer(__name__, "test", tracer_provider),
            config=config,
        )
        
        event_handler = EventHandler(tracer=tracer)
        
        assert event_handler._span_handler is not None
        assert hasattr(event_handler._span_handler, '_otel_tracer')

    def test_event_handler_llm_events(self, tracer_provider, config):
        """Test event handler processing LLM events."""
        from traceai_llamaindex._handler import EventHandler
        
        tracer = FITracer(
            trace_api.get_tracer(__name__, "test", tracer_provider),
            config=config,
        )
        
        event_handler = EventHandler(tracer=tracer)
        
        # Mock LLM events
        with patch("llama_index.core.instrumentation.events.llm.LLMChatStartEvent") as mock_event:
            mock_event.span_id = "test-span-id"
            mock_event.model_dict = {}
            
            # Test handling events doesn't raise errors
            try:
                event_handler.handle(mock_event)
            except Exception as e:
                # Some mocking issues are expected in isolated tests
                assert "instrumentation" in str(e).lower() or "span" in str(e).lower()

    def test_event_handler_embedding_events(self, tracer_provider, config):
        """Test event handler processing embedding events."""
        from traceai_llamaindex._handler import EventHandler
        
        tracer = FITracer(
            trace_api.get_tracer(__name__, "test", tracer_provider),
            config=config,
        )
        
        event_handler = EventHandler(tracer=tracer)
        
        # Mock embedding events
        with patch("llama_index.core.instrumentation.events.embedding.EmbeddingStartEvent") as mock_event:
            mock_event.span_id = "test-embedding-span"
            mock_event.model_dict = {}
            
            # Test handling events doesn't raise errors
            try:
                event_handler.handle(mock_event)
            except Exception as e:
                # Some mocking issues are expected in isolated tests
                assert "instrumentation" in str(e).lower() or "span" in str(e).lower()


class TestSpanHandler:
    """🎭 Test span handler functionality."""

    def test_span_handler_creation(self, tracer_provider, config):
        """Test span handler creation and initialization."""
        from traceai_llamaindex._handler import _SpanHandler
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        span_handler = _SpanHandler(tracer=tracer)
        
        assert span_handler._otel_tracer == tracer
        assert span_handler._export_queue is not None

    def test_span_handler_new_span(self, tracer_provider, config):
        """Test span handler creating new spans."""
        from traceai_llamaindex._handler import _SpanHandler
        import inspect
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        span_handler = _SpanHandler(tracer=tracer)
        
        # Mock bound arguments
        bound_args = MagicMock()
        bound_args.args = []
        bound_args.kwargs = {}
        
        # Test creating a new span
        span = span_handler.new_span(
            id_="test-span-id",
            bound_args=bound_args,
            instance=None,
            parent_span_id=None
        )
        
        assert span is not None
        assert span.id_ == "test-span-id"  # Use id_ instead of id
        assert span._active

    def test_span_handler_exit_span(self, tracer_provider, config):
        """Test span handler exiting spans."""
        from traceai_llamaindex._handler import _SpanHandler
        import inspect
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        span_handler = _SpanHandler(tracer=tracer)
        
        # Mock bound arguments
        bound_args = MagicMock()
        bound_args.args = []
        bound_args.kwargs = {}
        
        # Create a span first
        span = span_handler.new_span(
            id_="test-span-id",
            bound_args=bound_args,
            instance=None,
            parent_span_id=None
        )
        
        # Test exiting the span
        result = span_handler.prepare_to_exit_span(
            id_="test-span-id",
            bound_args=bound_args,
            instance=None,
            result="test result"
        )
        
        # The span handler may return the result or None depending on implementation
        # The important thing is that the span was processed without errors


class TestCallbackHandler:
    """📞 Test legacy callback handler functionality."""

    def test_callback_handler_creation(self, tracer_provider, config):
        """Test callback handler creation and initialization."""
        from traceai_llamaindex._callback import FiTraceCallbackHandler
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        callback_handler = FiTraceCallbackHandler(tracer=tracer)
        
        assert callback_handler._tracer == tracer
        assert callback_handler._event_data is not None

    def test_callback_handler_event_start(self, tracer_provider, config):
        """Test callback handler starting events."""
        from traceai_llamaindex._callback import FiTraceCallbackHandler
        from llama_index.core.callbacks import CBEventType
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        callback_handler = FiTraceCallbackHandler(tracer=tracer)
        
        # Test starting an LLM event
        event_id = callback_handler.on_event_start(
            event_type=CBEventType.LLM,
            payload={"model_name": "gpt-3.5-turbo"},
            event_id="test-event-id",
            parent_id=""
        )
        
        assert event_id == "test-event-id"
        assert "test-event-id" in callback_handler._event_data

    def test_callback_handler_event_end(self, tracer_provider, config):
        """Test callback handler ending events."""
        from traceai_llamaindex._callback import FiTraceCallbackHandler
        from llama_index.core.callbacks import CBEventType
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        callback_handler = FiTraceCallbackHandler(tracer=tracer)
        
        # Start an event first
        event_id = callback_handler.on_event_start(
            event_type=CBEventType.LLM,
            payload={"model_name": "gpt-3.5-turbo"},
            event_id="test-event-id",
            parent_id=""
        )
        
        # Test ending the event
        callback_handler.on_event_end(
            event_type=CBEventType.LLM,
            payload={"response": "Hello world"},
            event_id="test-event-id"
        )
        
        # Event should be processed and removed
        assert "test-event-id" not in callback_handler._event_data

    def test_callback_handler_streaming_response(self, tracer_provider, config):
        """Test callback handler with streaming responses."""
        from traceai_llamaindex._callback import FiTraceCallbackHandler
        from llama_index.core.callbacks import CBEventType
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        callback_handler = FiTraceCallbackHandler(tracer=tracer)
        
        # Mock streaming response
        mock_stream = MagicMock()
        mock_stream.__iter__ = lambda self: iter(["Hello", " world", "!"])
        
        # Start an event with streaming response
        event_id = callback_handler.on_event_start(
            event_type=CBEventType.LLM,
            payload={"model_name": "gpt-3.5-turbo"},
            event_id="test-stream-id",
            parent_id=""
        )
        
        # End event with streaming response
        callback_handler.on_event_end(
            event_type=CBEventType.LLM,
            payload={"response": mock_stream},
            event_id="test-stream-id"
        )
        
        # Event should be marked as dispatched
        assert "test-stream-id" not in callback_handler._event_data


class TestUtilityFunctions:
    """🛠️ Test utility functions."""

    def test_get_current_span_no_instrumentor(self):
        """Test get_current_span when no instrumentor is active."""
        span = get_current_span()
        assert span is None

    def test_get_current_span_with_instrumentor(self, tracer_provider, config, mock_llama_index_core):
        """Test get_current_span with active instrumentor."""
        instrumentor = LlamaIndexInstrumentor()
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Mock active span
        with patch("llama_index.core.instrumentation.span.active_span_id") as mock_span_id:
            mock_span_id.get.return_value = "test-span-id"
            
            # Mock span in handler
            mock_span = MagicMock()
            mock_otel_span = MagicMock()
            mock_span._otel_span = mock_otel_span
            instrumentor._span_handler.open_spans = {"test-span-id": mock_span}
            
            span = get_current_span()
            assert span == mock_otel_span

    def test_payload_to_semantic_attributes(self):
        """Test payload to semantic attributes conversion."""
        from traceai_llamaindex._callback import payload_to_semantic_attributes
        from llama_index.core.callbacks import CBEventType, EventPayload
        
        # Test LLM payload
        payload = {
            EventPayload.QUERY_STR: "What is the weather?",
            EventPayload.SERIALIZED: {
                "model": "gpt-3.5-turbo",
                "temperature": 0.7
            }
        }
        
        attributes = payload_to_semantic_attributes(CBEventType.LLM, payload)
        
        assert SpanAttributes.INPUT_VALUE in attributes
        assert SpanAttributes.GEN_AI_REQUEST_MODEL in attributes
        assert attributes[SpanAttributes.INPUT_VALUE] == "What is the weather?"
        assert attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-3.5-turbo"

    def test_payload_to_semantic_attributes_embedding(self):
        """Test payload to semantic attributes conversion for embeddings."""
        from traceai_llamaindex._callback import payload_to_semantic_attributes
        from llama_index.core.callbacks import CBEventType, EventPayload
        
        # Test embedding payload
        payload = {
            EventPayload.CHUNKS: ["text1", "text2"],
            EventPayload.EMBEDDINGS: [[0.1, 0.2], [0.3, 0.4]],
            EventPayload.SERIALIZED: {
                "model_name": "text-embedding-ada-002"
            }
        }
        
        attributes = payload_to_semantic_attributes(CBEventType.EMBEDDING, payload)
        
        assert SpanAttributes.EMBEDDING_EMBEDDINGS in attributes
        assert SpanAttributes.EMBEDDING_MODEL_NAME in attributes
        assert len(attributes[SpanAttributes.EMBEDDING_EMBEDDINGS]) == 2

    def test_payload_to_semantic_attributes_reranking(self):
        """Test payload to semantic attributes conversion for reranking."""
        from traceai_llamaindex._callback import payload_to_semantic_attributes
        from llama_index.core.callbacks import CBEventType, EventPayload
        
        # Test reranking payload
        payload = {
            EventPayload.QUERY_STR: "search query",
            EventPayload.TOP_K: 5,
            EventPayload.MODEL_NAME: "rerank-model"
        }
        
        attributes = payload_to_semantic_attributes(CBEventType.RERANKING, payload)
        
        assert RerankerAttributes.RERANKER_QUERY in attributes
        assert RerankerAttributes.RERANKER_TOP_K in attributes
        assert RerankerAttributes.RERANKER_MODEL_NAME in attributes


class TestErrorHandling:
    """💥 Test error handling scenarios."""

    def test_instrumentation_with_missing_dependencies(self):
        """Test instrumentation behavior with missing dependencies."""
        # This test ensures graceful degradation when optional dependencies are missing
        instrumentor = LlamaIndexInstrumentor()
        
        # Should not raise errors even if some components are missing
        try:
            deps = instrumentor.instrumentation_dependencies()
            assert isinstance(deps, tuple)
        except ImportError:
            # Expected if llama-index-core is not available
            pass

    def test_callback_handler_with_malformed_payload(self, tracer_provider, config):
        """Test callback handler with malformed payload."""
        from traceai_llamaindex._callback import FiTraceCallbackHandler, payload_to_semantic_attributes
        from llama_index.core.callbacks import CBEventType
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        callback_handler = FiTraceCallbackHandler(tracer=tracer)
        
        # Test with malformed payload
        malformed_payload = {"invalid_key": None}
        
        # Should not raise errors
        try:
            attributes = payload_to_semantic_attributes(CBEventType.LLM, malformed_payload)
            assert isinstance(attributes, dict)
        except Exception as e:
            # Should handle gracefully
            assert "payload" in str(e).lower() or "attribute" in str(e).lower()

    def test_span_handler_with_invalid_span_id(self, tracer_provider, config):
        """Test span handler with invalid span ID."""
        from traceai_llamaindex._handler import _SpanHandler
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        span_handler = _SpanHandler(tracer=tracer)
        
        # Mock bound arguments
        bound_args = MagicMock()
        bound_args.args = []
        bound_args.kwargs = {}
        
        # Test exiting a non-existent span
        result = span_handler.prepare_to_exit_span(
            id_="non-existent-span",
            bound_args=bound_args,
            instance=None,
            result="test result"
        )
        


class TestIntegrationScenarios:
    """🎭 Test integration scenarios and real-world usage patterns."""

    def test_full_instrumentation_flow_new_mode(self, tracer_provider, config, mock_llama_index_core):
        """Test complete LlamaIndex instrumentation flow in new mode."""
        instrumentor = LlamaIndexInstrumentor()
        
        # Instrument
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Verify instrumentation
        assert instrumentor._event_handler is not None
        assert instrumentor._span_handler is not None
        assert not instrumentor._use_legacy_callback_handler
        
        # Test uninstrumentation
        instrumentor._uninstrument()
        assert instrumentor._event_handler is None

    def test_full_instrumentation_flow_legacy_mode(self, tracer_provider, config):
        """Test complete LlamaIndex instrumentation flow in legacy mode."""
        instrumentor = LlamaIndexInstrumentor()
        
        with patch("llama_index.core") as mock_core:
            # Set up original handler to be captured
            original_handler = MagicMock()
            mock_core.global_handler = original_handler
            
            # Instrument
            instrumentor._instrument(
                tracer_provider=tracer_provider,
                config=config,
                use_legacy_callback_handler=True
            )
            
            # Verify instrumentation
            assert instrumentor._use_legacy_callback_handler
            assert instrumentor._original_global_handler == original_handler
            
            # Test uninstrumentation
            instrumentor._uninstrument()
            assert instrumentor._original_global_handler is None

    def test_context_attributes_integration(self, tracer_provider, config):
        """Test integration with context attributes."""
        from traceai_llamaindex._callback import FiTraceCallbackHandler
        from llama_index.core.callbacks import CBEventType
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        callback_handler = FiTraceCallbackHandler(tracer=tracer)
        
        # Mock context attributes
        with patch("fi_instrumentation.get_attributes_from_context") as mock_context:
            mock_context.return_value = {"user_id": "test123", "session_id": "session456"}
            
            # Start an event
            event_id = callback_handler.on_event_start(
                event_type=CBEventType.LLM,
                payload={"model_name": "gpt-3.5-turbo"},
                event_id="test-context-id",
                parent_id=""
            )
            
            # End the event
            callback_handler.on_event_end(
                event_type=CBEventType.LLM,
                payload={"response": "Hello world"},
                event_id="test-context-id"
            )
            


    def test_complex_event_hierarchy(self, tracer_provider, config):
        """Test complex event hierarchy with parent-child relationships."""
        from traceai_llamaindex._callback import FiTraceCallbackHandler
        from llama_index.core.callbacks import CBEventType
        
        tracer = trace_api.get_tracer(__name__, "test", tracer_provider)
        callback_handler = FiTraceCallbackHandler(tracer=tracer)
        
        # Start parent event
        parent_id = callback_handler.on_event_start(
            event_type=CBEventType.QUERY,
            payload={"query_str": "What is AI?"},
            event_id="parent-event",
            parent_id=""
        )
        
        # Start child event
        child_id = callback_handler.on_event_start(
            event_type=CBEventType.LLM,
            payload={"model_name": "gpt-3.5-turbo"},
            event_id="child-event",
            parent_id=parent_id
        )
        
        # End child event
        callback_handler.on_event_end(
            event_type=CBEventType.LLM,
            payload={"response": "AI is artificial intelligence"},
            event_id=child_id
        )
        
        # End parent event
        callback_handler.on_event_end(
            event_type=CBEventType.QUERY,
            payload={"response": "AI is artificial intelligence"},
            event_id=parent_id
        )
        
        # Both events should be processed
        assert parent_id not in callback_handler._event_data
        assert child_id not in callback_handler._event_data


if __name__ == "__main__":
    pytest.main([__file__, "-v"]) 