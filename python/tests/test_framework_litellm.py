import asyncio
import json
from typing import Any, Dict, List, AsyncIterator, Iterator
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
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from traceai_litellm import (
    LiteLLMInstrumentor,
    StreamingIteratorWrapper,
    AsyncStreamingIteratorWrapper,
    _set_span_attribute,
    _instrument_func_type_completion,
    _instrument_func_type_embedding,
    _instrument_func_type_image_generation,
    _finalize_span,
    _process_messages,
    _get_attributes_from_message_param,
    _get_attributes_from_message_content,
    _get_attributes_from_image,
    is_iterable_of,
)


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
def mock_litellm():
    """Mock LiteLLM functions."""
    with patch("litellm.completion") as mock_completion:
        with patch("litellm.acompletion") as mock_acompletion:
            with patch("litellm.completion_with_retries") as mock_retries:
                with patch("litellm.acompletion_with_retries") as mock_aretries:
                    with patch("litellm.embedding") as mock_embedding:
                        with patch("litellm.aembedding") as mock_aembedding:
                            with patch("litellm.image_generation") as mock_image:
                                with patch("litellm.aimage_generation") as mock_aimage:
                                    yield {
                                        "completion": mock_completion,
                                        "acompletion": mock_acompletion,
                                        "completion_with_retries": mock_retries,
                                        "acompletion_with_retries": mock_aretries,
                                        "embedding": mock_embedding,
                                        "aembedding": mock_aembedding,
                                        "image_generation": mock_image,
                                        "aimage_generation": mock_aimage,
                                    }


@pytest.fixture
def sample_completion_response():
    """Sample completion response."""
    mock_response = MagicMock()
    mock_response.choices = [
        MagicMock(message=MagicMock(content="Hello! How can I help you?"))
    ]
    mock_response.to_dict.return_value = {
        "choices": [{"message": {"content": "Hello! How can I help you?"}}]
    }
    return mock_response


@pytest.fixture
def sample_embedding_response():
    """Sample embedding response."""
    mock_response = MagicMock()
    mock_response.data = [MagicMock(embedding=[0.1, 0.2, 0.3])]
    mock_response.to_dict.return_value = {
        "data": [{"embedding": [0.1, 0.2, 0.3]}]
    }
    return mock_response


@pytest.fixture
def sample_image_response():
    """Sample image generation response."""
    mock_response = MagicMock()
    mock_response.data = [MagicMock(url="https://example.com/image.png")]
    mock_response.to_dict.return_value = {
        "data": [{"url": "https://example.com/image.png"}]
    }
    return mock_response


class TestLiteLLMInstrumentor:
    """⚡ Test LiteLLMInstrumentor lifecycle and basic functionality."""

    def test_instrumentation_dependencies(self):
        """Test instrumentor dependencies are properly declared."""
        instrumentor = LiteLLMInstrumentor()
        dependencies = instrumentor.instrumentation_dependencies()
        
        assert isinstance(dependencies, tuple)
        assert "litellm" in str(dependencies)
        assert len(dependencies) >= 1

    def test_instrument_basic(self, tracer_provider, config, mock_litellm):
        """Test basic instrumentation setup."""
        instrumentor = LiteLLMInstrumentor()
        
        # Mock the original functions before instrumentation
        instrumentor.original_litellm_funcs["completion"] = mock_litellm["completion"]
        instrumentor.original_litellm_funcs["acompletion"] = mock_litellm["acompletion"]
        instrumentor.original_litellm_funcs["completion_with_retries"] = mock_litellm["completion_with_retries"]
        instrumentor.original_litellm_funcs["acompletion_with_retries"] = mock_litellm["acompletion_with_retries"]
        instrumentor.original_litellm_funcs["embedding"] = mock_litellm["embedding"]
        instrumentor.original_litellm_funcs["aembedding"] = mock_litellm["aembedding"]
        instrumentor.original_litellm_funcs["image_generation"] = mock_litellm["image_generation"]
        instrumentor.original_litellm_funcs["aimage_generation"] = mock_litellm["aimage_generation"]
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Verify tracer was created
        assert hasattr(instrumentor, '_tracer')
        assert isinstance(instrumentor._tracer, FITracer)
        
        # Verify original functions were stored
        assert len(instrumentor.original_litellm_funcs) == 8

    def test_instrument_without_tracer_provider(self, config, mock_litellm):
        """Test instrumentation without explicit tracer provider."""
        instrumentor = LiteLLMInstrumentor()
        
        with patch("opentelemetry.trace.get_tracer_provider") as mock_get_provider:
            mock_get_provider.return_value = MagicMock()
            instrumentor._instrument(config=config)
            mock_get_provider.assert_called_once()

    def test_instrument_without_config(self, tracer_provider, mock_litellm):
        """Test instrumentation without explicit config."""
        instrumentor = LiteLLMInstrumentor()
        
        instrumentor._instrument(tracer_provider=tracer_provider)
        
        # Should create default TraceConfig
        assert hasattr(instrumentor, '_tracer')
        assert isinstance(instrumentor._tracer, FITracer)

    def test_uninstrument(self, tracer_provider, config, mock_litellm):
        """Test proper uninstrumentation."""
        instrumentor = LiteLLMInstrumentor()
        
        # Store original functions
        instrumentor.original_litellm_funcs["completion"] = mock_litellm["completion"]
        instrumentor.original_litellm_funcs["acompletion"] = mock_litellm["acompletion"]
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Verify instrumentation
        assert instrumentor._tracer is not None
        
        # Test uninstrumentation
        instrumentor._uninstrument()
        
        # Verify cleanup
        assert instrumentor._tracer is None


class TestCompletionInstrumentation:
    """Test completion function instrumentation."""

    def test_completion_basic(self, tracer_provider, config, mock_litellm, sample_completion_response):
        """Test basic completion instrumentation."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["completion"] = mock_litellm["completion"]
        mock_litellm["completion"].return_value = sample_completion_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test completion call
        result = instrumentor._completion_wrapper(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Hello"}]
        )
        
        assert result == sample_completion_response
        mock_litellm["completion"].assert_called_once()

    @pytest.mark.asyncio
    async def test_acompletion_basic(self, tracer_provider, config, mock_litellm, sample_completion_response):
        """Test basic async completion instrumentation."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["acompletion"] = mock_litellm["acompletion"]
        
        # Configure AsyncMock to return the response when awaited
        mock_litellm["acompletion"].return_value = sample_completion_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test async completion call
        result = await instrumentor._acompletion_wrapper(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Hello"}]
        )
        
        assert result == sample_completion_response
        mock_litellm["acompletion"].assert_called_once()

    def test_completion_with_retries(self, tracer_provider, config, mock_litellm, sample_completion_response):
        """Test completion with retries instrumentation."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["completion_with_retries"] = mock_litellm["completion_with_retries"]
        mock_litellm["completion_with_retries"].return_value = sample_completion_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test completion with retries call
        result = instrumentor._completion_with_retries_wrapper(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Hello"}],
            max_retries=3
        )
        
        assert result == sample_completion_response
        mock_litellm["completion_with_retries"].assert_called_once()

    @pytest.mark.asyncio
    async def test_acompletion_with_retries(self, tracer_provider, config, mock_litellm, sample_completion_response):
        """Test async completion with retries instrumentation."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["acompletion_with_retries"] = mock_litellm["acompletion_with_retries"]
        
        # Configure AsyncMock to return the response when awaited
        mock_litellm["acompletion_with_retries"].return_value = sample_completion_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test async completion with retries call
        result = await instrumentor._acompletion_with_retries_wrapper(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Hello"}],
            max_retries=3
        )
        
        assert result == sample_completion_response
        mock_litellm["acompletion_with_retries"].assert_called_once()

    def test_completion_with_streaming(self, tracer_provider, config, mock_litellm):
        """Test completion with streaming response."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["completion"] = mock_litellm["completion"]
        
        # Mock streaming response
        mock_stream_chunk = MagicMock()
        mock_stream_chunk.to_dict.return_value = {
            "choices": [{"delta": {"content": "Hello"}}]
        }
        mock_stream = [mock_stream_chunk]
        mock_litellm["completion"].return_value = iter(mock_stream)
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test streaming completion call
        result = instrumentor._completion_wrapper(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Hello"}],
            stream=True
        )
        
        # Should return StreamingIteratorWrapper
        assert isinstance(result, StreamingIteratorWrapper)
        
        # Consume the stream
        chunks = list(result)
        assert len(chunks) == 1

    def test_completion_suppressed(self, tracer_provider, config, mock_litellm, sample_completion_response):
        """Test completion with instrumentation suppressed."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["completion"] = mock_litellm["completion"]
        mock_litellm["completion"].return_value = sample_completion_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Set instrumentation suppression
        with patch.object(context_api, 'get_value', return_value=True):
            result = instrumentor._completion_wrapper(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "Hello"}]
            )
        
        assert result == sample_completion_response
        mock_litellm["completion"].assert_called_once()


class TestEmbeddingInstrumentation:
    """Test embedding function instrumentation."""

    def test_embedding_basic(self, tracer_provider, config, mock_litellm, sample_embedding_response):
        """Test basic embedding instrumentation."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["embedding"] = mock_litellm["embedding"]
        mock_litellm["embedding"].return_value = sample_embedding_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test embedding call
        result = instrumentor._embedding_wrapper(
            model="text-embedding-ada-002",
            input="Hello world"
        )
        
        assert result == sample_embedding_response
        mock_litellm["embedding"].assert_called_once()

    @pytest.mark.asyncio
    async def test_aembedding_basic(self, tracer_provider, config, mock_litellm, sample_embedding_response):
        """Test basic async embedding instrumentation."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["aembedding"] = mock_litellm["aembedding"]
        
        # Configure AsyncMock to return the response when awaited
        mock_litellm["aembedding"].return_value = sample_embedding_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test async embedding call
        result = await instrumentor._aembedding_wrapper(
            model="text-embedding-ada-002",
            input="Hello world"
        )
        
        assert result == sample_embedding_response
        mock_litellm["aembedding"].assert_called_once()

    def test_embedding_suppressed(self, tracer_provider, config, mock_litellm, sample_embedding_response):
        """Test embedding with instrumentation suppressed."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["embedding"] = mock_litellm["embedding"]
        mock_litellm["embedding"].return_value = sample_embedding_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Set instrumentation suppression
        with patch.object(context_api, 'get_value', return_value=True):
            result = instrumentor._embedding_wrapper(
                model="text-embedding-ada-002",
                input="Hello world"
            )
        
        assert result == sample_embedding_response
        mock_litellm["embedding"].assert_called_once()


class TestImageGenerationInstrumentation:
    """🎨 Test image generation function instrumentation."""

    def test_image_generation_basic(self, tracer_provider, config, mock_litellm, sample_image_response):
        """Test basic image generation instrumentation."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["image_generation"] = mock_litellm["image_generation"]
        mock_litellm["image_generation"].return_value = sample_image_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test image generation call
        result = instrumentor._image_generation_wrapper(
            model="dall-e-3",
            prompt="A beautiful sunset"
        )
        
        assert result == sample_image_response
        mock_litellm["image_generation"].assert_called_once()

    @pytest.mark.asyncio
    async def test_aimage_generation_basic(self, tracer_provider, config, mock_litellm, sample_image_response):
        """Test basic async image generation instrumentation."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["aimage_generation"] = mock_litellm["aimage_generation"]
        
        # Configure AsyncMock to return the response when awaited
        mock_litellm["aimage_generation"].return_value = sample_image_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test async image generation call
        result = await instrumentor._aimage_generation_wrapper(
            model="dall-e-3",
            prompt="A beautiful sunset"
        )
        
        assert result == sample_image_response
        mock_litellm["aimage_generation"].assert_called_once()

    def test_image_generation_suppressed(self, tracer_provider, config, mock_litellm, sample_image_response):
        """Test image generation with instrumentation suppressed."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["image_generation"] = mock_litellm["image_generation"]
        mock_litellm["image_generation"].return_value = sample_image_response
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Set instrumentation suppression
        with patch.object(context_api, 'get_value', return_value=True):
            result = instrumentor._image_generation_wrapper(
                model="dall-e-3",
                prompt="A beautiful sunset"
            )
        
        assert result == sample_image_response
        mock_litellm["image_generation"].assert_called_once()


class TestStreamingWrappers:
    """🌊 Test streaming response wrapper functionality."""

    def test_streaming_iterator_wrapper_basic(self):
        """Test basic StreamingIteratorWrapper functionality."""
        mock_span = MagicMock()
        mock_span_cm = MagicMock()
        
        # Mock stream data
        mock_chunk = MagicMock()
        mock_chunk.to_dict.return_value = {
            "choices": [{"delta": {"content": "Hello"}}]
        }
        mock_iterator = iter([mock_chunk])
        
        wrapper = StreamingIteratorWrapper(mock_iterator, mock_span, mock_span_cm)
        
        # Test iteration
        chunks = list(wrapper)
        assert len(chunks) == 1
        assert chunks[0] == mock_chunk
        
        # Verify span attributes were set
        mock_span.set_attribute.assert_called()

    def test_streaming_iterator_wrapper_context_manager(self):
        """Test StreamingIteratorWrapper as context manager."""
        mock_span = MagicMock()
        mock_span_cm = MagicMock()
        mock_iterator = iter([])
        
        wrapper = StreamingIteratorWrapper(mock_iterator, mock_span, mock_span_cm)
        
        # Test context manager
        with wrapper as w:
            assert w == wrapper
        
        # Verify span was closed
        mock_span_cm.__exit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_streaming_iterator_wrapper_basic(self):
        """Test basic AsyncStreamingIteratorWrapper functionality."""
        mock_span = MagicMock()
        mock_span_cm = MagicMock()
        
        # Mock async stream data
        mock_chunk = MagicMock()
        mock_chunk.to_dict.return_value = {
            "choices": [{"delta": {"content": "Hello"}}]
        }
        
        async def mock_async_iter():
            yield mock_chunk
        
        wrapper = AsyncStreamingIteratorWrapper(mock_async_iter(), mock_span, mock_span_cm)
        
        # Test async iteration
        chunks = []
        async for chunk in wrapper:
            chunks.append(chunk)
        
        assert len(chunks) == 1
        assert chunks[0] == mock_chunk
        
        # Verify span attributes were set
        mock_span.set_attribute.assert_called()

    @pytest.mark.asyncio
    async def test_async_streaming_iterator_wrapper_close(self):
        """Test AsyncStreamingIteratorWrapper close functionality."""
        mock_span = MagicMock()
        mock_span_cm = MagicMock()
        
        async def mock_async_iter():
            yield MagicMock()
        
        wrapper = AsyncStreamingIteratorWrapper(mock_async_iter(), mock_span, mock_span_cm)
        
        # Test close
        await wrapper.close()
        
        # Verify span was closed
        mock_span_cm.__exit__.assert_called_once()


class TestUtilityFunctions:
    """🛠️ Test utility functions."""

    def test_set_span_attribute(self):
        """Test _set_span_attribute utility function."""
        mock_span = MagicMock()
        
        # Test with valid value
        _set_span_attribute(mock_span, "test.attribute", "test_value")
        mock_span.set_attribute.assert_called_once_with("test.attribute", "test_value")
        
        # Test with None value
        mock_span.reset_mock()
        _set_span_attribute(mock_span, "test.none", None)
        mock_span.set_attribute.assert_not_called()
        
        # Test with empty string
        mock_span.reset_mock()
        _set_span_attribute(mock_span, "test.empty", "")
        mock_span.set_attribute.assert_not_called()

    def test_is_iterable_of(self):
        """Test is_iterable_of utility function."""
        # Test with list of dicts
        assert is_iterable_of([{"key": "value"}, {"key2": "value2"}], dict)
        
        # Test with list of strings
        assert is_iterable_of(["a", "b", "c"], str)
        
        # Test with mixed types
        assert not is_iterable_of([{"key": "value"}, "string"], dict)
        
        # Test with string (strings are iterable and contain str characters)
        assert is_iterable_of("string", str)
        
        # Test with non-iterable
        assert not is_iterable_of(42, int)

    def test_get_attributes_from_message_param(self):
        """Test _get_attributes_from_message_param utility function."""
        message = {
            "role": "user",
            "content": "Hello world"
        }
        
        attributes = list(_get_attributes_from_message_param(message))
        
        # Should extract role and content
        assert len(attributes) >= 2
        role_attr = next((attr for attr in attributes if "role" in attr[0]), None)
        content_attr = next((attr for attr in attributes if "content" in attr[0]), None)
        
        assert role_attr is not None
        assert content_attr is not None
        assert role_attr[1] == "user"
        assert content_attr[1] == "Hello world"

    def test_get_attributes_from_message_content(self):
        """Test _get_attributes_from_message_content utility function."""
        content = {
            "type": "text",
            "text": "Hello world"
        }
        
        attributes = list(_get_attributes_from_message_content(content))
        
        # Should extract type and text
        assert len(attributes) >= 2
        type_attr = next((attr for attr in attributes if "type" in attr[0]), None)
        text_attr = next((attr for attr in attributes if "text" in attr[0]), None)
        
        assert type_attr is not None
        assert text_attr is not None
        assert type_attr[1] == "text"
        assert text_attr[1] == "Hello world"

    def test_get_attributes_from_image(self):
        """Test _get_attributes_from_image utility function."""
        image = {
            "url": "https://example.com/image.png"
        }
        
        attributes = list(_get_attributes_from_image(image))
        
        # Should extract URL
        assert len(attributes) >= 1
        url_attr = attributes[0]
        assert "url" in url_attr[0].lower()
        assert url_attr[1] == "https://example.com/image.png"


class TestInstrumentationFunctions:
    """Test instrumentation helper functions."""

    def test_instrument_func_type_completion(self):
        """Test _instrument_func_type_completion function."""
        mock_span = MagicMock()
        kwargs = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "max_tokens": 100
        }
        
        _instrument_func_type_completion(mock_span, kwargs)
        
        # Verify span attributes were set
        mock_span.set_attribute.assert_called()
        
        # Check that proper attributes were set
        call_args_list = [call[0] for call in mock_span.set_attribute.call_args_list]
        attribute_names = [args[0] for args in call_args_list]
        
        assert SpanAttributes.FI_SPAN_KIND in attribute_names
        assert SpanAttributes.LLM_MODEL_NAME in attribute_names

    def test_instrument_func_type_embedding(self):
        """Test _instrument_func_type_embedding function."""
        mock_span = MagicMock()
        kwargs = {
            "model": "text-embedding-ada-002",
            "input": "Hello world"
        }
        
        _instrument_func_type_embedding(mock_span, kwargs)
        
        # Verify span attributes were set
        mock_span.set_attribute.assert_called()
        
        # Check that proper attributes were set
        call_args_list = [call[0] for call in mock_span.set_attribute.call_args_list]
        attribute_names = [args[0] for args in call_args_list]
        
        assert SpanAttributes.FI_SPAN_KIND in attribute_names
        assert SpanAttributes.EMBEDDING_MODEL_NAME in attribute_names

    def test_instrument_func_type_image_generation(self):
        """Test _instrument_func_type_image_generation function."""
        mock_span = MagicMock()
        kwargs = {
            "model": "dall-e-3",
            "prompt": "A beautiful sunset"
        }
        
        _instrument_func_type_image_generation(mock_span, kwargs)
        
        # Verify span attributes were set
        mock_span.set_attribute.assert_called()
        
        # Check that proper attributes were set
        call_args_list = [call[0] for call in mock_span.set_attribute.call_args_list]
        attribute_names = [args[0] for args in call_args_list]
        
        assert SpanAttributes.FI_SPAN_KIND in attribute_names
        assert SpanAttributes.LLM_MODEL_NAME in attribute_names

    def test_finalize_span(self, sample_completion_response):
        """Test _finalize_span function."""
        mock_span = MagicMock()
        
        _finalize_span(mock_span, sample_completion_response)
        
        # Verify span attributes were set
        mock_span.set_attribute.assert_called()

    def test_process_messages_basic(self):
        """Test _process_messages function with basic messages."""
        messages = [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Hi there!"}
        ]
        
        result = _process_messages(messages)
        
        assert result is not None
        assert "filtered_messages" in result
        assert "eval_input" in result
        assert "query" in result
        assert result["filtered_messages"] == messages
        assert "Hello world" in result["eval_input"]

    def test_process_messages_with_images(self):
        """Test _process_messages function with image content."""
        messages = [
            {
                "role": "user", 
                "content": [
                    {"type": "text", "text": "What's in this image?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
                ]
            }
        ]
        
        result = _process_messages(messages)
        
        assert result is not None
        assert "input_images" in result
        assert result["input_images"] is not None
        assert len(result["input_images"]) == 1
        assert result["input_images"][0] == "https://example.com/image.png"

    def test_process_messages_error_handling(self):
        """Test _process_messages error handling."""
        # Test with malformed messages
        malformed_messages = "not a list"
        
        result = _process_messages(malformed_messages)
        
        # Should not raise error and return safe defaults
        assert result is not None
        assert "filtered_messages" in result
        assert result["filtered_messages"] == malformed_messages


class TestErrorHandling:
    """Test error handling scenarios."""

    def test_completion_wrapper_error(self, tracer_provider, config, mock_litellm):
        """Test completion wrapper with error."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["completion"] = mock_litellm["completion"]
        mock_litellm["completion"].side_effect = Exception("API Error")
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test that error is propagated
        with pytest.raises(Exception, match="API Error"):
            instrumentor._completion_wrapper(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "Hello"}]
            )

    @pytest.mark.asyncio
    async def test_acompletion_wrapper_error(self, tracer_provider, config, mock_litellm):
        """Test async completion wrapper with error."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["acompletion"] = mock_litellm["acompletion"]
        
        # Configure mock to raise exception when called
        mock_litellm["acompletion"].side_effect = Exception("Async API Error")
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test that error is propagated
        with pytest.raises(Exception, match="Async API Error"):
            await instrumentor._acompletion_wrapper(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "Hello"}]
            )

    def test_embedding_wrapper_error(self, tracer_provider, config, mock_litellm):
        """Test embedding wrapper with error."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["embedding"] = mock_litellm["embedding"]
        mock_litellm["embedding"].side_effect = Exception("Embedding Error")
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test that error is propagated
        with pytest.raises(Exception, match="Embedding Error"):
            instrumentor._embedding_wrapper(
                model="text-embedding-ada-002",
                input="Hello world"
            )

    def test_image_generation_wrapper_error(self, tracer_provider, config, mock_litellm):
        """Test image generation wrapper with error."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["image_generation"] = mock_litellm["image_generation"]
        mock_litellm["image_generation"].side_effect = Exception("Image Error")
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test that error is propagated
        with pytest.raises(Exception, match="Image Error"):
            instrumentor._image_generation_wrapper(
                model="dall-e-3",
                prompt="A beautiful sunset"
            )


class TestIntegrationScenarios:
    """🎭 Test integration scenarios and real-world usage patterns."""

    def test_full_instrumentation_flow(self, tracer_provider, config, mock_litellm, sample_completion_response):
        """Test complete LiteLLM instrumentation flow."""
        instrumentor = LiteLLMInstrumentor()
        
        # Set up all original functions
        for func_name in ["completion", "acompletion", "completion_with_retries", 
                         "acompletion_with_retries", "embedding", "aembedding", 
                         "image_generation", "aimage_generation"]:
            instrumentor.original_litellm_funcs[func_name] = mock_litellm[func_name]
        
        mock_litellm["completion"].return_value = sample_completion_response
        
        # Instrument
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test completion
        result = instrumentor._completion_wrapper(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Hello"}]
        )
        
        assert result == sample_completion_response
        
        # Test uninstrumentation
        instrumentor._uninstrument()
        assert instrumentor._tracer is None

    def test_complex_message_processing(self):
        """Test complex message processing scenarios."""
        complex_messages = [
            {
                "role": "system",
                "content": "You are a helpful assistant."
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analyze this image and text:"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/chart.png"}},
                    {"type": "text", "text": "What do you see?"}
                ]
            },
            {
                "role": "assistant", 
                "content": "I can see a chart showing data trends."
            }
        ]
        
        result = _process_messages(complex_messages)
        
        assert result is not None
        assert result["input_images"] is not None
        assert len(result["input_images"]) == 1
        assert "chart.png" in result["input_images"][0]
        assert "Analyze this image" in result["eval_input"]
        assert "What do you see?" in result["eval_input"]

    def test_streaming_with_context_attributes(self, tracer_provider, config, mock_litellm):
        """Test streaming response with context attributes."""
        instrumentor = LiteLLMInstrumentor()
        instrumentor.original_litellm_funcs["completion"] = mock_litellm["completion"]
        
        # Mock streaming chunks
        chunks = []
        for i, word in enumerate(["Hello", " world", "!"]):
            chunk = MagicMock()
            chunk.to_dict.return_value = {
                "choices": [{"delta": {"content": word}}]
            }
            chunks.append(chunk)
        
        mock_litellm["completion"].return_value = iter(chunks)
        
        instrumentor._instrument(tracer_provider=tracer_provider, config=config)
        
        # Test streaming with context
        with patch("traceai_litellm.get_attributes_from_context") as mock_context:
            mock_context.return_value = {"user_id": "test123", "session_id": "session456"}
            
            result = instrumentor._completion_wrapper(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "Hello"}],
                stream=True
            )
            
            # Consume stream
            response_chunks = list(result)
            assert len(response_chunks) == 3
            
            # Verify context was used
            mock_context.assert_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"]) 